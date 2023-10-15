"""Correct accounting errors in the internal state.

"""
import datetime
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

from tabulate import tabulate
from typer import Option

from eth_defi.hotwallet import HotWallet

from tradeexecutor.strategy.account_correction import correct_accounts as _correct_accounts, check_accounts
from .app import app
from ..bootstrap import prepare_executor_id, create_web3_config, create_sync_model, create_state_store, create_client
from ..log import setup_logging
from ...ethereum.enzyme.tx import EnzymeTransactionBuilder
from ...ethereum.enzyme.vault import EnzymeVaultSyncModel
from ...ethereum.hot_wallet_sync_model import HotWalletSyncModel
from ...ethereum.tx import HotWalletTransactionBuilder
from ...strategy.bootstrap import import_strategy_file, make_factory_from_strategy_mod
from ...strategy.account_correction import calculate_account_corrections
from ...strategy.description import StrategyExecutionDescription
from ...strategy.execution_context import ExecutionContext, ExecutionMode
from ...strategy.execution_model import AssetManagementMode
from . import shared_options
from ...strategy.run_state import RunState
from ...strategy.strategy_module import StrategyModuleInformation, read_strategy_module
from ...strategy.trading_strategy_universe import TradingStrategyUniverseModel
from ...strategy.universe_model import UniverseOptions


@app.command()
def correct_accounts(
    id: str = shared_options.id,
    name: str = shared_options.name,

    strategy_file: Path = shared_options.strategy_file,
    state_file: Optional[Path] = shared_options.state_file,
    private_key: Optional[str] = shared_options.private_key,
    log_level: str = shared_options.log_level,

    trading_strategy_api_key: str = shared_options.trading_strategy_api_key,
    cache_path: Optional[Path] = shared_options.cache_path,

    asset_management_mode: AssetManagementMode = shared_options.asset_management_mode,
    vault_address: Optional[str] = shared_options.vault_address,
    vault_adapter_address: Optional[str] = shared_options.vault_adapter_address,
    vault_deployment_block_number: Optional[int] = shared_options.vault_deployment_block_number,

    json_rpc_binance: Optional[str] = shared_options.json_rpc_binance,
    json_rpc_polygon: Optional[str] = shared_options.json_rpc_polygon,
    json_rpc_avalanche: Optional[str] = shared_options.json_rpc_avalanche,
    json_rpc_ethereum: Optional[str] = shared_options.json_rpc_ethereum,
    json_rpc_arbitrum: Optional[str] = shared_options.json_rpc_arbitrum,
    json_rpc_anvil: Optional[str] = shared_options.json_rpc_anvil,

    unknown_token_receiver: Optional[str] = Option(None, "--unknown-token-receiver", envvar="UNKNOWN_TOKEN_RECEIVER", help="The Ethereum address that will receive any token that cannot be associated with an open position. For Enzyme vault based strategies this address defauts to the executor hot wallet."),

    # Test functionality
    test_evm_uniswap_v2_router: Optional[str] = shared_options.test_evm_uniswap_v2_router,
    test_evm_uniswap_v2_factory: Optional[str] = shared_options.test_evm_uniswap_v2_factory,
    test_evm_uniswap_v2_init_code_hash: Optional[str] = shared_options.test_evm_uniswap_v2_init_code_hash,
    unit_testing: bool = shared_options.unit_testing,

):
    """Correct accounting errors in the internal ledger of the trade executor.

    Trade executor tracks all non-controlled flow of assets with events.
    This includes deposits and redemptions and interest events.
    Under misbehavior, tracked asset amounts in the internal ledger
    might drift off from the actual on-chain balances. Such
    misbehavior may be caused e.g. misbehaving blockchain nodes.

    This command will fix any accounting divergences between a vault and a strategy state.
    The strategy must not have any open positions to be reinitialised, because those open
    positions cannot carry over with the current event based tracking logic.

    This command is interactive and you need to confirm any changes applied to the state.

    An old state file is automatically backed up.
    """

    global logger

    id = prepare_executor_id(id, strategy_file)

    logger = setup_logging(log_level)

    web3config = create_web3_config(
        gas_price_method=None,
        json_rpc_binance=json_rpc_binance,
        json_rpc_polygon=json_rpc_polygon,
        json_rpc_avalanche=json_rpc_avalanche,
        json_rpc_ethereum=json_rpc_ethereum,
        json_rpc_arbitrum=json_rpc_arbitrum,
        json_rpc_anvil=json_rpc_anvil,
        unit_testing=unit_testing,
    )

    assert web3config, "No RPC endpoints given. A working JSON-RPC connection is needed for check-wallet"

    # Check that we are connected to the chain strategy assumes
    web3config.choose_single_chain()

    if private_key is not None:
        hot_wallet = HotWallet.from_private_key(private_key)
    else:
        hot_wallet = None

    web3 = web3config.get_default()

    sync_model = create_sync_model(
        asset_management_mode,
        web3,
        hot_wallet,
        vault_address,
        vault_adapter_address,
    )

    logger.info("RPC details")

    # Check the chain is online
    logger.info(f"  Chain id is {web3.eth.chain_id:,}")
    logger.info(f"  Latest block is {web3.eth.block_number:,}")

    # Check balances
    logger.info("Balance details")
    logger.info("  Hot wallet is %s", hot_wallet.address)

    vault_address =  sync_model.get_vault_address()
    if vault_address:
        logger.info("  Vault is %s", vault_address)
        if vault_deployment_block_number:
            start_block = vault_deployment_block_number
            logger.info("  Vault deployment block number is %d", start_block)

    if not state_file:
        state_file = f"state/{id}.json"

    state_file = Path(state_file)
    store = create_state_store(state_file)
    assert not store.is_pristine(), f"State does not exists yet: {state_file}"

    # Make a backup
    # https://stackoverflow.com/a/47528275/315168
    backup_file = None
    for i in range(1, 99):  # Try 99 different iterateive backup filenames
        backup_file = state_file.with_suffix(f".correct-accounts-backup-{i}.json")
        if os.path.exists(backup_file):
            continue

        shutil.copy(state_file, backup_file)
        break
    else:
        raise RuntimeError(f"Could not create backup {backup_file}")

    logger.info("Old state backed up as %s", backup_file)

    state = store.load()

    mod: StrategyModuleInformation = read_strategy_module(strategy_file)

    client, routing_model = create_client(
        mod=mod,
        web3config=web3config,
        trading_strategy_api_key=trading_strategy_api_key,
        cache_path=cache_path,
        test_evm_uniswap_v2_factory=test_evm_uniswap_v2_factory,
        test_evm_uniswap_v2_router=test_evm_uniswap_v2_router,
        test_evm_uniswap_v2_init_code_hash=test_evm_uniswap_v2_init_code_hash,
        clear_caches=False,
    )
    assert client is not None, "You need to give details for TradingStrategy.ai client"

    execution_context = ExecutionContext(mode=ExecutionMode.one_off)

    strategy_factory = make_factory_from_strategy_mod(mod)

    run_description: StrategyExecutionDescription = strategy_factory(
        execution_model=None,
        execution_context=execution_context,
        sync_model=None,
        valuation_model_factory=None,
        pricing_model_factory=None,
        approval_model=None,
        client=client,
        run_state=RunState(),
        timed_task_context_manager=execution_context.timed_task_context_manager,
    )

    #
    universe_model: TradingStrategyUniverseModel = run_description.universe_model
    universe = universe_model.construct_universe(
        datetime.datetime.utcnow(),
        execution_context.mode,
        UniverseOptions()
    )

    logger.info("Universe contains %d pairs", universe.data_universe.pairs.get_count())
    logger.info("Reserve assets are: %s", universe.reserve_assets)

    assert len(universe.reserve_assets) == 1, "Need exactly one reserve asset"

    # Set initial reserves,
    # in order to run the tests
    # TODO: Have this / treasury sync as a separate CLI command later
    if unit_testing:
        if len(state.portfolio.reserves) == 0:
            logger.info("Initialising reserves for the unit test: %s", universe.reserve_assets[0])
            state.portfolio.initialise_reserves(universe.reserve_assets[0])

    corrections = calculate_account_corrections(
        universe.data_universe.pairs,
        universe.reserve_assets,
        state,
        sync_model,
    )
    corrections = list(corrections)

    if len(corrections) == 0:
        logger.info("No account corrections found")

    if isinstance(sync_model, EnzymeVaultSyncModel):
        vault = sync_model.vault
        tx_builder = EnzymeTransactionBuilder(hot_wallet, vault)
    elif isinstance(sync_model, HotWalletSyncModel):
        vault = None
        tx_builder = HotWalletTransactionBuilder(web3, hot_wallet)
    else:
        raise NotImplementedError()

    # Set the default token dump address
    if not unknown_token_receiver:
        if isinstance(sync_model, EnzymeVaultSyncModel):
            unknown_token_receiver = sync_model.get_hot_wallet().address

    if not unknown_token_receiver:
        raise RuntimeError(f"unknown_token_receiver missing and cannot deduct from the config. Please give one on the command line.")

    assert hot_wallet is not None
    hot_wallet.sync_nonce(web3)
    logger.info("Hot wallet nonce is %d", hot_wallet.current_nonce)

    balance_updates = _correct_accounts(
        state,
        corrections,
        strategy_cycle_included_at=None,
        interactive=not unit_testing,
        tx_builder=tx_builder,
        unknown_token_receiver=unknown_token_receiver,  # Send any unknown tokens to the hot wallet of the trade-executor
    )
    balance_updates = list(balance_updates)
    logger.info("Applied %d balance updates", len(balance_updates))

    store.sync(state)
    web3config.close()

    clean, df = check_accounts(
        universe.data_universe.pairs,
        universe.reserve_assets,
        state,
        sync_model,
    )

    output = tabulate(df, headers='keys', tablefmt='rounded_outline')

    if clean:
        logger.info("Accounts after the correction match:\n%s", output)
        sys.exit(0)
    else:
        logger.error("Accounts still broken after the correction")
        logger.info(output)
        sys.exit(1)
