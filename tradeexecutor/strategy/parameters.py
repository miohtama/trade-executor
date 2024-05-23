"""Strategy input parameters handlding.

- Handle input parameters in single-run and grid search cases
"""
import datetime
from typing import Tuple, Iterable, TypedDict

from web3.datastructures import MutableAttributeDict

from tradeexecutor.state.types import USDollarAmount
from tradeexecutor.strategy.cycle import CycleDuration
from tradeexecutor.strategy.default_routing_options import TradeRouting


class StrategyParametersMissing(Exception):
    """Strategy parameters are not well defined."""



class CoreStrategyParameters(TypedDict):
    """Describe strategy parameters that are always available.

    """

    cycle_duration: CycleDuration

    #: Current strategy decision cycle
    cycle: int

    #: Trade routing model.
    #:
    #: Applies to live strategies only
    #:
    routing: TradeRouting

    #: US dollars at the start of the backtesting
    initial_cash: USDollarAmount | None

    backtest_start: datetime.datetime | None

    backtest_end: datetime.datetime | None

    #: Live trading needed history
    #:
    #: How much data load for each strategy cycle in live trading
    #:
    required_history_period: datetime.timedelta | None


class StrategyParameters(MutableAttributeDict):
    """Strategy parameters.

    These parameters may present

    - Individual constant parameters for the strategy, like indicators threshold levels:
      `rsi_low`, `rsi_high`

    - Parameters about the backtesting itself: `backtest_start`, `backtest_end`

    - Parameters about strategy execution: `cycle_duration`.

    - See :py:class:`CoreStrategyParameters` for the always present parameters.
      Due to Python limitations these cannot be automatically type hinted.

    The parameters are presented as attributed dict and
    are accessible using both dotted attribe access and dict access:

    .. code-block:: python

        value = parameters.rsi_low
        value = parameters["rsi_low"]  # Are equal

    Example parameter definition:

    .. code-block:: python

        from tradeexecutor.strategy.cycle import CycleDuration
        from tradeexecutor.backtest.backtest_runner import run_backtest_inline

        class Parameters:
            cycle_duration = CycleDuration.cycle_1d
            rsi_bars = 5  # 5 days = 15 8 hour bars
            eth_btc_rsi_bars = 20  # The length of ETH/BTC RSI
            rsi_high = 77 # RSI trigger threshold for decision making
            rsi_low = 60  # RSI trigger threshold for decision making
            allocation = 0.85 # Allocate 90% of cash to each position
            lookback_candles = 140
            minimum_rebalance_trade_threshold = 500.00  # Don't do trades that would have less than 500 USD value change
            initial_cash = 10_000 # Start with 10k USD
            trailing_stop_loss = 0.875
            shift = 0

        state, universe, debug_dump = run_backtest_inline(
            name="RSI multipair",
            engine_version="0.4",
            decide_trades=decide_trades,
            client=client,
            universe=strategy_universe,
            parameters=Parameters,
            strategy_logging=False,
        )

        trade_count = len(list(state.portfolio.get_all_trades()))
        print(f"Backtesting completed, backtested strategy made {trade_count} trades")

    You can use `StrategyParameters` with inline backtest as a dict:

    .. code-block:: python

        def decide_trades(
                timestamp: pd.Timestamp,
                parameters: StrategyParameters,
                strategy_universe: TradingStrategyUniverse,
                state: State,
                pricing_model: PricingModel) -> List[TradeExecution]:
            assert parameters.test_val == 111
            # ...

        parameters = StrategyParameters({
            "test_val": 111,
        })

        # Run the test
        state, universe, debug_dump = run_backtest_inline(
            parameters=parameters,
        )

    When dealing with grid search input, every parameter is assumed to be a list
    of potential grid search combination values. `decide_trades` function
    gets called with every single combination of the list.

    .. code-block:: python

        from tradeexecutor.strategy.cycle import CycleDuration
        from pathlib import Path
        from tradeexecutor.backtest.grid_search import prepare_grid_combinations

        # This is the path where we keep the result files around
        storage_folder = Path("/tmp/v5-grid-search.ipynb")

        class StrategyParameters:
            cycle_duration = CycleDuration.cycle_1d
            rsi_days = [5, 6, 9, 8, 20]  # The length of RSI indicator
            eth_btc_rsi_days = 60  # The length of ETH/BTC RSI
            rsi_high = [60, 70, 80] # RSI trigger threshold for decision making
            rsi_low = [30, 40, 50]  # RSI trigger threshold for decision making
            allocation = 0.9 # Allocate 90% of cash to each position
            lookback_candles = 120
            minimum_rebalance_trade_threshold = 500.00  # Don't do trades that would have less than 500 USD value change

        combinations = prepare_grid_combinations(StrategyParameters, storage_folder)
        print(f"We prepared {len(combinations)} grid search combinations")

        # Then pass the combinations to a grid search
        from tradeexecutor.backtest.grid_search import perform_grid_search

        grid_search_results = perform_grid_search(
            decide_trades,
            strategy_universe,
            combinations,
            max_workers=8,
            trading_strategy_engine_version="0.4",
            multiprocess=True,
        )
    """

    def __getattribute__(self, name):
        # Only implemented to make type hinting to stop complaining
        # https://stackoverflow.com/questions/78210800/type-hinting-python-class-with-dynamic-any-attribute/78210867#78210867
        try:
            return object.__getattribute__(self, name)
        except AttributeError:
            all_params = ", ".join(key for key, val in self.iterate_parameters())
            raise AttributeError(f"Strategy parameters lacks parameter: {name}\nWe have: {all_params}")

    def iterate_parameters(self) -> Iterable[Tuple[str, any]]:
        """Iterate over parameter definitions."""
        return self.items()

    def is_single_run(self) -> bool:
        """Are these parameters for a single backtest run.

        As opposite to the grid search.
        """
        for key, value in self.iterate_parameters():
            if type(value) == list:
                return True

    def is_grid_search(self) -> bool:
        """Are these parameters for a grid search.

        Search over multiple values.
        """
        return not self.is_single_run()

    def validate_backtest(self):
        """Do a basic validation for backtesting parameters."""
        if "initial_cash" not in self:
            raise StrategyParametersMissing("initial_cash parameter missing")

        if "cycle_duration" not in self:
            raise StrategyParametersMissing("cycle_duration parameter missing")

    @staticmethod
    def from_class(c: type, grid_search=False) -> "StrategyParameters":
        """Create parameter dict out from a class object.

        - Convert inlined class-style strategy parameter input to dictionary

        :param grid_search:
            Make grid-search style parameters.

            Every scalar input is converted to a single item list if set.
        """
        # https://stackoverflow.com/a/1939279/315168
        keys = [attr for attr in dir(c) if not callable(getattr(c, attr)) and not attr.startswith("__")]
        params = {k: getattr(c, k) for k in keys}

        if not grid_search:
            return StrategyParameters(params)

        # Convert single variable declarations to a list
        output = {}
        for k, v in params.items():
            if not type(v) in (list, tuple):
                v = [v]
            output[k] = v

        return StrategyParameters(output)
