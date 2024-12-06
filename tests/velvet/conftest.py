"""Test fixtures for Velvet tests.

- Uses Base mainnet onchain vault in testing (forked), as Velvet lacks any kind of test support
"""

import os
from typing import cast

import pytest
from eth_typing import HexAddress
from web3 import Web3

from eth_defi.provider.anvil import AnvilLaunch, fork_network_anvil
from eth_defi.provider.multi_provider import create_multi_provider_web3
from eth_defi.token import fetch_erc20_details, TokenDetails
from eth_defi.vault.base import VaultSpec
from eth_defi.velvet import VelvetVault
from tradeexecutor.ethereum.ethereum_protocol_adapters import create_uniswap_v3_adapter, EthereumPairConfigurator
from tradeexecutor.ethereum.uniswap_v2.uniswap_v2_live_pricing import UniswapV2LivePricing
from tradeexecutor.ethereum.uniswap_v3.uniswap_v3_live_pricing import UniswapV3LivePricing
from tradeexecutor.strategy.generic.generic_pricing_model import GenericPricing
from tradeexecutor.strategy.generic.pair_configurator import ProtocolRoutingId
from tradeexecutor.strategy.trading_strategy_universe import TradingStrategyUniverse, translate_token

from tradingstrategy.chain import ChainId
from tradingstrategy.exchange import ExchangeUniverse, Exchange, ExchangeType
from tradingstrategy.pair import PandasPairUniverse

from tradeexecutor.state.identifier import AssetIdentifier, TradingPairIdentifier
from tradeexecutor.strategy.reverse_universe import create_universe_from_trading_pair_identifiers
from tradingstrategy.timebucket import TimeBucket
from tradingstrategy.universe import Universe


JSON_RPC_BASE = os.environ.get("JSON_RPC_BASE", "https://mainnet.base.org")


@pytest.fixture()
def vault_owner() -> HexAddress:
    # Vaut owner
    return "0x0c9db006f1c7bfaa0716d70f012ec470587a8d4f"


@pytest.fixture()
def existing_shareholder() -> HexAddress:
    """A user that has shares for the vault that can be redeemed.

    - This user has a pre-approved approve() to withdraw all shares

    https://basescan.org/token/0x205e80371f6d1b33dff7603ca8d3e92bebd7dc25#balances
    """
    return "0x0C9dB006F1c7bfaA0716D70F012EC470587a8D4F"


@pytest.mark.skipif(not JSON_RPC_BASE, reason="JSON_RPC_BASE is needed to run mainnet fork tets")
@pytest.fixture()
def anvil_base_fork(request, vault_owner, deposit_user, existing_shareholder) -> AnvilLaunch:
    """Create a testable fork of live BNB chain.

    :return: JSON-RPC URL for Web3
    """
    launch = fork_network_anvil(
        JSON_RPC_BASE,
        unlocked_addresses=[vault_owner, deposit_user, existing_shareholder],
        # Cannot use fork_block_number with live Enso API :(
        # fork_block_number=22_978_054,  # Keep block height stable for tests
    )
    try:
        yield launch
    finally:
        # Wind down Anvil process after the test is complete
        launch.close()


@pytest.fixture()
def web3(anvil_base_fork) -> Web3:
    web3 = create_multi_provider_web3(
        anvil_base_fork.json_rpc_url,
        retries=0,  # eth_sendTransaction retry spoils the tests
        default_http_timeout=(2, 60),  # Anvil is slow with eth_sendTransaction
    )
    assert web3.eth.chain_id == 8453
    return web3


@pytest.fixture(scope='module')
def base_test_vault_spec() -> VaultSpec:
    """Vault https://dapp.velvet.capital/ManagerVaultDetails/0x205e80371f6d1b33dff7603ca8d3e92bebd7dc25"""
    return VaultSpec(8453, "0x205e80371f6d1b33dff7603ca8d3e92bebd7dc25")


@pytest.fixture(scope='module')
def base_usdc_address() -> HexAddress:
    """USDC"""
    return "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


@pytest.fixture(scope='module')
def base_doginme_address() -> HexAddress:
    """DogInMe"""
    return "0x6921B130D297cc43754afba22e5EAc0FBf8Db75b"


@pytest.fixture()
def base_example_vault(web3, base_test_vault_spec: VaultSpec) -> VelvetVault:
    return VelvetVault(web3, base_test_vault_spec)


@pytest.fixture()
def deposit_user() -> HexAddress:
    """A user that has preapproved 5 USDC deposit for the vault above, no approve() needed."""
    return "0x7612A94AafF7a552C373e3124654C1539a4486A8"


@pytest.fixture(scope='module')
def base_usdc(base_usdc_address) -> AssetIdentifier:
    """USDC"""
    return AssetIdentifier(
        chain_id=8453,
        address=base_usdc_address,
        decimals=6,
        token_symbol="USDC",
    )


@pytest.fixture()
def base_usdc_token(web3, base_usdc) -> TokenDetails:
    """USDC"""
    return fetch_erc20_details(web3, base_usdc.address)


@pytest.fixture(scope='module')
def base_doginme(base_doginme_address) -> AssetIdentifier:
    """DogInMe"""
    return AssetIdentifier(
        chain_id=8453,
        address=base_doginme_address,
        decimals=18,
        token_symbol="DogInMe",
    )



@pytest.fixture(scope='module')
def base_ski(base_doginme_address) -> AssetIdentifier:
    """SKI - SkiMask"""
    return AssetIdentifier(
        chain_id=8453,
        address="0x768BE13e1680b5ebE0024C42c896E3dB59ec0149",
        decimals=18,
        token_symbol="SKI",
    )


@pytest.fixture()
def base_doginme_token(web3, base_doginme) -> TokenDetails:
    """DogInMe"""
    return fetch_erc20_details(web3, base_doginme.address)


@pytest.fixture(scope='module')
def base_weth() -> AssetIdentifier:
    """WETH"""
    return AssetIdentifier(
        chain_id=8453,
        address="0x4200000000000000000000000000000000000006",
        decimals=18,
        token_symbol="WETH",
    )


@pytest.fixture(scope='module')
def velvet_test_vault_pair_universe(
    base_usdc,
    base_doginme,
    base_weth,
    base_ski,
) -> PandasPairUniverse:
    """Define pair universe of USDC and DogMeIn assets, trading on Uni v3 on Base.

    """

    exchange_universe = ExchangeUniverse(
        exchanges={
            1: Exchange(
                chain_id=ChainId(8453),
                chain_slug="base",
                exchange_id=1,
                exchange_slug="uniswap-v3",
                address="0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
                exchange_type=ExchangeType.uniswap_v3,
                pair_count=1,
            ),

            2: Exchange(
                chain_id=ChainId(8453),
                chain_slug="base",
                exchange_id=2,
                exchange_slug="uniswap-v2",
                address="0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6",
                exchange_type=ExchangeType.uniswap_v2,
                pair_count=1,
            )
        }
    )

    trading_pair_uniswap_v3 = TradingPairIdentifier(
        base=base_doginme,
        quote=base_weth,
        pool_address="0xADE9BcD4b968EE26Bed102dd43A55f6A8c2416df",
        exchange_address="0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
        fee=0.0100,
    )

    trading_pair_uniswap_v2 = TradingPairIdentifier(
        base=base_ski,
        quote=base_weth,
        pool_address="0x6d6391b9bd02eefa00fa711fb1cb828a6471d283",
        exchange_address="0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6",  # Uniswap v2 factory on Base
        fee=0.0030,
    )

    # https://coinmarketcap.com/dexscan/base/0xd0b53d9277642d899df5c87a3966a349a798f224/
    weth_usdc_uniswap_v3 = TradingPairIdentifier(
        base=base_weth,
        quote=base_usdc,
        pool_address="0xd0b53d9277642d899df5c87a3966a349a798f224",
        exchange_address="0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
        fee=0.0005,
    )

    # https://app.uniswap.org/explore/pools/base/0x88A43bbDF9D098eEC7bCEda4e2494615dfD9bB9C
    weth_usdc_uniswap_v2 = TradingPairIdentifier(
        base=base_weth,
        quote=base_usdc,
        pool_address="0x88A43bbDF9D098eEC7bCEda4e2494615dfD9bB9C",
        exchange_address="0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6",
        fee=0.0030,
    )

    universe = create_universe_from_trading_pair_identifiers(
        [trading_pair_uniswap_v3, trading_pair_uniswap_v2, weth_usdc_uniswap_v3, weth_usdc_uniswap_v2],
        exchange_universe=exchange_universe,
    )
    return universe


@pytest.fixture(scope='module')
def velvet_test_vault_strategy_universe(
    velvet_test_vault_pair_universe,
    base_usdc,
) -> TradingStrategyUniverse:
    """Contains pairs and exchanges"""

    universe = Universe(
        chains={ChainId.base},
        time_bucket=TimeBucket.not_applicable,  # Not used, only live price checks done
        exchange_universe=velvet_test_vault_pair_universe.exchange_universe,
        pairs=velvet_test_vault_pair_universe,
    )

    usdc = velvet_test_vault_pair_universe.get_token(base_usdc.address)

    return TradingStrategyUniverse(
        data_universe=universe,
        reserve_assets={translate_token(usdc)},
    )


@pytest.fixture()
def velvet_uniswap_v3_pricing(
    web3,
    velvet_test_vault_strategy_universe,
) -> UniswapV3LivePricing:
    """Uses Uni v3 onchain pricing, needed when generating spot positions for new tokens detected."""

    routing_id = ProtocolRoutingId(
        router_name="uniswap-v3",
        exchange_slug="uniswap-v3",
    )

    # Should autopick Uniswap v3 on Base
    routing_config = create_uniswap_v3_adapter(
        web3,
        strategy_universe=velvet_test_vault_strategy_universe,
        routing_id=routing_id,
    )

    return cast(UniswapV3LivePricing, routing_config.pricing_model)


@pytest.fixture()
def velvet_uniswap_v2_pricing(
    web3,
    velvet_test_vault_strategy_universe,
) -> UniswapV2LivePricing:
    """Uses Uni v3 onchain pricing, needed when generating spot positions for new tokens detected."""

    routing_id = ProtocolRoutingId(
        router_name="uniswap-v2",
        exchange_slug="uniswap-v2",
    )

    # Should autopick Uniswap v3 on Base
    routing_config = create_uniswap_v3_adapter(
        web3,
        strategy_universe=velvet_test_vault_strategy_universe,
        routing_id=routing_id,
    )

    return cast(UniswapV2LivePricing, routing_config.pricing_model)


@pytest.fixture()
def velvet_pricing_model(
    web3: Web3,
    velvet_test_vault_strategy_universe
) -> GenericPricing:
    """Mixed Uniswap v2 and v3 pairs on Base."""

    pair_configurator = EthereumPairConfigurator(
        web3,
        velvet_test_vault_strategy_universe,
    )

    return GenericPricing(
        pair_configurator,
    )
