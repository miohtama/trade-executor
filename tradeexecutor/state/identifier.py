"""Asset and trading pair identifiers.

How executor internally knows how to connect trading pairs in data and in execution environment (on-chain).
"""
import enum
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from dataclasses_json import dataclass_json
from eth_typing import HexAddress
from tradingstrategy.chain import ChainId
from web3 import Web3

from tradeexecutor.state.types import JSONHexAddress
from tradingstrategy.stablecoin import is_stablecoin_like


@dataclass_json
@dataclass
class AssetIdentifier:
    """Identify a blockchain asset for trade execution.

    As internal token_ids and pair_ids may be unstable, trading pairs and tokens are explicitly
    referred by their smart contract addresses when a strategy decision moves to the execution.
    We duplicate data here to make sure we have a persistent record that helps to diagnose the sisues.
    """

    #: See https://chainlist.org/
    chain_id: int

    #: Smart contract address of the asset.
    #: Always lowercase.
    address: JSONHexAddress

    #: The ticker symbol of this token.
    token_symbol: str

    #: How many tokens this decimals.
    #: Must be always set and non-negative.
    decimals: int

    #: How this asset is referred in the internal database
    internal_id: Optional[int] = None

    #: Info page URL for this asset
    info_url: Optional[str] = None

    def __str__(self):
        return f"<{self.token_symbol} at {self.address}>"

    def __hash__(self):
        assert self.chain_id, "chain_id needs to be set to be hashable"
        assert self.address, "address needs to be set to be hashable"
        return hash((self.chain_id, self.address))

    def __eq__(self, other):
        assert isinstance(other, TradingPairIdentifier), f"Got {other}"
        return self.chain_id == other.chain_id and self.address == other.address

    def __post_init__(self):
        assert type(self.address) == str, f"Got address {self.address} as {type(self.address)}"
        assert self.address.startswith("0x")
        self.address= self.address.lower()
        assert type(self.chain_id) == int
        assert type(self.decimals) == int, f"Bad decimals {self.decimals}"
        assert self.decimals >= 0

    def get_identifier(self) -> str:
        """Assets are identified by their smart contract address."""
        return self.address.lower()

    @property
    def checksum_address(self) -> HexAddress:
        """Ethereum madness."""
        return Web3.to_checksum_address(self.address)

    def __eq__(self, other: "AssetIdentifier") -> bool:
        """Assets are considered be identical if they share the same smart contract address."""
        assert isinstance(other, AssetIdentifier), f"Compared to wrong class: {other} {other.__class__}"
        return self.address.lower() == other.address.lower()

    def convert_to_raw_amount(self, amount: Decimal) -> int:
        """Return any amount in token native units.

        Convert decimal to fixed point integer.
        """
        assert isinstance(amount, Decimal), "Input only exact numbers for the conversion, not fuzzy ones like floats"
        assert self.decimals is not None, f"Cannot perform human to raw token amount conversion, because no decimals given: {self}"
        return int(amount * Decimal(10**self.decimals))

    def convert_to_decimal(self, raw_amount: int) -> Decimal:
        assert self.decimals is not None, f"Cannot perform human to raw token amount conversion, because no decimals given: {self}"
        return Decimal(raw_amount) / Decimal(10**self.decimals)

    def is_stablecoin(self) -> bool:
        """Do we think this asset reprents a stablecoin"""
        return is_stablecoin_like(self.token_symbol)


class TradingPairKind(enum.Enum):
    """What kind of trading position this is.

    - Spot markets are base:quote token pairs

    - Credit supplies are aToken:token pairs
    """

    #: Bought tokens from DEX
    #:
    spot_market_hold = "spot_market_hold"

    #: Bought rebalancing tokens from DEX
    #:
    #: E.g. buy stETH or aUSD directly through DEX,
    #: instead of thru vault/reserves deposit.
    #:
    spot_market_rebalancing_token = "spot_market_rebalancing token"

    #: Supplying credit to Aave reserves/gaining interest
    #:
    credit_supply = "credit_supply"

    #: Leveraged long constructd using lending protocols
    #:
    lending_protocol_long = "lending_protocol_long"

    #: Leveraged short constructd using lending protocols
    #:
    lending_protocol_short = "lending_protocol_short"

    def is_interest_accruing(self) -> bool:
        return self != TradingPairKind.spot_market_hold


@dataclass_json
@dataclass(slots=True)
class TradingPairIdentifier:
    """Uniquely identify one trading pair across all tradeable blockchain assets.

    - Tokens are converted from machine readable token0 - token1 pair
      to more human-friendly base and quote token pair.
      See :ref:`conversion <trading pair>`.

    - This class is a data class that is a copy-by-value in the persistent state:
      We copy both machine-readable information (smart contract addresses)
      and human readable information (symbols), as both are important
      to store for the persistent use - we do not expect to be able to lookup
      the information again with smart contract addresses in the future,
      as API access is expensive and blockchains may permanently be abandon.


    - This class is preferred to be used as immutable, but
      sometimes it is handy to manually override :py:attr`fee`
      for different backtesting scenarios

    - This identifier is also used for :term:`lending protocols <lending protocol>`.
      In this case :py:attr:`base` is aToken like aUSDC. and :py:attr:`quote`
      is USDC.
    """

    #: Base token in this trading pair
    #:
    #: E.g. `WETH`
    base: AssetIdentifier

    #: Quote token in this trading pair
    #:
    #: E.g. `USDC`
    quote: AssetIdentifier

    #: Smart contract address of the pool contract.
    #:
    #: - Uniswap v2 pair contract address
    #:
    #: - Uniswap v3 pool contract address
    #:
    #: Set to asset address for Aave pools.
    #:
    pool_address: str

    #: Exchange address.
    #: Identifies a decentralised exchange.
    #: Uniswap v2 likes are identified by their factory address.
    exchange_address: str

    #: How this asset is referred in the internal database
    #:
    #: Internal ids are not stable over the long duration.
    #: Internal ids are not also stable across different oracles.
    #: Always use `(chain_id, pool_address)` pair for persistent lookups.
    #:
    internal_id: Optional[int] = None

    #: What is the internal exchange id of this trading pair.
    internal_exchange_id: Optional[int] = None

    #: Info page URL for this trading pair e.g. with the price charts
    info_url: Optional[str] = None

    #: Trading fee for this pair.
    #:
    #: Liquidity provider fee expressed as the percent of the trade.
    #:
    #: E.g. `0.0030` for 0.30% fee.
    #:
    #: Should be filled for all Uniswap v2 and Uniswap v3 pairs.
    #: If the smaller Uni v2 forks do not have good data, 0.0030% is assumed.
    #:
    fee: Optional[float] = None

    #: The underlying token0/token1 for Uniswap pair is flipped compared to base token/quote token.
    #:
    #: Use :py:meth:`has_reverse_token_order` to access - might not be set.
    #:
    reverse_token_order: Optional[bool] = None

    #: What kind of position this is
    #:
    kind: TradingPairKind = TradingPairKind.spot_market_hold

    #: How this pair is routed
    #:
    #: Always initially set to None, but can be set later
    #:
    routing_model: Optional["RoutingModel"] = None

    def __post_init__(self):
        assert self.base.chain_id == self.quote.chain_id, "Cross-chain trading pairs are not possible"

        # float/int zero fix
        # TODO: Can be carefully removed later
        if self.fee == 0:
            self.fee = 0.0

        assert (type(self.fee) in {float, type(None)}) or (self.fee == 0)

    def __repr__(self):
        fee = self.fee or 0
        return f"<Pair {self.base.token_symbol}-{self.quote.token_symbol} at {self.pool_address} ({fee * 100:.4f}% fee) on exchange {self.exchange_address}>"

    def __hash__(self):
        assert self.internal_id, "Internal id needed to be hashable"
        return self.internal_id

    def __eq__(self, other):
        assert isinstance(other, TradingPairIdentifier), f"Got {other}"
        return self.base == other.base and self.quote == other.quote

    @property
    def chain_id(self) -> int:
        """Return raw chain id.

        Get one from the base token, beacuse both tokens are on the same chain.

        See also :py:class:`tradingstrategy.chain.ChainId`
        """
        return self.base.chain_id

    def get_identifier(self) -> str:
        """We use the smart contract pool address to uniquely identify trading positions.

        Ethereum address is lowercased, not checksummed.
        """
        return self.pool_address.lower()

    def get_ticker(self) -> str:
        """Return base token symbol - quote token symbol human readable ticket.

        Example: `WETH-USDC`.
        """
        return f"{self.base.token_symbol}-{self.quote.token_symbol}"

    def get_human_description(self) -> str:
        """Same as get_ticker()."""
        return self.get_ticker()

    def has_complete_info(self) -> bool:
        """Check if the pair has good information.

        Because of the open-ended  nature a lot of irrelevant broken
        data can be found on blockchains.

        Both base and quote token must have

        - Symbol

        - Decimals

        This check is mainly useful to filter out crap tokens
        from the trading decisions.
        """
        return (self.base.decimals > 0 and
                self.base.token_symbol and
                self.quote.decimals > 0 and
                self.quote.token_symbol)

    def has_reverse_token_order(self) -> bool:
        """Has Uniswap smart contract a flipped token order.

        - Is token0 base token or token0 is the quote token

        See :py:func:`eth_defi.uniswap_v3.price.get_onchain_price`
        """
        assert self.reverse_token_order is not None, f"reverse_token_order not set for: {self}"
        return self.reverse_token_order
