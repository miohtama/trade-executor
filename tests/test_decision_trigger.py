"""Integration testings for data availability trigger for live oracle."""
import datetime
import os

import pytest

from tradeexecutor.strategy.execution_context import ExecutionContext, ExecutionMode
from tradeexecutor.strategy.pandas_trader.decision_trigger import wait_for_universe_data_availability_jsonl, \
    validate_latest_candles
from tradeexecutor.strategy.trading_strategy_universe import TradingStrategyUniverse, load_all_data
from tradeexecutor.strategy.universe_model import UniverseOptions
from tradeexecutor.utils.timer import timed_task
from tradingstrategy.chain import ChainId
from tradingstrategy.timebucket import TimeBucket

pytestmark = pytest.mark.skipif(os.environ.get("TRADING_STRATEGY_API_KEY") is None, reason="Set TRADING_STRATEGY_API_KEY environment variable to run this test")



@pytest.fixture()
def execution_context(request) -> ExecutionContext:
    """Setup backtest execution context."""
    return ExecutionContext(mode=ExecutionMode.backtesting, timed_task_context_manager=timed_task)


@pytest.fixture()
def universe(execution_context, persistent_test_client) -> TradingStrategyUniverse:
    """Construct WETH-USDC """

    client = persistent_test_client

    # Time bucket for our candles
    candle_time_bucket = TimeBucket.d1

    # Which chain we are trading
    chain_id = ChainId.bsc

    # Which exchange we are trading on.
    exchange_slug = "pancakeswap-v2"

    # Which trading pair we are trading
    trading_pair = ("WBNB", "BUSD")

    # Load all datas we can get for our candle time bucket
    dataset = load_all_data(client, candle_time_bucket, execution_context, UniverseOptions())

    # Filter down to the single pair we are interested in
    universe = TradingStrategyUniverse.create_single_pair_universe(
        dataset,
        chain_id,
        exchange_slug,
        trading_pair[0],
        trading_pair[1],
    )

    return universe


def test_decision_trigger_ready_data(persistent_test_client, universe):
    """Test that we can immedidately trigger trades for old data.

    We do not need to wait.
    """
    client = persistent_test_client
    timestamp = datetime.datetime(2023, 1, 1)
    updated_universe_result = wait_for_universe_data_availability_jsonl(
        timestamp,
        client,
        universe,
    )

    assert updated_universe_result.ready_at <=  datetime.datetime.utcnow()
    assert updated_universe_result.poll_cycles == 1

    pair = updated_universe_result.updated_universe.universe.pairs.get_single()
    candles = updated_universe_result.updated_universe.universe.candles.get_candles_by_pair(pair.pair_id)

    validate_latest_candles(
        {pair},
        candles,
        timestamp
    )
