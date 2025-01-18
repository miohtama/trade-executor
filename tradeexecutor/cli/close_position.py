"""Closing all positions externally.

Code to clean up positions or forcing a shutdown.
"""
import logging
import datetime

from tabulate import tabulate
from web3 import Web3

from tradeexecutor.analysis.position import display_positions
from tradeexecutor.ethereum.enzyme.vault import EnzymeVaultSyncModel
from tradeexecutor.strategy.sync_model import SyncModel
from tradeexecutor.strategy.valuation import ValuationModel
from tradeexecutor.strategy.valuation_update import update_position_valuations
from tradingstrategy.types import Percent
from tradeexecutor.state.state import State
from tradeexecutor.strategy.execution_model import ExecutionModel
from tradeexecutor.strategy.pandas_trader.position_manager import PositionManager
from tradeexecutor.strategy.pricing_model import PricingModel
from tradeexecutor.strategy.routing import RoutingModel, RoutingState
from tradeexecutor.strategy.trading_strategy_universe import TradingStrategyUniverse


logger = logging.getLogger(__name__)


class CloseAllAborted(Exception):
    """Interactively chosen to cancel"""


def close_single_or_all_positions(
    web3: Web3,
    execution_model: ExecutionModel,
    pricing_model: PricingModel,
    sync_model: SyncModel,
    state: State,
    universe: TradingStrategyUniverse,
    routing_model: RoutingModel,
    routing_state: RoutingState,
    valuation_model: ValuationModel,
    slippage_tolerance: Percent,
    interactive=True,
    position_id: int | None = None,
    unit_testing=False,
):
    """Close single/all positions.

    - CLI entry point

    - Sync reserves before starting

    - Close any open positions

    - Display trade execution and position report afterwards
    """

    assert isinstance(sync_model, SyncModel)
    assert isinstance(universe, TradingStrategyUniverse)
    assert isinstance(valuation_model, ValuationModel)

    if position_id is not None:
        assert type(position_id) is int, f"Got: {position_id} {type(position_id)}"
        assert position_id >= 0

    ts = datetime.datetime.utcnow()

    # Sync nonce for the hot wallet
    execution_model.initialize()

    logger.info("Sync model is %s", sync_model)
    logger.info("Trading university reserve asset is %s", universe.get_reserve_asset())

    # Use unit_testing flag so this code path is easier to check
    if sync_model.has_async_deposits() or unit_testing:
        logger.info("Vault must be revalued before proceeding, using: %s", sync_model.__class__.__name__)
        update_position_valuations(
            timestamp=ts,
            state=state,
            universe=universe,
            execution_context=execution_model.execution_context,
            routing_state=routing_state,
            valuation_model=valuation_model,
            long_short_metrics_latest=None,
        )

    # Sync any incoming stablecoin transfers
    # that have not been synced yet
    balance_updates = sync_model.sync_treasury(
        ts,
        state,
        list(universe.reserve_assets),
        post_valuation=True,
    )

    logger.info("We received balance update events: %s", balance_updates)

    # Velvet capital code path
    if sync_model.has_position_sync():
        sync_model.sync_positions(
            ts,
            state,
            universe,
            pricing_model
        )

    vault_address = sync_model.get_key_address()
    hot_wallet = sync_model.get_hot_wallet()
    gas_at_start = hot_wallet.get_native_currency_balance(web3)

    logger.info("Account data before starting to close all")
    logger.info("  Vault address: %s", vault_address)
    logger.info("  Hot wallet address: %s", hot_wallet.address)
    logger.info("  Hot wallet balance: %s", gas_at_start)

    if isinstance(sync_model, EnzymeVaultSyncModel):
        vault = sync_model.vault
        logger.info("  Comptroller address: %s", vault.comptroller.address)
        logger.info("  Vault owner: %s", vault.vault.functions.getOwner().call())
        sync_model.check_ownership()

    if len(state.portfolio.reserves) == 0:
        raise RuntimeError("No reserves detected for the strategy. Does your wallet/vault have USDC deposited for trading?")

    reserve_currency = state.portfolio.get_default_reserve_position().asset.token_symbol
    reserve_currency_at_start = state.portfolio.get_default_reserve_position().get_value()

    logger.info("  Reserve currency balance: %s %s", reserve_currency_at_start, reserve_currency)

    assert reserve_currency_at_start > 0, f"No deposits available to trade. Vault at {vault_address}"

    # Create PositionManager helper class
    # that helps open and close positions
    position_manager = PositionManager(
        ts,
        universe.data_universe,
        state,
        pricing_model,
        default_slippage_tolerance=slippage_tolerance,
    )

    # The message left on the positions that were closed
    note = f"Closed with close-all command at {datetime.datetime.utcnow()}"

    # Open the test position only if there isn't position already open
    # on the previous run

    open_positions = list(state.portfolio.open_positions.values())
    logger.info("Performing close-all for %d open positions", len(open_positions))

    assert len(open_positions) > 0, "Strategy does not have any open positions to close"

    if position_id is None:
        positions_to_close = list(open_positions)
    else:
        positions_to_close = [state.portfolio.open_positions[position_id]]

    for p in positions_to_close:
        logger.info("  Position: %s, quantity %s", p, p.get_quantity())

        trading_quantity = p.get_available_trading_quantity()
        quantity = p.get_quantity()
        assert trading_quantity == quantity, f"Position quantity vs. available trading quantity mismatch. Probably unexecuted trades? {quantity} vs. {trading_quantity}"

    if interactive:
        confirmation = input("Attempt to close positions [y/n]").lower()
        if confirmation != "y":
            raise CloseAllAborted()

    for p in positions_to_close:
        # Create trades to open the position
        logger.info("Closing position %s", p)

        trades = position_manager.close_position(p)

        assert len(trades) == 1
        trade = trades[0]

        # Compose the trades as approve() + swapTokenExact(),
        # broadcast them to the blockchain network and
        # wait for the confirmation
        execution_model.execute_trades(
            ts,
            state,
            trades,
            routing_model,
            routing_state,
        )

        if not trade.is_success():
            logger.error("Trade failed: %s", trade)
            logger.error("Tx hash: %s", trade.blockchain_transactions[-1].tx_hash)
            logger.error("Revert reason: %s", trade.blockchain_transactions[-1].revert_reason)
            logger.error("Trade dump:\n%s", trade.get_debug_dump())
            raise AssertionError("Trade to close position failed")

        if p.notes is None:
            p.notes = ""

        p.add_notes_message(note)

    gas_at_end = hot_wallet.get_native_currency_balance(web3)
    reserve_currency_at_end = state.portfolio.get_default_reserve_position().get_value()

    logger.info("Trade report")
    logger.info("  Gas spent: %s", gas_at_start - gas_at_end)
    logger.info("  Trades done currently: %d", len(list(state.portfolio.get_all_trades())))
    logger.info("  Reserves currently: %s %s", reserve_currency_at_end, reserve_currency)
    logger.info("  Reserve currency spent: %s %s", reserve_currency_at_start - reserve_currency_at_end, reserve_currency)

    df = display_positions(state.portfolio.frozen_positions.values())
    position_info = tabulate(df, headers='keys', tablefmt='rounded_outline')

    logger.info("Position data for positions that were closed:\n%s", position_info)