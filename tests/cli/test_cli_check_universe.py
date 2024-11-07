import os
from pathlib import Path

import pytest

from tradeexecutor.cli.main import app


@pytest.fixture(scope="session")
def unit_test_cache_path():
    """Where unit tests  cache files.

    We have special path for CLI tests to make sure CLI tests
    always do fresh downloads.
    """
    path = os.path.join(os.path.dirname(__file__), "/tmp/cli_tests")
    os.makedirs(path, exist_ok=True)
    return path


def test_cli_check_universe(
    unit_test_cache_path: str,
    mocker,
):
    """check-universe command works"""

    strategy_path = Path(os.path.join(os.path.dirname(__file__), "..", "..", "strategies", "test_only", "enzyme-ethereum-btc-eth-stoch-rsi.py"))

    environment = {
        "TRADING_STRATEGY_API_KEY": os.environ["TRADING_STRATEGY_API_KEY"],
        "STRATEGY_FILE": strategy_path.as_posix(),
        "CACHE_PATH": unit_test_cache_path,
        "LOG_LEVEL": "disabled",
        "UNIT_TESTING": "true",
        "MAX_DATA_DELAY_MINUTES": "99999",
    }

    mocker.patch.dict("os.environ", environment, clear=True)
    app(["check-universe"], standalone_mode=False)