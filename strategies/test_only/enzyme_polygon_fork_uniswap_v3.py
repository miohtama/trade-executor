"""Dummy strategy used in on Enzyme Polygon mainnet fork + Uniswap v3 end-to-end tests.

"""
import datetime
from typing import Dict, List

import pandas as pd
from tradingstrategy.chain import ChainId

from tradeexecutor.strategy.cycle import CycleDuration
from tradeexecutor.strategy.default_routing_options import TradeRouting
from tradeexecutor.strategy.execution_context import ExecutionContext
from tradeexecutor.strategy.pricing_model import PricingModel
from tradeexecutor.strategy.trading_strategy_universe import TradingStrategyUniverse, load_pair_data_for_single_exchange
from tradeexecutor.strategy.universe_model import UniverseOptions
from tradingstrategy.client import Client
from tradingstrategy.timebucket import TimeBucket
from tradingstrategy.universe import Universe

from tradeexecutor.state.state import State
from tradeexecutor.state.trade import TradeExecution
from tradeexecutor.strategy.strategy_type import StrategyType
from tradeexecutor.strategy.pandas_trader.position_manager import PositionManager
from tradeexecutor.strategy.reserve_currency import ReserveCurrency

trading_strategy_engine_version = "0.1"
trading_strategy_type = StrategyType.managed_positions
trade_routing = TradeRouting.uniswap_v3_usdc_poly
trading_strategy_cycle = CycleDuration.cycle_1s
reserve_currency = ReserveCurrency.usdc
candle_time_bucket = TimeBucket.d7
trading_pair = (ChainId.polygon, "uniswap-v3", "WMATIC", "USDC", 0.0005)


def decide_trades(
        timestamp: pd.Timestamp,
        universe: Universe,
        state: State,
        pricing_model: PricingModel,
        cycle_debug_data: Dict) -> List[TradeExecution]:

    # Create a position manager helper class that allows us easily to create
    # opening/closing trades for different positions
    position_manager = PositionManager(timestamp, universe, state, pricing_model, default_slippage_tolerance=0.02)

    pair = universe.pairs.get_single()

    assert pair.pair_id > 0

    cash = state.portfolio.get_cash()

    cycle_number = cycle_debug_data["cycle"]

    trades = []

    # For odd seconds buy, for even seconds sell
    if cycle_number % 2 == 0:
        # buy on even days
        if not position_manager.is_any_open():
            position_size = 0.10
            buy_amount = cash * position_size
            trades += position_manager.open_1x_long(pair, buy_amount)
    else:
        # sell on odd days
        if position_manager.is_any_open():
            trades += position_manager.close_all()

    return trades


def create_trading_universe(
        ts: datetime.datetime,
        client: Client,
        execution_context: ExecutionContext,
        universe_options: UniverseOptions,
):
    assert isinstance(client, Client), f"Looks like we are not running on the real data. Got: {client}"

    # Download live data from the oracle
    dataset = load_pair_data_for_single_exchange(
        client,
        time_bucket=candle_time_bucket,
        pair_tickers=[trading_pair],
        execution_context=execution_context,
        universe_options=universe_options,
    )

    # Convert loaded data to a trading pair universe
    universe = TradingStrategyUniverse.create_single_pair_universe(
        dataset,
        pair=trading_pair,
    )

    return universe