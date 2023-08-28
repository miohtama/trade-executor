"""Core portfolio statistics calculations."""

import logging
import datetime
from dataclasses import dataclass, field
from typing import Dict, List
from tradeexecutor.analysis.trade_analyser import build_trade_analysis

from tradeexecutor.state.portfolio import Portfolio
from tradeexecutor.state.position import TradingPosition
from tradeexecutor.state.statistics import Statistics, PortfolioStatistics, PositionStatistics, FinalPositionStatistics
from tradeexecutor.strategy.execution_context import ExecutionMode

logger = logging.getLogger(__name__)


@dataclass
class TimestampedStatistics:
    """Statistics calculated for the portfolio on each stats cycle.

    Can be collected at

    - And of the strategy cycle

    - During the position revaluation
    """

    #: Statistics for the whole portfolio
    #:
    #: Inc. quant finance metrics
    #:
    portfolio: PortfolioStatistics

    #: Statistics for each individual position that was open at the time of colleciton
    positions: Dict[int, PositionStatistics] = field(default_factory=dict)

    #: Strategy cycle timestamp
    #:
    #: Not available if the statistics where calculated
    #: out of rebalance
    #:
    strategy_cycle_at: datetime.datetime | None = None


def calculate_position_statistics(clock: datetime.datetime, position: TradingPosition) -> PositionStatistics:
    """Generate a historical record of the current position state."""
    # first_trade = position.get_first_trade()

    value = position.get_value()

    first_trade = position.get_first_trade()
    if position.is_open() and first_trade.is_success():
        # Normally opened positions should always have some value
        assert value > 0, f"Position {position} reported value {value}. Last token price: {position.last_token_price}. Last reserve price: {position.last_reserve_price}"

    stats = PositionStatistics(
        calculated_at=clock,
        last_valuation_at=position.last_pricing_at,
        profitability=position.get_total_profit_percent(),
        profit_usd=position.get_total_profit_usd(),
        quantity=float(position.get_quantity_old()),
        value=value,
    )

    return stats


def calculate_closed_position_statistics(
        clock: datetime.datetime,
        position: TradingPosition,
        position_stats: List[PositionStatistics]) -> FinalPositionStatistics:
    """Calculate the statistics for closed positions."""

    # This position should have at least some relevant value calculated
    # for it
    first_trade = position.get_first_trade()

    # TODO: Is also recorded at position.portfolio_value_at_open
    # after v311
    value_at_open = position_stats[0].value

    if first_trade.is_success():
        if value_at_open == 0:
            # TODO:
            # We should really abort here with an exception
            # but we have some legacy data that prevents this
            logger.warning(f"PositionStatistics lacked value at open for {position}.\n"
                           f"Stats:\n"
                           f"{position_stats}")

    value_at_max = max([(p.value or 0) for p in position_stats])
    stats = FinalPositionStatistics(
        calculated_at=clock,
        trade_count=len(position.trades),
        value_at_open=value_at_open,
        value_at_max=value_at_max,
    )
    return stats


def calculate_statistics(
        clock: datetime.datetime,
        portfolio: Portfolio,
        execution_mode: ExecutionMode,
        strategy_cycle_at: datetime.datetime | None = None,
) -> TimestampedStatistics:
    """Calculate statistics for a portfolio."""

    first_trade, last_trade = portfolio.get_first_and_last_executed_trade()

    # comprehensenhive statistics after each trade are not needed for backtesting
    if (execution_mode != ExecutionMode.backtesting):

        trade_analysis = build_trade_analysis(portfolio)

        pf_stats = PortfolioStatistics(
            calculated_at=clock,
            total_equity=portfolio.get_total_equity(),
            free_cash=float(portfolio.get_cash()),
            open_position_count=len(portfolio.open_positions),
            open_position_equity=portfolio.get_position_equity_and_loan_nav(),
            frozen_position_equity=portfolio.get_frozen_position_equity(),
            frozen_position_count=len(portfolio.frozen_positions),
            closed_position_count=len(portfolio.closed_positions),
            unrealised_profit_usd=portfolio.get_unrealised_profit_usd(),
            realised_profit_usd=portfolio.get_closed_profit_usd(),
            first_trade_at=first_trade and first_trade.executed_at or None,
            last_trade_at=last_trade and last_trade.executed_at or None,
            summary=trade_analysis.calculate_summary_statistics(),
        )
    else:
        pf_stats = PortfolioStatistics(
            calculated_at=clock,
            total_equity=portfolio.get_total_equity(),
        )

    stats = TimestampedStatistics(
        portfolio=pf_stats,
        strategy_cycle_at=strategy_cycle_at,
    )

    for position in portfolio.open_positions.values():
        stats.positions[position.position_id] = calculate_position_statistics(clock, position)

    for position in portfolio.frozen_positions.values():
        stats.positions[position.position_id] = calculate_position_statistics(clock, position)

    return stats


def update_statistics(
        clock: datetime.datetime,
        stats: Statistics,
        portfolio: Portfolio,
        execution_mode: ExecutionMode,
        strategy_cycle_at: datetime.datetime | None = None,
):
    """Update statistics in a portfolio.

    TODO: Rename to `update_trading_statistics()`.

    Can be collected at

    - And of the strategy cycle

    - During the position revaluation

    - Other ad-hoc timestamp

    .. note ::

        You *must* make sure this function is called any time a position has been opened
        or closed, or relevant statistics may get corrupted due to holes in them.

    :param clock:
        Real-time clock of the update.

        Always available.

    :param strategy_cycle_at:
        The strategy cycle timestamp.

        Only available when `update_statistics()` is run
        at the end of live trading cycle.
    """

    logger.info("update_statistics(), real-time clock at %s, strategy cycle at %s",
                clock,
                strategy_cycle_at
                )

    new_stats = calculate_statistics(clock, portfolio, execution_mode)
    stats.portfolio.append(new_stats.portfolio)
    for position_id, position_stats in new_stats.positions.items():
        stats.add_positions_stats(position_id, position_stats)

    # Check if we have missing closed position statistics
    # by comparing which closed ids are missing from the stats list
    closed_position_stat_ids = set(stats.closed_positions.keys())
    all_closed_position_ids = set(portfolio.closed_positions.keys())

    missing_closed_position_stats = all_closed_position_ids - closed_position_stat_ids
    for position_id in missing_closed_position_stats:
        position = portfolio.closed_positions[position_id]
        # Calculate the closing value for the profitability
        position_stats = calculate_position_statistics(clock, position)
        stats.add_positions_stats(position_id, position_stats)
        # Calculate stats that are only available for closed positions
        stats.closed_positions[position_id] = calculate_closed_position_statistics(clock, position, stats.positions[position_id])

    logger.info("update_statistics() complete")
