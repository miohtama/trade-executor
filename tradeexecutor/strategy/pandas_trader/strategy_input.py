"""Strategy decision input.

- Input arguments for `decide_trade` functions
"""

import logging
from dataclasses import dataclass

import pandas as pd

from tradeexecutor.state.identifier import TradingPairIdentifier
from tradeexecutor.state.state import State
from tradeexecutor.state.types import USDollarPrice
from tradeexecutor.strategy.execution_context import ExecutionContext
from tradeexecutor.strategy.pandas_trader.indicator import IndicatorResultMap, IndicatorSet
from tradeexecutor.strategy.pandas_trader.position_manager import PositionManager
from tradeexecutor.strategy.parameters import StrategyParameters
from tradeexecutor.strategy.pricing_model import PricingModel
from tradeexecutor.strategy.trading_strategy_universe import TradingStrategyUniverse


logger = logging.getLogger(__name__)


class InvalidForMultipairStrategy(Exception):
    """Try to use single trading pair functions in a multipair strategy."""



@dataclass(slots=True)
class StrategyInputIndicators:
    """Indicator results for the strategy decision.

    Wraps the indicator results to a format that has good developer experience
    when accessed from `decide_trades()`. The default timestamp

    - Indicators are prepared in `create_indicators` function
    - The framework takes care of recalculating indicators when needed,
      for backtest and live access
    - For backtests, this class is instiated only once

    For simple strategies calling :py:meth:`get_indicator_value` should be only required here.
    """

    #: Trading universe
    #:
    #: - Perform additional pair lookups if needed
    #:
    strategy_universe: TradingStrategyUniverse

    #: The indicator definitions.
    #:
    #: Indicators set up in `create_indicators()`.
    #:
    available_indicators: IndicatorSet

    #: Raw cached indicator results or ones calculated in the memory
    #:
    indicator_results: IndicatorResultMap

    #: The current decision_cycle() timestamp.
    #:
    #: Stored here, so we do not need to pass it explicitly in API.
    #:
    timestamp: pd.Timestamp | None = None

    def __post_init__(self):
        assert type(self.indicator_results) == dict
        assert isinstance(self.available_indicators, IndicatorSet)
        assert isinstance(self.strategy_universe, TradingStrategyUniverse)

    def get_price(
        self,
        pair: TradingPairIdentifier | None = None,
        data_lag_tolerance: pd.Timedelta = pd.Timedelta(days=5),
    ) -> USDollarPrice:
        """Read the available close price of a trading pair.

        - Returns the latest available close price

        - **Does not** return the current price in the decision_cycle,
          because any decision must be made based on the previous price
        """
        assert self.timestamp, f"prepare_decision_cycle() not called - framework missing something somewhere"

        if pair is None:
            pair = self.strategy_universe.get_single_pair()
        assert isinstance(pair, TradingPairIdentifier)
        assert pair.internal_id, "pair.internal_id missing - bad unit test data?"

        ts = self.strategy_universe.data_universe.candles.get_prior_timestamp(self.timestamp)
        price, close_at = self.strategy_universe.data_universe.candles.get_single_value(
            asset_id=pair.internal_id,
            when=ts,
            data_lag_tolerance=data_lag_tolerance,
            asset_name=pair.get_ticker(),
        )
        return price

    def get_indicator_value(
        self,
        name: str,
        pair: TradingPairIdentifier | None = None
    ) -> float:
        """Read the available value of an indicator.

        - Returns the latest available indicator value

        - **Does not** return the current indicator in the decision_cycle,
          because any decision must be made based on the previous price

        :param name:
            Indicator name as defined in `create_indicators`.

        :param pair:
            Trading pair.

            Must be given if the working with a multipair strategy.

        :return:
            The latest available indicator value
        """
        assert self.timestamp, f"prepare_decision_cycle() not called"
        assert self.available_indicators.has_indicator(name), f"Indicator with name {name} not defined in create_indicators. Available indicators are: {self.available_indicators.get_label()}"

        if pair is None:
            pair = self.strategy_universe.get_single_pair()
        assert isinstance(pair, TradingPairIdentifier)
        assert pair.internal_id, "pair.internal_id missing - bad unit test data?"

        key = (pair, )
        return self

    def prepare_decision_cycle(self, cycle: int, timestamp: pd.Timestamp):
        logger.info("Strategy indicators moved to the cycle: %d: %s", cycle, timestamp)
        self.timestamp = timestamp



@dataclass
class StrategyInput:
    """Inputs for a trading decision.

    The data structure used to make trade decisions. Captures
    all values that need to go to a single trade, under different live and backtesting
    circumstances.

    - Inputs for `decide_trades` function

    - Enabled when `trading_strategy_engine_version = "0.5"` or higher
    """

    #: Strategy cycle number
    #:
    #: - Deterministic for a backtest
    #: - May be reset for live execution
    #:
    cycle: int

    #: Timestamp of this strategy cycle
    #:
    #: - Timestamp can/should only access earlier data and cannot peek into the future
    #: - Always in UTC, no timezone
    #:
    timestamp: pd.Timestamp

    #: The current state of a strategy
    #:
    #: - You can peek for open/closed positions
    #: - Use :py:meth:`get_position_manager` to access
    #:
    state: State

    #: The source trading universe for this strategy run
    strategy_universe: TradingStrategyUniverse

    #: Parameters used for this backtest or live run
    parameters: StrategyParameters

    #: All indicators that are precalculated with create_indicators()
    #:
    #: - Indicators calculated in `create_indicators` function
    #: - Cached in backtesting for fast reader
    #: - In livee trading recalculated for every cycle
    #:
    indicators: StrategyInputIndicators

    #: Asset pricing model.
    #:
    #: - Used to determine the position size and value of trades
    #: - Backtesting uses historical pricing whereas live trading will read any data directly on-chain
    #: - Access using :py:meth:`get_position_manager`
    #:
    pricing_model: PricingModel

    #: Information about whether this is live or backtest run.
    #:
    execution_context: ExecutionContext

    #: Diagnostics and debug data
    #:
    #: - Undefined format
    #: - Mostly used in internal testing and logging
    #: - Is mutated in-place, but don't rely on this to work for live strategies
    #:
    other_data: dict

    def get_position_manager(self) -> PositionManager:
        """Create a position manager instance to open/close trading positions in this decision cycle."""
        return PositionManager(
            self.timestamp,
            self.strategy_universe,
            self.state,
            self.pricing_model
        )

    def get_default_pair(self) -> TradingPairIdentifier:
        """Get the default trading pair for this stragegy.

        - Works only for single pair strateiges

        :raise InvalidForMultipairStrategy:
            If called for a multi pair strategy
        """
        if self.strategy_universe.get_pair_count() != 1:
            raise InvalidForMultipairStrategy("Strategy universe is multipair - get_default_pair() not available")
        return self.strategy_universe.get_single_pair()