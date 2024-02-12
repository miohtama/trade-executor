"""Summary statistics are displayed on the summary tiles of the strategies."""
import datetime
from time import perf_counter
from typing import Optional
import logging

import pandas as pd

from tradeexecutor.state.state import State
from tradeexecutor.statistics.key_metric import calculate_key_metrics
from tradeexecutor.strategy.execution_context import ExecutionMode
from tradeexecutor.strategy.summary import StrategySummaryStatistics
from tradeexecutor.visual.equity_curve import calculate_compounding_realised_trading_profitability, calculate_cumulative_daily_returns
from tradeexecutor.visual.web_chart import export_time_series

logger = logging.getLogger(__name__)


def calculate_summary_statistics(
    state: State,
    execution_mode: ExecutionMode = ExecutionMode.one_off,
    time_window = pd.Timedelta(days=90),
    now_: Optional[pd.Timestamp | datetime.datetime] = None,
    legacy_workarounds=False,
    backtested_state: State | None = None,
    key_metrics_backtest_cut_off = datetime.timedelta(days=90),
) -> StrategySummaryStatistics:
    """Preprocess the strategy statistics for the summary card in the web frontend.

    TODO: Rename to `calculate_strategy_preview_statistics()`.

    To test out in the :ref:`console`:

    .. code-block:: python

        from tradeexecutor.statistics.summary import calculate_summary_statistics
        from tradeexecutor.strategy.execution_context import ExecutionMode

        calculate_summary_statistics(state, ExecutionMode.preflight_check)

    :param state:
        Strategy state from which we calculate the summary

    :param execution_mode:
        If we need to skip calculations during backtesting.

    :param time_window:
        How long we look back for the summary statistics

    :param now_:
        Override current time for unit testing.

        Set this to the date of the last trade.

    :param legacy_workarounds:
        Skip some calculations on old data, because data is missing.

    :param backtested_state:
        The result of the earlier backtest run.

        The live web server needs to show backtested metrics on the side of
        live trading metrics. This state is used to calculate them.

    :param key_metrics_backtest_cut_off:
        How many days live data is collected until key metrics are switched from backtest to live trading based,

    :return:
        Summary calculations for the summary tile,
        or empty `StrategySummaryStatistics` if cannot be calculated.
    """

    logger.info("calculate_summary_statistics() for %s", state.name)
    func_started_at = perf_counter()

    portfolio = state.portfolio

    # We can alway get the current value even if there are no trades
    current_value = portfolio.get_total_equity()

    strategy_start, strategy_end  = state.get_strategy_time_range()

    first_trade, last_trade = portfolio.get_first_and_last_executed_trade()

    first_trade_at = first_trade.executed_at if first_trade else None
    last_trade_at = last_trade.executed_at if last_trade else None

    if not now_:
        now_ = pd.Timestamp.utcnow().tz_localize(None)

    start_at = now_ - time_window
    if strategy_start and strategy_end:
        age = strategy_end - strategy_start
    else:
        age = None

    stats = state.stats

    profitability_90_days = None
    enough_data = False
    performance_chart_90_days = None
    returns_all_time = returns_annualised = None

    if len(stats.portfolio) > 0 and not legacy_workarounds:
        profitability = calculate_compounding_realised_trading_profitability(state)
        enough_data = len(profitability.index) > 1 and profitability.index[0] <= start_at
        if len(profitability) >= 2:  # TypeError: cannot do slice indexing on RangeIndex with these indexers [2023-09-08 13:42:01.749186] of type Timestamp
            profitability_time_windowed = profitability[start_at:]
            if len(profitability_time_windowed) > 0:
                profitability_daily = calculate_cumulative_daily_returns(state)
                # We do not generate entry for dates without trades so forward fill from the previous day
                profitability_daily = profitability_daily.ffill()
                profitability_90_days = profitability_daily.iloc[-1]
                performance_chart_90_days = export_time_series(profitability_daily)
                returns_all_time = profitability.iloc[-1]
            else:
                profitability_90_days = None
                performance_chart_90_days = None

    if age and returns_all_time:
        returns_annualised = returns_all_time * datetime.timedelta(days=365) / age

    key_metrics = {m.kind.value: m for m in calculate_key_metrics(state, backtested_state, required_history=key_metrics_backtest_cut_off)}

    logger.info("calculate_summary_statistics() finished, took %s seconds", perf_counter() - func_started_at)

    return StrategySummaryStatistics(
        first_trade_at=first_trade_at,
        last_trade_at=last_trade_at,
        enough_data=enough_data,
        current_value=current_value,
        profitability_90_days=profitability_90_days,
        performance_chart_90_days=performance_chart_90_days,
        key_metrics=key_metrics,
        launched_at=state.created_at,
        backtest_metrics_cut_off_period=key_metrics_backtest_cut_off,
        return_all_time=returns_all_time,
        return_annualised=returns_annualised,
    )
