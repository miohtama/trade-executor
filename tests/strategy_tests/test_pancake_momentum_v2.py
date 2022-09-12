"""Run Pancake momentum v2 strategy some backtesting cycles to see the code does not break."""
import datetime
import logging
import os

import pandas as pd

from pathlib import Path

import pytest
from tradeexecutor.backtest.backtest_runner import run_backtest, setup_backtest
from tradeexecutor.cli.log import setup_pytest_logging
from tradeexecutor.strategy.cycle import CycleDuration
from tradingstrategy.timebucket import TimeBucket


# https://docs.pytest.org/en/latest/how-to/skipping.html#skip-all-test-functions-of-a-class-or-module
pytestmark = pytest.mark.skipif(os.environ.get("TRADING_STRATEGY_API_KEY") is None, reason="Set TRADING_STRATEGY_API_KEY environment variable to run this test")


@pytest.fixture(scope="module")
def logger(request):
    """Setup test logger."""
    return setup_pytest_logging(request, mute_requests=False)


@pytest.fixture()
def strategy_path() -> Path:
    """Where do we load our strategy file."""
    return Path(os.path.join(os.path.dirname(__file__), "..", "..", "strategies", "pancake-momentum-v2.py"))


def test_pancake_momentum_v2(
    strategy_path,
    logger: logging.Logger,
    persistent_test_client,
    ):
    """Check the strategy does not crash."""

    client = persistent_test_client

    module_overwrites = {
        "momentum_lookback_period": pd.Timedelta(days=7),
        "candle_time_bucket": TimeBucket.d7,
        "trading_strategy_cycle": CycleDuration.cycle_7d,
    }

    # Run backtest over 6 months, daily
    setup = setup_backtest(
        strategy_path,
        start_at=datetime.datetime(2021, 6, 1),
        end_at=datetime.datetime(2021, 7, 1),
        initial_deposit=10_000,
        module_overwrites=module_overwrites,
    )

    state, universe, debug_dump = run_backtest(setup, client)

    # We have done this many cycles
    assert len(debug_dump) == 5

    # We have done some trades
    assert len(list(state.portfolio.get_all_trades())) >= 40

