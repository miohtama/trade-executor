"""Test that Pancake momentum strategy with 8h ticks and 4h candles.

To run:

.. code-block:: shell

    export TRADING_STRATEGY_API_KEY="secret-token:tradingstrategy-6ce98...."
    export BNB_CHAIN_JSON_RPC="https://bsc-dataseed.binance.org/"

"""
import datetime
import logging
import os
import pickle
from pathlib import Path

import pytest
from eth_account import Account

from eth_defi.abi import get_deployed_contract
from eth_defi.txmonitor import wait_transactions_to_complete
from eth_typing import HexAddress, HexStr
from hexbytes import HexBytes
from typer.testing import CliRunner
from web3 import Web3, HTTPProvider
from web3.contract import Contract

from eth_defi.ganache import fork_network
from eth_defi.hotwallet import HotWallet
from tradeexecutor.cli.main import app
from tradeexecutor.state.state import State

from tradeexecutor.cli.log import setup_pytest_logging


# https://docs.pytest.org/en/latest/how-to/skipping.html#skip-all-test-functions-of-a-class-or-module
pytestmark = pytest.mark.skipif(os.environ.get("BNB_CHAIN_JSON_RPC") is None, reason="Set BNB_CHAIN_JSON_RPC environment variable to Binance Smart Chain node to run this test")


@pytest.fixture(scope="module")
def logger(request):
    """Setup test logger."""
    return setup_pytest_logging(request)


@pytest.fixture
def busd_token(web3) -> Contract:
    """BUSD with $4B supply."""
    # https://bscscan.com/address/0xe9e7cea3dedca5984780bafc599bd69add087d56
    token = get_deployed_contract(web3, "ERC20MockDecimals.json", "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56")
    return token


@pytest.fixture()
def large_busd_holder() -> HexAddress:
    """A random account picked from BNB Smart chain that holds a lot of BUSD.

    This account is unlocked on Ganache, so you have access to good BUSD stash.

    `To find large holder accounts, use bscscan <https://bscscan.com/token/0xe9e7cea3dedca5984780bafc599bd69add087d56#balances>`_.
    """
    # Binance Hot Wallet 6
    return HexAddress(HexStr("0x8894E0a0c962CB723c1976a4421c95949bE2D4E3"))


@pytest.fixture()
def ganache_bnb_chain_fork(logger, large_busd_holder) -> str:
    """Create a testable fork of live BNB chain.

    :return: JSON-RPC URL for Web3
    """

    mainnet_rpc = os.environ["BNB_CHAIN_JSON_RPC"]

    launch = fork_network(
        mainnet_rpc,
        block_time=1,  # Insta mining cannot be done in this test
        evm_version="berlin",  # BSC is not yet London compatible?
        unlocked_addresses=[large_busd_holder],  # Unlock WBNB stealing
        quiet=True,  # Otherwise the Ganache output is millions lines of long
    )
    yield launch.json_rpc_url
    # Wind down Ganache process after the test is complete
    launch.close(verbose=True)


@pytest.fixture
def web3(ganache_bnb_chain_fork: str):
    """Set up a local unit testing blockchain."""
    # https://web3py.readthedocs.io/en/stable/examples.html#contract-unit-tests-in-python
    return Web3(HTTPProvider(ganache_bnb_chain_fork))


@pytest.fixture
def chain_id(web3):
    return web3.eth.chain_id


@pytest.fixture()
def hot_wallet(web3: Web3, busd_token: Contract, large_busd_holder: HexAddress) -> HotWallet:
    """Our trading Ethereum account.

    Start with 10,000 USDC cash and 2 BNB.
    """
    account = Account.create()
    web3.eth.send_transaction({"from": large_busd_holder, "to": account.address, "value": 2*10**18})
    tx_hash = busd_token.functions.transfer(account.address, 10_000 * 10**18).transact({"from": large_busd_holder})
    wait_transactions_to_complete(web3, [tx_hash])
    wallet = HotWallet(account)
    wallet.sync_nonce(web3)
    return wallet


@pytest.fixture()
def strategy_path() -> Path:
    """Where do we load our strategy file."""
    return Path(os.path.join(os.path.dirname(__file__), "../..", "strategies", "bnb_chain_16h_momentum.py"))


@pytest.mark.skipif(os.environ.get("CI") is not None, reason="This test is too flaky on Github CI. Manual runs only.")
def test_bnb_chain_16h_momentum(
        logger: logging.Logger,
        strategy_path: Path,
        ganache_bnb_chain_fork,
        hot_wallet: HotWallet,
    ):
    """Run the strategy test

    - Use decision data from the past

    - Trade against live exchanges
    """

    debug_dump_file = "/tmp/test_bnb_chain_16h_momentum.debug.json"

    state_file = "/tmp/test_bnb_chain_16h_momentum.json"

    # Set up the configuration for the backtesting,
    # run the loop 6 cycles using Ganache + live BNB Chain fork
    environment = {
        "NAME": "test_bnb_chain_16h_momentum",
        "STRATEGY_FILE": strategy_path.as_posix(),
        "PRIVATE_KEY": hot_wallet.account.privateKey.hex(),
        "HTTP_ENABLED": "false",
        "JSON_RPC": ganache_bnb_chain_fork,
        "GAS_PRICE_METHOD": "legacy",
        "STATE_FILE": state_file,
        "RESET_STATE": "true",
        "EXECUTION_TYPE": "uniswap_v2_hot_wallet",
        "APPROVAL_TYPE": "unchecked",
        "CACHE_PATH": "/tmp/main_loop_tests",
        "TRADING_STRATEGY_API_KEY": os.environ["TRADING_STRATEGY_API_KEY"],
        "DEBUG_DUMP_FILE": debug_dump_file,
        "BACKTEST_START": "2021-12-07",
        "BACKTEST_END": "2021-12-09",
        "TICK_OFFSET_MINUTES": "10",
        "TICK_SIZE": "16h",
        "CONFIRMATION_BLOCK_COUNT": "8",
    }

    # Points to the private test trash channel on Discord
    discord_webhook_url = os.environ.get("DISCORD_TRASH_WEBHOOK_URL")
    if discord_webhook_url:
        environment["DISCORD_WEBHOOK_URL"] = discord_webhook_url
        environment["DISCORD_AVATAR_URL"] = "https://i0.wp.com/www.theterminatorfans.com/wp-content/uploads/2012/09/the-terminator3.jpg?resize=900%2C450&ssl=1"

    # https://typer.tiangolo.com/tutorial/testing/
    runner = CliRunner()

    try:
        result = runner.invoke(app, "start", env=environment)

        if result.exception:
            raise result.exception

        if result.exit_code != 0:
            logger.error("runner failed")
            for line in result.stdout.split('\n'):
                logger.error(line)
            raise AssertionError("runner launch failed")

        assert result.exit_code == 0

    except ValueError as e:
        # ValueError: I/O operation on closed file.
        # bug in Typer,
        # but the app should still have completed
        logger.error("Typer failed to close cleanly %s", e)

    with open(debug_dump_file, "rb") as inp:
        debug_dump = pickle.load(inp)

        # We run for 2 days, 3 rebalances per day
        assert len(debug_dump) == 6

        cycle_1 = debug_dump[1]
        cycle_2 = debug_dump[2]
        cycle_3 = debug_dump[3]
        # cycle_4 = debug_dump[4]

        logger.info("Cycle 1 trades %s", cycle_1["rebalance_trades"])
        assert cycle_1["cycle"] == 1
        assert cycle_1["timestamp"] == datetime.datetime(2021, 12, 7, 0, 0)
        assert len(cycle_1["approved_trades"]) == 4
        assert len(cycle_1["positions_at_start_of_construction"]) == 0

        # 4 buys + 4 sells
        logger.info("Cycle 2 trades %s", cycle_2["rebalance_trades"])
        assert cycle_2["cycle"] == 2
        assert cycle_2["timestamp"].replace(minute=0) == datetime.datetime(2021, 12, 7, 8, 0)
        assert len(cycle_2["approved_trades"]) == 8
        assert len(cycle_2["positions_at_start_of_construction"]) == 4

        # 4 buys + 4 sells
        logger.info("Cycle 3 trades %s", cycle_3["rebalance_trades"])
        assert cycle_3["cycle"] == 3
        assert len(cycle_3["positions_at_start_of_construction"]) == 4
        assert len(cycle_3["approved_trades"]) == 8
        assert cycle_3["timestamp"].replace(minute=0) == datetime.datetime(2021, 12, 7, 16, 0)

        # 3 buys + 4 sells
        #logger.info("Cycle 4 trades %s", cycle_4["rebalance_trades"])
        #assert cycle_4["cycle"] == 4
        #assert len(cycle_4["positions_at_start_of_construction"]) == 4
        #assert len(cycle_4["approved_trades"]) == 7
        #assert cycle_4["timestamp"].replace(minute=0) == datetime.datetime(2021, 12, 8, 0, 0)

    # See we can load the state after all this testing.
    # Mainly stresses on serialization/deserialization issues.
    json_text = open(state_file, "rt").read()
    state = State.from_json(json_text)
    state.perform_integrity_check()
    assert len(state.portfolio.open_positions) > 0
    assert len(state.portfolio.closed_positions) > 0

    logger.info("All ok")
