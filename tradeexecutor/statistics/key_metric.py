"""Key metric calculations.

Calculate key metrics used in the web frontend summary cards.
"""
import datetime
from typing import List, Iterable

import pandas as pd
import numpy as np

from tradeexecutor.state.portfolio import Portfolio
from tradeexecutor.state.state import State
from tradeexecutor.state.types import Percent
from tradeexecutor.strategy.summary import KeyMetric, KeyMetricKind, KeyMetricSource, KeyMetricCalculationMethod
from tradeexecutor.visual.equity_curve import calculate_size_relative_realised_trading_returns


def calculate_sharpe(returns: pd.Series, periods=365) -> float:
    """Calculate annualised sharpe ratio.

    Internally uses quantstats.

    See :term:`sharpe`.

    :param returns:
        Returns series

    :param periods:
        How many periods per year returns series has

    """
    # Lazy import to allow optional dependency
    from quantstats.stats import sharpe
    return sharpe(
        returns,
        periods=periods,
    )


def calculate_sortino(returns: pd.Series, periods=365) -> float:
    """Calculate annualised share ratio.

    Internally uses quantstats.

    See :term:`sortino`.

    :param returns:
        Returns series

    :param periods:
        How many periods per year returns series has

    """
    # Lazy import to allow optional dependency
    from quantstats.stats import sortino
    return sortino(
        returns,
        periods=periods,
    )


def calculate_profit_factor(returns: pd.Series) -> float:
    """Calculate profit factor.

    Internally uses quantstats.

    See :term:`profit factor`.

    :param returns:
        Returns series

    """
    # Lazy import to allow optional dependency
    from quantstats.stats import profit_factor
    return profit_factor(returns)


def calculate_max_drawdown(returns: pd.Series) -> Percent:
    """Calculate maximum drawdown.

    Internally uses quantstats.

    See :term:`maximum drawdown`.

    :param returns:
        Returns series

    :return:
        Negative value 0...-1

    """
    # Lazy import to allow optional dependency
    from quantstats.stats import to_drawdown_series
    dd = to_drawdown_series(returns)
    return dd.min()


def calculate_max_runup(returns: pd.Series) -> Percent:
    """Calculate maximum runup. Somewhat manual implementation since quantstats doesn't have this.

    :param returns:
        Returns series (can use original returns, doesn't have to be daily returns since not annualised)

    :return:
        Positive value
    
    """

    from quantstats import utils

    # convert returns to runup series
    prices = utils._prepare_prices(returns)
    ru = prices / np.minimum.accumulate(prices) - 1.
    runup_series = ru.replace([np.inf, -np.inf, -0], 0)
    
    return runup_series.max()


def calculate_profitability(returns: pd.Series) -> Percent:
    """Calculate annualised profitability.

    Internally uses quantstats.

    See :term:`profitability`.

    :param returns:
        Returns series

    :return:
        Value -1...inf

    """
    compounded = returns.add(1).cumprod().sub(1)
    return compounded[-1]


def calculate_trades_last_week(portfolio: Portfolio, cut_off_date=None) -> int:
    """How many trades were executed last week.

    See :term:`trades last week`.
    """
    if cut_off_date is None:
        cut_off_date = datetime.datetime.utcnow() - datetime.timedelta(days=7)

    end_date = datetime.datetime.utcnow()

    trades = portfolio.get_all_trades()
    trades_last_week = [t for t in trades if t.is_success() and t.executed_at >= cut_off_date and t.executed_at <= end_date]
    return len(trades_last_week)


def calculate_key_metrics(
        live_state: State,
        backtested_state: State | None = None,
        required_history = datetime.timedelta(days=90),
        freq_base: pd.DateOffset = pd.offsets.Day(),
) -> Iterable[KeyMetric]:
    """Calculate summary metrics to be displayed on the web frontend.

    - Metrics are calculated either based live trading data or backtested data,
      whichever makes more sense

    - Live execution state is used if it has enough history

    :param live_state:
        The current live execution state

    :param backtested_state:
        The backtested state

    :param required_history:
        How long history we need before using live execution
        as the basis for the key metric calculations

    :param freq_base:
        The frequency for which we resample data when resamping is needed for calculations.

    :param now_:
        Override the current timestamp for testing

    :return:
        Key metrics.

        Currently sharpe, sortino, max drawdown and age.
    """

    assert isinstance(live_state, State)

    source_state = None
    source = None
    calculation_window_start_at = None
    calculation_window_end_at = None

    # Live history is calculated from the
    live_history = live_state.portfolio.get_trading_history_duration()
    if live_history is not None and live_history >= required_history:
        source_state = live_state
        source = KeyMetricSource.live_trading
        calculation_window_start_at = source_state.created_at
        calculation_window_end_at = datetime.datetime.utcnow()
    else:
        if backtested_state:
            if backtested_state.portfolio.get_trading_history_duration():
                source_state = backtested_state
                source = KeyMetricSource.backtesting
                first_trade, last_trade = source_state.portfolio.get_first_and_last_executed_trade()
                calculation_window_start_at = first_trade.executed_at
                calculation_window_end_at = last_trade.executed_at

    if source_state:

        # Use trading profitability instead of the fund performance
        # as the base for calculations to ensure
        # sharpe/sortino/etc. stays compatible regardless of deposit flow
        returns = calculate_size_relative_realised_trading_returns(source_state)
        returns = returns.resample(freq_base).max().fillna(0)

        periods = pd.Timedelta(days=365) / freq_base

        sharpe = calculate_sharpe(returns, periods=periods)
        yield KeyMetric.create_metric(KeyMetricKind.sharpe, source, sharpe, calculation_window_start_at, calculation_window_end_at, KeyMetricCalculationMethod.historical_data)

        sortino = calculate_sortino(returns, periods=periods)
        yield KeyMetric.create_metric(KeyMetricKind.sortino, source, sortino, calculation_window_start_at, calculation_window_end_at, KeyMetricCalculationMethod.historical_data)

        # Flip the sign of the max drawdown
        max_drawdown = -calculate_max_drawdown(returns)
        yield KeyMetric.create_metric(KeyMetricKind.max_drawdown, source, max_drawdown, calculation_window_start_at, calculation_window_end_at, KeyMetricCalculationMethod.historical_data)

        profitability = calculate_profitability(returns)
        yield KeyMetric.create_metric(KeyMetricKind.profitability, source, profitability, calculation_window_start_at, calculation_window_end_at, KeyMetricCalculationMethod.historical_data)

        if live_state:
            total_equity = live_state.portfolio.get_total_equity()

            # The total equity is made available always
            yield KeyMetric(
                KeyMetricKind.total_equity,
                KeyMetricSource.live_trading,
                total_equity,
                calculation_window_start_at=calculation_window_start_at,
                calculation_window_end_at=calculation_window_end_at,
                calculation_method=KeyMetricCalculationMethod.latest_value,
                help_link=KeyMetricKind.total_equity.get_help_link(),
            )

    else:
        # No live or backtesting data available,
        # mark all metrics N/A
        reason = "Not enough live trading or backtesting data available"
        calculation_window_start_at = None
        calculation_window_end_at = None

        yield KeyMetric.create_na(KeyMetricKind.sharpe, reason)
        yield KeyMetric.create_na(KeyMetricKind.sortino, reason)
        yield KeyMetric.create_na(KeyMetricKind.max_drawdown, reason)
        yield KeyMetric.create_na(KeyMetricKind.profitability, reason)
        yield KeyMetric.create_na(KeyMetricKind.total_equity, reason)

    # The age of the trading history is made available always
    yield KeyMetric(
        KeyMetricKind.started_at,
        KeyMetricSource.live_trading,
        live_state.created_at,
        calculation_window_start_at=calculation_window_start_at,
        calculation_window_end_at=calculation_window_end_at,
        calculation_method=KeyMetricCalculationMethod.latest_value,
        help_link=KeyMetricKind.started_at.get_help_link(),
    )

    # The age of the trading history is made available always
    #
    # Always live
    _, last_trade = live_state.portfolio.get_first_and_last_executed_trade()
    if last_trade is not None:
        yield KeyMetric(
            KeyMetricKind.last_trade,
            KeyMetricSource.live_trading,
            last_trade.executed_at,
            calculation_window_start_at=calculation_window_start_at,
            calculation_window_end_at=calculation_window_end_at,
            calculation_method=KeyMetricCalculationMethod.latest_value,
            help_link=KeyMetricKind.last_trade.get_help_link(),
        )

    # The age of the trading history is made available always
    #
    # Always live
    trades_last_week = calculate_trades_last_week(live_state.portfolio)
    yield KeyMetric(
        KeyMetricKind.trades_last_week,
        KeyMetricSource.live_trading,
        trades_last_week,
        calculation_window_start_at=calculation_window_start_at,
        calculation_window_end_at=calculation_window_end_at,
        calculation_method=KeyMetricCalculationMethod.latest_value,
        help_link=KeyMetricKind.trades_last_week.get_help_link(),
    )

