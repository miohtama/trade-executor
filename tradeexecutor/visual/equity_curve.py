"""Equity curve based statistics and visualisations.

For more information see the narrative documentation on :ref:`profitability`.
"""
import datetime
import warnings
from typing import List

import pandas as pd
from matplotlib.figure import Figure

from tradeexecutor.state.state import State
from tradeexecutor.state.statistics import Statistics, PortfolioStatistics
from tradeexecutor.state.position import TradingPosition
from tradeexecutor.state.types import USDollarAmount


def calculate_equity_curve(
    state: State,
    attribute_name="total_equity",
    fill_time_gaps=False,
) -> pd.Series:
    """Calculate equity curve for the portfolio.

    Translate the portfolio internal :py:attr:`Statistics.portfolio`
    to :py:class:`pd.Series` that allows easy equity curve calculations.

    This reads :py:class:`tradeexecutor.state.stats.PortfolioStatistics`

    :param attribute_name:
        Calculate equity curve based on this attribute of :py:class:`

    :param fill_time_gaps:
        Insert a faux book keeping entries at start and end.

        If not set, only renders the chart when there was some activate
        deposits and ignores non-activity gaps at start and end.

        See :py:meth:`tradeexecutor.state.state.State.get_strategy_time_range`.

    :return:
        Pandas series (timestamp, equity value).

        Index is DatetimeIndex.

        Empty series is returned if there is no data.

        We ensure only one entry per timestamp through
        filtering out duplicate indices.

    """

    stats: Statistics = state.stats

    portfolio_stats: List[PortfolioStatistics] = stats.portfolio

    data = [(s.calculated_at, getattr(s, attribute_name)) for s in portfolio_stats]

    if len(data) == 0:
        return pd.Series([], index=pd.to_datetime([]), dtype='float64')

    if fill_time_gaps:
        start, end = state.get_strategy_time_range()
        end_val = data[-1][1]

        if data[0][0] != start:
            data = [(start, 0)] + data

        if end != end_val:
            data.append((end, end_val))

    # https://stackoverflow.com/a/66772284/315168
    df = pd.DataFrame(data).set_index(0)[1]

    # Remove duplicates
    #
    # Happens in unit tests as we get a calculate event from deposit
    # and recalculatin at the same timestam

    # ipdb> curve
    # 0
    # 2021-06-01 00:00:00.000000        0.000000
    # 2023-10-18 10:04:05.834542    10000.000000
    # 2021-06-01 00:00:00.000000    10000.000000

    # https://stackoverflow.com/a/34297689/315168
    df = df[~df.index.duplicated(keep='last')]
    return df


def calculate_returns(equity_curve: pd.Series) -> pd.Series:
    """Calculate returns of an equity curve.

    Calculate % change for each time point we have on the equity curve.

    See also https://quantdare.com/calculate-monthly-returns-with-pandas/

    :param equity_curve:
        The equity curve.

        Created with :py:func:`calculate_equity_curve`

    :return:
        Series of returns.

        This is % change over the previous entry in the time series.


    """
    return equity_curve.pct_change().fillna(0.0)


def generate_buy_and_hold_returns(
    buy_and_hold_price_series: pd.Series,
):
    """Create a benchmark series based on price action.

    - Create a returns series that can be used as a benchmark in :py:func:`tradeexecutor.analysis.advanced_metrics.visualise_advanced_metrics`

    Example:

    .. code-block:: python

        eth_index = strategy_universe.data_universe.candles.get_candles_by_pair(eth_pair)["close"]
        benchmark_returns = generate_buy_and_hold_returns(eth_index)

    """
    assert isinstance(buy_and_hold_price_series, pd.Series)
    returns = calculate_returns(buy_and_hold_price_series)
    return returns


def calculate_cumulative_return(returns: pd.Series) -> pd.Series:
    """Calculate cumulative returns for the equity curve.

    See :term:`Cumulative return`.

    :param returns:
        The returns curve of a portfolio.

        See :py:func:`calculate_returns`.

    :return:
        Pandas DataFrame representing cumulative returns.
    """

    raise NotImplementedError()

    # Taken from qstrader.statistics.performance
    #return np.exp(np.log(1 + returns).cumsum())[-1] - 1


def calculate_aggregate_returns(equity_curve: pd.Series, freq: str | pd.DateOffset = "BM") -> pd.Series:
    """Calculate strategy aggregatd results over different timespans.

    Good to calculate

    - Monthly returns

    - Quaterly returns

    See :term:`Aggregate return` for more information what this metric represents.

    .. note ::

        There are multiple ways to calculate aggregated returns and they give a bit different results.
        See this article for details: https://quantdare.com/calculate-monthly-returns-with-pandas/

    .. note ::

        The current simplicist method does not calculate returns for the first and last period.

    :param equity_curve:
        The equity curve of the portfolio.

        See :py:func:`calculate_equity_curve`

    :param freq:

        Pandas frequency string.

        The default value is "month-end frequency".

        For valid values see https://stackoverflow.com/a/35339226/315168

    :return:
        Monthly returns for each month.

        The array is keyed by the end date of the period e.g. July is `2021-07-31`.

        The first month can be incomplete, so its value is `NaN`.
    """
    assert isinstance(equity_curve.index, pd.DatetimeIndex), f"Got {equity_curve.index}"

    equity_curve.sort_index(inplace=True)
    
    # Each equity curve sample is the last day of the period
    # https://stackoverflow.com/a/14039589/315168
    sampled = equity_curve.asfreq(freq, method='ffill')
    return sampled.pct_change()


def calculate_daily_returns(state: State, freq: pd.DateOffset | str= "D") -> (pd.Series | None):
    """Calculate daily returns of a backtested results.

    Used for advanced statistics.

    .. warning::

        Uses equity curve to calculate profits. This does not correctly
        handle deposit/redemptions. Use in backtesting only.

    :param freq:
        Frequency of the binned data.

    :returns:
        If valid state provided, returns are returned as calendar day (D) frequency, else None"""
    
    equity_curve = calculate_equity_curve(state)
    returns = calculate_aggregate_returns(equity_curve, freq=freq)
    return returns


def visualise_equity_curve(
        returns: pd.Series,
        title="Equity curve",
        line_width=1.5,
) -> Figure:
    """Draw equity curve, drawdown and daily returns using quantstats.

    `See Quantstats README for more details <https://github.com/ranaroussi/quantstats>`__.

    Example:

    .. code-block:: python

        from tradeexecutor.visual.equity_curve import calculate_equity_curve, calculate_returns
        from tradeexecutor.visual.equity_curve import visualise_equity_curve

        curve = calculate_equity_curve(state)
        returns = calculate_returns(curve)
        fig = visualise_equity_curve(returns)
        display(fig)

    :return:
        Matplotlit figure

    """
    import quantstats as qs  # Optional dependency
    fig = qs.plots.snapshot(
        returns,
        title=title,
        lw=line_width,
        show=False)
    return fig


def visualise_returns_over_time(
        returns: pd.Series,
) -> Figure:
    """Draw a grid of returns over time.

    - Currently only monthly breakdown supported

    - `See Quantstats README for more details <https://github.com/ranaroussi/quantstats>`__.

    Example:

    .. code-block:: python

        from tradeexecutor.visual.equity_curve import calculate_equity_curve, calculate_returns
        from tradeexecutor.visual.equity_curve import visualise_returns_over_time

        curve = calculate_equity_curve(state)
        returns = calculate_returns(curve)
        fig = visualise_equity_performance(returns)
        display(fig)

    :return:
        Matplotlit figure

    """

    # /Users/moo/Library/Caches/pypoetry/virtualenvs/tradingview-defi-strategy-XB2Vkmi1-py3.10/lib/python3.10/site-packages/quantstats/stats.py:968: FutureWarning:
    #
    # In a future version of pandas all arguments of DataFrame.pivot will be keyword-only.

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore")
        import quantstats as qs  # Optional dependency
        fig = qs.plots.monthly_returns(
            returns,
            show=False)
        return fig


def visualise_returns_distribution(
        returns: pd.Series,
) -> Figure:
    """Breakdown the best day/month/yearly returns

    - `See Quantstats README for more details <https://github.com/ranaroussi/quantstats>`__.

    Example:

    .. code-block:: python

        from tradeexecutor.visual.equity_curve import calculate_equity_curve, calculate_returns
        from tradeexecutor.visual.equity_curve import visualise_returns_distribution

        curve = calculate_equity_curve(state)
        returns = calculate_returns(curve)
        fig = visualise_returns_distribution(returns)
        display(fig)

    :return:
        Matplotlit figure

    """
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore")
        import quantstats as qs  # Optional dependency
        fig = qs.plots.distribution(
            returns,
            show=False)
        return fig


def calculate_investment_flow(
    state: State,
) -> pd.Series:
    """Calculate deposit/redemption i nflows/outflows of a strategy.

    See :ref:`profitability` for more information.

    :return:
        Pandas series (DatetimeIndex by the timestamp when the strategy treasury included the flow event, usd deposits/redemption amount)
    """

    treasury = state.sync.treasury
    balance_updates = treasury.balance_update_refs
    index = [e.strategy_cycle_included_at for e in balance_updates]
    values = [e.usd_value for e in balance_updates]

    if len(index) == 0:
        return pd.Series([], index=pd.to_datetime([]), dtype='float64')

    return pd.Series(values, index)


def calculate_realised_profitability(
    state: State,
) -> pd.Series:
    """Calculate realised profitability of closed trading positions.

    This function returns the :term:`profitability` of individually
    closed trading positions.

    See :ref:`profitability` for more information.

    :return:
        Pandas series (DatetimeIndex, % profit).

        Empty series if there are no trades.
    """
    data = [(p.closed_at, p.get_realised_profit_percent()) for p in state.portfolio.closed_positions.values() if p.is_closed()]

    if len(data) == 0:
        return pd.Series(dtype='float64')

    # https://stackoverflow.com/a/66772284/315168
    return pd.DataFrame(data).set_index(0)[1]


def calculate_size_relative_realised_trading_returns(
    state: State,
) -> pd.Series:
    """Calculate realised profitability of closed trading positions relative to the portfolio size..

    This function returns the :term:`profitability` of individually
    closed trading positions.

    See :ref:`profitability` for more information.

    :return:
        Pandas series (DatetimeIndex, % profit).

        Empty series if there are no trades.
    """
    positions = [p for p in state.portfolio.closed_positions.values() if p.is_closed()]
    return _calculate_size_relative_realised_trading_returns(positions)

def _calculate_size_relative_realised_trading_returns(positions: list[TradingPosition]):
    data = [(p.closed_at, p.get_size_relative_realised_profit_percent()) for p in positions]

    if len(data) == 0:
        return pd.Series(dtype='float64')

    # https://stackoverflow.com/a/66772284/315168
    return pd.DataFrame(data).set_index(0)[1]


def calculate_compounding_realised_trading_profitability(
    state: State,
    fill_time_gaps=True,
) -> pd.Series:
    """Calculate realised profitability of closed trading positions, with the compounding effect.

    Assume the profits from the previous PnL are used in the next one.

    This function returns the :term:`profitability` of individually
    closed trading positions, relative to the portfolio total equity.

    - See :py:func:`calculate_realised_profitability` for more information

    - See :ref:`profitability` for more information.

    :param fill_time_gaps:
        Insert a faux book keeping entries at star tand end.

        There chart ends at the last profitable trade.
        However, we want to render the chart all the way up to the current date.
        If `True` then insert a booking keeping entry to the last strategy timestamp
        (`State.last_updated_at),
        working around various issues when dealing with this data at the frontend.

        Any fixes are not applied to empty arrays.

    :return:
        Pandas series (DatetimeIndex, cumulative % profit).

        Cumulative profit is 0% if there is no market action.
        Cumulative profit is 1 (100%) if the equity has doubled during the period.

        The last value of the series is the total trading profitability
        of the strategy over its lifetime.
    """
    started_at, last_ts = _get_strategy_time_range(state, fill_time_gaps)
    positions = [p for p in state.portfolio.closed_positions.values() if p.is_closed()]
    return _calculate_compounding_realised_trading_profitability(positions, fill_time_gaps, started_at, last_ts)


def _calculate_compounding_realised_trading_profitability(
    positions: list[TradingPosition], 
    fill_time_gaps: bool,
    started_at: pd.Timestamp = None,
    last_ts: pd.Timestamp = None,
):
    realised_profitability = _calculate_size_relative_realised_trading_returns(positions)
    # https://stackoverflow.com/a/42672553/315168
    compounded = realised_profitability.add(1).cumprod().sub(1)

    if fill_time_gaps and len(compounded) > 0:

        assert started_at
        assert last_ts
        last_value = compounded.iloc[-1]

        # Strategy always starts at zero
        compounded[started_at] = 0

        # Fill fromt he last sample to current
        if last_ts and len(compounded) > 0 and last_ts > compounded.index[-1]:
            compounded[last_ts] = last_value

        # Because we insert new entries, we need to resort the array
        compounded = compounded.sort_index()

    return compounded


def calculate_long_compounding_realised_trading_profitability(state, fill_time_gaps=True):
    started_at, last_ts = _get_strategy_time_range(state, fill_time_gaps)
    positions = [p for p in state.portfolio.closed_positions.values() if (p.is_closed() and p.is_long())]
    return _calculate_compounding_realised_trading_profitability(positions, fill_time_gaps, started_at, last_ts)


def calculate_short_compounding_realised_trading_profitability(state, fill_time_gaps=True):
    started_at, last_ts = _get_strategy_time_range(state, fill_time_gaps)
    positions = [p for p in state.portfolio.closed_positions.values() if (p.is_closed() and p.is_short())]
    return _calculate_compounding_realised_trading_profitability(positions, fill_time_gaps, started_at, last_ts)
  

def _get_strategy_time_range(state, fill_time_gaps):
    started_at, last_ts = None, None
    if fill_time_gaps:
        started_at, last_ts = state.get_strategy_time_range()
    if not last_ts:
        last_ts = datetime.datetime.utcnow()
    return started_at,last_ts
  

def calculate_deposit_adjusted_returns(
    state: State,
    freq: pd.DateOffset = pd.offsets.Day(),
) -> pd.Series:
    """Calculate daily/monthly/returns on capital.
z
    See :ref:`profitability` for more information

    - This is `Total equity - net deposits`

    - This result is compounding

    - The result is resampled to a timeframe

    :param freq:
        Which sampling frequency we use for the resulting series.

        By default resample the results for daily timeframe.

    :return:
        Pandas series (DatetimeIndex by the the start timestamp fo the frequency, USD amount)
    """
    equity = calculate_equity_curve(state)
    flow = calculate_investment_flow(state)

    # https://pandas.pydata.org/pandas-docs/stable/user_guide/timeseries.html#resampling
    equity_resampled = equity.resample(freq)
    equity_delta = equity_resampled.last() - equity_resampled.first()

    flow_delta = flow.resample(freq).sum()

    return equity_delta - flow_delta


def calculate_non_cumulative_daily_returns(state: State, freq_base: pd.offsets.DateOffset | None = pd.offsets.Day()) -> pd.Series:
    """Calculates the the non cumulative daily returns for the strategy over time. 

    - Accounts for multiple positions in the same day
    - If no positions/trades are made on a day, it will be filled with 0

    .. note:: 
        Forward fill cannot be used with this method since the stat for each day represents the realised profit for that day only.
        So we fill na values with 0 

    :param state: Strategy state
    :param freq_base: Time frequency to resample to
    :return: Pandas series
    """
    returns = calculate_size_relative_realised_trading_returns(state)
    non_cumulative_daily_returns = returns.add(1).resample(freq_base).prod().sub(1).fillna(0)
    return non_cumulative_daily_returns

def calculate_cumulative_daily_returns(state: State, freq_base: pd.offsets.DateOffset | None = pd.offsets.Day()) -> pd.Series:
    """Calculates the cumulative daily returns for the strategy over time

    - Accounts for multiple positions in the same day
    - If no positions/trades are made on a day, that day will use latest non-zero profit value (forward fill)

    :param state: Strategy state
    :param freq_base: Time frequency to resample to
    :return: Pandas series
    """
    returns = calculate_compounding_realised_trading_profitability(state)
    cumulative_daily_returns = returns.add(1).resample(freq_base).prod().sub(1).ffill()
    return cumulative_daily_returns