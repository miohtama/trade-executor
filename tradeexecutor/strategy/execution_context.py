"""Execution modes.

- Are we doing live trading or backtesting

- Any instrumentation like task duration tracing needed for the run
"""
import enum
from dataclasses import dataclass
from typing import Callable
from packaging import version

from tradeexecutor.strategy.engine_version import TradingStrategyEngineVersion
from tradeexecutor.utils.timer import timed_task


class ExecutionMode(enum.Enum):
    """Different execution modes the strategy engine can handle.

    Depending on how we are using the engine, we might enable and disable
    additional checks and features.

    - In unit testing execution mode we can skip
      all kind of delays when we need to wait a blockchain chain tip to stabilise

    - In backtesting execution mode we skip calculation of statistics
      between strategy decision cycles, as these statistics are discarted
      and calculations slows us down

    """

    #: We are live trading with real assets
    real_trading = "real_trading"

    #: We are live trading with mock assets
    #:
    #: TODO: This mode is not yet supported
    paper_trading = "paper_trading"

    #: We are backtesting
    #: When backtesting mode is selected, we can skip most of the statistical calculations that would otherwise be calculated during live-trade. 
    #: This offers great performance benefits for backtesting. 
    backtesting = "backtesting"

    #: We are doing data research.
    #:
    #: There is not going to be any trading,
    #: we are only interested in datsets.
    data_research = "data_research"

    #: We are loading and caching datasets before a backtesting session can begin.
    #: We call create_trading_universe() and assume :py:class:`tradingstrategy.client.Client`
    #: class is set to a such state it can display nice progress bar when loading
    #: data in a Jupyter notebook.
    data_preload = "data_preload"

    #: We are operating on real datasets like :py:data:`real_trading`
    #: but we do not want to purge caches.
    #:
    #: This mode is specially used to test some live trading features.
    #:
    unit_testing_trading = "unit_testing_trading"

    #: Simulated trading: Blockchain we are connected is not real.
    #:
    #: We are trading against a simulated step-by-step blockchain
    #: like EthereumTester. This allows us to control
    #: block production, but otherwise behave as
    #: live trading.
    #:
    #: In this mode, we are also not using any dataset loading features,
    #: but the trading universe and price feeds are typed in the test code.
    #:
    simulated_trading = "simulated_trading"

    #: Prefilight checks
    #:
    #: In this execution mode, we are invoked from the command line
    #: to check that all of our files and connections are intact.
    preflight_check = "preflight_check"

    #: One off diagnostic and scripts
    #:
    #: Used in the interactive :ref:`console.
    #: and debugging scripts.
    one_off = "one_off"

    def is_live_trading(self) -> bool:
        """Are we trading  real time?"""
        return self in (self.real_trading, self.paper_trading, self.unit_testing_trading, self.simulated_trading)

    def is_fresh_data_always_needed(self):
        """Should we purge caches for each trade cycle.

        This will force the redownload of data on each cycle.
        """
        return self in (self.real_trading, self.paper_trading, self.simulated_trading)

    def is_unit_testing(self) -> bool:
        """Are we executing unit tests."""
        return self in (self.unit_testing_trading, self.simulated_trading,)


@dataclass
class ExecutionContext:
    """Information about the strategy execution environment.

    This is passed to `create_trading_universe` and couple of other
    functions and they can determine and take action based
    the mode of strategy execution. For example,
    we may load pair and candle data differently in live trading.

    Example how to create for backtests:

    .. code-block:: python

        from tradeexecutor.strategy.execution_context import ExecutionContext, ExecutionMode

        execution_context = ExecutionContext(
            mode=ExecutionMode.backtesting,
        )

    """

    #: What is the current mode of the execution.
    mode: ExecutionMode

    #: Python context manager for timed tasks.
    #:
    #: Functions can use this context manager to add them to the tracing.
    #: Used for profiling the strategy code run-time performance.
    #:
    #: Set default to :py:func:`tradeexecutor.utils.timer.timed_task`.
    #: which logs task duration using logging.INFO level.
    #:
    timed_task_context_manager: Callable = timed_task

    #: What TS engine the strategy is using.
    #:
    #: `None` means 0.1.
    #:
    #: See :py:mod:`tradeexecutor.strategy.engine_version`.
    #:
    engine_version: TradingStrategyEngineVersion = None

    def __repr__(self):
        return f"<ExecutionContext {self.mode.name} v{self.engine_version}>"

    def is_version_greater_or_equal_than(self, major: int, minor: int, patch: int) -> bool:
        """Check that we are runing engine as the minimum required version."""
        running_version = self.engine_version or "0.1"
        required_version = f"{major}.{minor}.{patch}"
        return version.parse(running_version) >= version.parse(required_version)

    @property
    def live_trading(self) -> bool:
        """Are we doing live trading.

        :return:
            True if we doing live trading or paper trading.
             False if we are operating on backtesting data.
        """
        return self.mode in (ExecutionMode.real_trading, ExecutionMode.paper_trading, ExecutionMode.simulated_trading)


#: Shorthand for unit testing
unit_test_execution_context = ExecutionContext(ExecutionMode.unit_testing_trading)

#: Shorthand for unit testing
ExecutionContext.unit_test = unit_test_execution_context
