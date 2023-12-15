"""Test transaction reconfirmation."""

import os
import shutil
import tempfile
import logging

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.main import get_command

from eth_defi.provider.anvil import AnvilLaunch, fork_network_anvil
from eth_typing import HexAddress

from eth_defi.hotwallet import HotWallet
from eth_defi.uniswap_v2.deployment import UniswapV2Deployment

from tradingstrategy.pair import PandasPairUniverse

from tradeexecutor.cli.main import app
from tradeexecutor.state.state import State


logger = logging.getLogger(__name__)


@pytest.fixture
def anvil_polygon_chain_fork() -> str:
    """Create a testable fork of live Polygon.

    :return: JSON-RPC URL for Web3
    """
    mainnet_rpc = os.environ["JSON_RPC_POLYGON"]
    launch = fork_network_anvil(
        mainnet_rpc,
        fork_block_number=51_156_615
    )
    try:
        yield launch.json_rpc_url
    finally:
        # Wind down Anvil process after the test is complete
        # launch.close(log_level=logging.ERROR)
        launch.close()


@pytest.fixture()
def strategy_file() -> Path:
    """Where do we load our strategy file."""
    return Path(os.path.dirname(__file__)) / "../../strategies/test_only" / "reconfirm.py"


@pytest.fixture()
def state_file() -> Path:
    """Return mutable test state copy."""
    path =  Path(tempfile.mkdtemp()) / "test-reconfirm.json"
    source = os.path.join(os.path.dirname(__file__), "reconfirm-test-state.json")
    shutil.cp(source, path)
    return path


@pytest.fixture()
def environment(
    anvil: AnvilLaunch,
    deployer: HexAddress,
    user_1: HexAddress,
    uniswap_v2: UniswapV2Deployment,
    pair_universe: PandasPairUniverse,
    hot_wallet: HotWallet,
    state_file: Path,
    strategy_file: Path,
    ) -> dict:
    """Set up environment vars for all CLI commands."""

    environment = {
        "EXECUTOR_ID": "test_close_all",
        "STRATEGY_FILE": strategy_file.as_posix(),
        # "PRIVATE_KEY": hot_wallet.account.key.hex(),  # Irrelevant
        "JSON_RPC_ANVIL": anvil.json_rpc_url,
        "STATE_FILE": state_file.as_posix(),
        "ASSET_MANAGEMENT_MODE": "enzyme",
        "UNIT_TESTING": "true",
    }
    return environment


def test_cli_reconfirm(
    environment: dict,
    state_file: Path,
):
    """Perform reconfirm command

    - Load a broken state snapshot from a test straetgy

    - Perform reconfirm for transactions broken in this state,
      taken from at a specific Polygon mainnet fork
    """

    cli = get_command(app)
    with patch.dict(os.environ, environment, clear=True):
        with pytest.raises(SystemExit) as e:
            cli.main(args=["reconfirm"])
        assert e.value.code == 0

    state = State.read_json_file(state_file)
    assert len(state.portfolio.open_positions) == 1
