"""Trade executor state.

The whole application date can be dumped and loaded as JSON.

Any datetime must be naive, without timezone, and is assumed to be UTC.
"""
import enum
from dataclasses import dataclass, field
import datetime
from decimal import Decimal
from itertools import chain
from typing import List, Optional, Dict, Callable, Iterable, Tuple, Any, Set

from dataclasses_json import dataclass_json
from eth_typing import HexAddress
from web3 import Web3

from .types import USDollarAmount


#
# Define our own type aliases that compatible with JSON/dataclass_json serialisation
#

#: 0x prefixed address
JSONHexAddress = str

#: 0x prefixed byte payload
JSONHexBytes = str


class PositionType(enum.Enum):
    token_hold = "token_hold"
    lending_pool_hold = "lending_pool_hold"


class TradeType(enum.Enum):
    rebalance = "rebalance"
    stop_loss = "stop_loss"
    take_profit = "take_profit"


class NotEnoughMoney(Exception):
    """We try to allocate reserve for a buy, but do not have enough it."""


class TradeStatus(enum.Enum):

    #: Trade has been put to the planning pipeline.
    #: The trade instance has been created and stored in the state,
    #: but no internal accounting changes have been made.
    planned = "planned"

    #: Trade has txid allocated.
    #: Any capital have been debited from the reserved and credited on the trade.
    started = "started"

    #: Trade has been pushed to the network
    broadcasted = "broadcasted"

    #: Trade was executed ok
    #: Any capital on sell transaction have been credited back to the reserves.
    success = "success"

    #: Trade was reversed e.g. due to too much slippage.
    #: Trade can be retries.
    failed = "failed"


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

    #: Smart contract address of the asset
    address: JSONHexAddress

    token_symbol: str
    decimals: Optional[int] = None

    #: How this asset is referred in the internal database
    internal_id: Optional[int] = None

    #: Info page URL for this asset
    info_url: Optional[str] = None

    def __str__(self):
        return f"<{self.token_symbol} at {self.address}>"

    def __post_init__(self):
        assert type(self.address) == str, f"Got address {self.address} as {type(self.address)}"
        assert self.address.startswith("0x")
        assert type(self.chain_id) == int

    def get_identifier(self) -> str:
        """Assets are identified by their smart contract address."""
        return self.address.lower()

    @property
    def checksum_address(self) -> HexAddress:
        """Ethereum madness."""
        return Web3.toChecksumAddress(self.address)


@dataclass_json
@dataclass
class TradingPairIdentifier:
    base: AssetIdentifier
    quote: AssetIdentifier

    #: Smart contract address of the pool contract.
    pool_address: str

    #: How this asset is referred in the internal database
    internal_id: Optional[int] = None

    #: Info page URL for this trading pair e.g. with the price charts
    info_url: Optional[str] = None

    def get_identifier(self) -> str:
        """We use the smart contract pool address to uniquely identify trading positions.

        Ethereum address is lowercased, not checksummed.
        """
        return self.pool_address.lower()

    def get_human_description(self) -> str:
        return f"{self.base.token_symbol}-{self.quote.token_symbol}"

    #def get_trading_pair(self, pair_universe: PandasPairUniverse) -> DEXPair:
    #    """Reverse resolves the smart contract address to trading pair data in the current trading pair universe."""
    #    return pair_universe.get_pair_by_smart_contract(self.pool_address)


@dataclass_json
@dataclass
class ReservePosition:
    asset: AssetIdentifier
    quantity: Decimal
    last_sync_at: datetime.datetime

    reserve_token_price: USDollarAmount
    last_pricing_at: datetime.datetime

    def get_identifier(self) -> str:
        return self.asset.get_identifier()

    def get_current_value(self) -> USDollarAmount:
        return float(self.quantity) * self.reserve_token_price


@dataclass_json
@dataclass
class BlockchainTransactionInfo:
    """Information about a blockchain level transaction associated with the trades and anything else.

    Transaction has four phases

    1. Preparation
    2. Signing
    3. Broadcast
    4. Confirmation
    """

    #: Chain id from https://github.com/ethereum-lists/chains
    chain_id: Optional[int] = None

    #: Contract we called. Usually the Uniswap v2 router address.
    contract_address: Optional[JSONHexAddress] = None

    #: Function we called
    function_selector: Optional[str] = None

    #: Arguments we passed to the smart contract function
    args: Optional[List[Any]] = None

    #: Blockchain bookkeeping
    tx_hash: Optional[JSONHexBytes] = None

    #: Blockchain bookkeeping
    nonce: Optional[int] = None

    #: Raw Ethereum transaction dict.
    #: Output from web3 buildTransaction()
    details: Optional[Dict] = None

    #: Raw bytes of the signed transaction
    signed_bytes: Optional[JSONHexBytes] = None

    included_at: Optional[datetime.datetime] = None
    block_number: Optional[int] = None
    block_hash: Optional[JSONHexBytes] = None

    #: status from the tx receipt. True is success, false is revert.
    status: Optional[bool] = None

    #: Gas consumed by the tx
    realised_gas_units_consumed: Optional[int] = None

    #: Gas price for the tx in gwei
    realised_gas_price: Optional[int] = None

    #: The transaction revert reason if we manage to extract it
    revert_reason: Optional[str] = None


    def set_target_information(self, chain_id: int, contract_address: str, function_selector: str, args: list, details: dict):
        """Update the information on which transaction we are going to perform."""
        assert type(contract_address) == str
        assert type(function_selector) == str
        assert type(args) == list
        self.chain_id = chain_id
        self.contract_address = contract_address
        self.function_selector = function_selector
        self.args = args
        self.details = details

    def set_broadcast_information(self, nonce: int, tx_hash: str, signed_bytes: str):
        """Update the information we are going to use to broadcast the transaction."""
        assert type(nonce) == int
        assert type(tx_hash) == str
        assert type(signed_bytes) == str
        self.nonce = nonce
        self.tx_hash = tx_hash
        self.signed_bytes = signed_bytes

    def set_confirmation_information(self,
        ts: datetime.datetime,
        block_number: int,
        block_hash: str,
        realised_gas_units_consumed: int,
        realised_gas_price: int,
        status: bool):
        """Update the information we are going to use to broadcast the transaction."""
        assert isinstance(ts, datetime.datetime)
        assert type(block_number) == int
        assert type(block_hash) == str
        assert type(realised_gas_units_consumed) == int
        assert type(realised_gas_price) == int
        assert type(status) == bool
        self.included_at = ts
        self.block_number = block_number
        self.block_hash = block_hash
        self.realised_gas_price = realised_gas_price
        self.realised_gas_units_consumed = realised_gas_units_consumed
        self.status = status


@dataclass_json
@dataclass
class TradeExecution:
    """Trade execution tracker.

    Each trade has a reserve currency that we use to trade the token (usually USDC).

    Each trade can be
    - Buy: swap quote token -> base token
    - Sell: swap base token -> quote token

    When doing a buy `planned_reserve` (fiat) is the input. This yields to `executed_quantity` of tokens
    that may be different from `planned_quantity`.

    When doing a sell `planned_quantity` (token) is the input. This yields to `executed_reserve`
    of fiat that might be different from `planned_reserve.

    Trade execution has four states
    - Planning: The execution object is prepared
    - Capital allocation and transaction creation: We move reserve from out portfolio to the trade in internal accounting
    - Transaction broadcast: trade cannot be cancelled in this point
    - Resolving the trade: We check the Ethereum transaction receipt to see how well we succeeded in the trade

    There trade state is resolved based on the market variables (usually timestamps).
    """

    trade_id: int
    position_id: int
    trade_type: TradeType
    pair: TradingPairIdentifier
    opened_at: datetime.datetime

    #: Positive for buy, negative for sell.
    planned_quantity: Decimal

    planned_price: USDollarAmount
    planned_reserve: Decimal

    #: Which reserve currency we are going to take
    reserve_currency: AssetIdentifier

    #: When capital is allocated for this trade
    started_at: Optional[datetime.datetime] = None
    reserve_currency_allocated: Optional[Decimal] = None

    #: When this trade entered mempool
    broadcasted_at: Optional[datetime.datetime] = None

    #: Timestamp of the block where the txid was first mined
    executed_at: Optional[datetime.datetime] = None
    failed_at: Optional[datetime.datetime] = None

    #: What was the actual price we received
    executed_price: Optional[USDollarAmount] = None

    #: Positive for buy, negative for sell
    executed_quantity: Optional[Decimal] = None

    executed_reserve: Optional[Decimal] = None

    #: LP fees estimated in the USD
    lp_fees_paid: Optional[USDollarAmount] = None

    #: Associated blockchain level details
    tx_info: Optional[BlockchainTransactionInfo] = None

    #: USD price per blockchain native currency unit, at the time of execution
    native_token_price: Optional[USDollarAmount] = None

    # Trade retries
    retry_of: Optional[int] = None

    def __repr__(self):
        if self.is_buy():
            return f"<Buy #{self.trade_id} {self.planned_quantity} {self.pair.base.token_symbol} at {self.planned_price}>"
        else:
            return f"<Sell #{self.trade_id} {abs(self.planned_quantity)} {self.pair.base.token_symbol} at {self.planned_price}>"

    def __post_init__(self):
        assert self.trade_id > 0
        assert self.planned_quantity != 0
        assert self.planned_price > 0
        assert self.planned_reserve >= 0
        assert self.opened_at.tzinfo is None, f"We got a datetime {self.opened_at} with tzinfo {self.opened_at.tzinfo}"

    def get_human_description(self) -> str:
        """User friendly description for this trade"""
        if self.is_buy():
            return f"Buy {self.planned_quantity} {self.pair.base.token_symbol} <id:{self.pair.base.internal_id}> at {self.planned_price}"
        else:
            return f"Sell {abs(self.planned_quantity)} {self.pair.base.token_symbol} <id:{self.pair.base.internal_id}> at {self.planned_price}"

    def is_sell(self):
        assert self.planned_quantity != 0, "Buy/sell concept does not exist for zero quantity"
        return self.planned_quantity < 0

    def is_buy(self):
        assert self.planned_quantity != 0, "Buy/sell concept does not exist for zero quantity"
        return self.planned_quantity >= 0

    def is_success(self):
        """This trade was succcessfully completed."""
        return self.executed_at is not None

    def is_failed(self):
        """This trade was succcessfully completed."""
        return self.failed_at is not None

    def is_pending(self):
        """This trade was succcessfully completed."""
        return self.get_status() in (TradeStatus.started, TradeStatus.broadcasted)

    def is_planned(self):
        """This trade is still in planning, unallocated."""
        return self.get_status() in (TradeStatus.planned,)

    def is_started(self):
        """This trade has a txid allocated."""
        return self.get_status() in (TradeStatus.started,)

    def is_accounted_for_equity(self):
        """Does this trade contribute towards the trading position equity.

        Failed trades are reverted. Only their fees account.
        """
        return self.get_status() in (TradeStatus.started, TradeStatus.broadcasted, TradeStatus.success)

    def get_status(self) -> TradeStatus:
        if self.failed_at:
            return TradeStatus.failed
        elif self.executed_at:
            return TradeStatus.success
        elif self.broadcasted_at:
            return TradeStatus.broadcasted
        elif self.started_at:
            return TradeStatus.started
        else:
            return TradeStatus.planned

    def get_executed_value(self) -> USDollarAmount:
        return abs(float(self.executed_quantity) * self.executed_price)

    def get_planned_value(self) -> USDollarAmount:
        return abs(self.planned_price * float(abs(self.planned_quantity)))

    def get_planned_reserve(self) -> Decimal:
        return self.planned_reserve

    def get_allocated_value(self) -> USDollarAmount:
        return self.reserve_currency_allocated

    def get_position_quantity(self) -> Decimal:
        """Get the planned or executed quantity of the base token.

        Positive for buy, negative for sell.
        """
        if self.executed_quantity is not None:
            return self.executed_quantity
        else:
            return self.planned_quantity

    def get_reserve_quantity(self) -> Decimal:
        """Get the planned or executed quantity of the quote token.

        Negative for buy, positive for sell.
        """
        if self.executed_reserve is not None:
            return self.executed_quantity
        else:
            return self.planned_reserve

    def get_equity_for_position(self) -> Decimal:
        """Get the planned or executed quantity of the base token.

        Positive for buy, negative for sell.
        """
        if self.executed_quantity is not None:
            return self.executed_quantity
        return Decimal(0)

    def get_equity_for_reserve(self) -> Decimal:
        """Get the planned or executed quantity of the quote token.

        Negative for buy, positive for sell.
        """

        if self.is_buy():
            if self.get_status() in (TradeStatus.started, TradeStatus.broadcasted):
                return self.reserve_currency_allocated

        return Decimal(0)

    def get_value(self) -> USDollarAmount:
        """Get estimated or realised value of this trade.

        Value is always a positive number.
        """

        if self.executed_at:
            return self.get_executed_value()
        elif self.failed_at:
            return self.get_planned_value()
        elif self.started_at:
            # Trade is being planned, but capital has already moved
            # from the portfolio reservs to this trade object in internal accounting
            return self.get_planned_value()
        else:
            # Trade does not have value until capital is allocated to it
            return 0.0

    def get_credit_debit(self) -> Tuple[Decimal, Decimal]:
        """Returns the token quantity and reserve currency quantity for this trade.

        If buy this is (+trading position quantity/-reserve currency quantity).

        If sell this is (-trading position quantity/-reserve currency quantity).
        """
        return self.get_position_quantity(), self.get_reserve_quantity()

    def get_fees_paid(self) -> USDollarAmount:
        """
        TODO: Make this functio to behave
        :return:
        """
        status = self.get_status()
        if status == TradeStatus.success:
            return self.lp_fees_paid
        elif status == TradeStatus.failed:
            return 0
        else:
            raise AssertionError(f"Unsupported trade state to query fees: {self.get_status()}")

    def get_execution_sort_position(self) -> int:
        """When this trade should be executed.

        Lower, negative, trades should be executed first.

        We need to execute sells first because we need to have cash in hand to execute buys.
        """
        if self.is_sell():
            return -self.trade_id
        else:
            return self.trade_id

    def mark_success(self, executed_at: datetime.datetime, executed_price: USDollarAmount, executed_quantity: Decimal, executed_reserve: Decimal, lp_fees: USDollarAmount, native_token_price: USDollarAmount):
        assert self.get_status() == TradeStatus.broadcasted
        assert isinstance(executed_quantity, Decimal)
        assert type(executed_price) == float, f"Received executed price: {executed_price} {type(executed_price)}"
        assert executed_at.tzinfo is None
        self.executed_at = executed_at
        self.executed_quantity = executed_quantity
        self.executed_reserve = executed_reserve
        self.executed_price = executed_price
        self.lp_fees_paid = lp_fees
        self.native_token_price = native_token_price
        self.reserve_currency_allocated = Decimal(0)

    def mark_failed(self, failed_at: datetime.datetime):
        assert self.get_status() == TradeStatus.broadcasted
        assert failed_at.tzinfo is None
        self.failed_at = failed_at


@dataclass_json
@dataclass
class TradingPosition:

    #: Runnint int counter primary key for positions
    position_id: int

    #: Trading pair this position is trading
    pair: TradingPairIdentifier

    # type: PositionType
    opened_at: datetime.datetime

    #: When was the last time this position was (re)valued
    last_pricing_at: datetime.datetime
    #: Base token price at the time of the valuation
    last_token_price: USDollarAmount
    # 1.0 for stablecoins, unless out of peg, in which case can be 0.99
    last_reserve_price: USDollarAmount

    #: Which reserve currency we are going to receive when we sell the asset
    reserve_currency: AssetIdentifier

    trades: Dict[int, TradeExecution] = field(default_factory=dict)

    closed_at: Optional[datetime.datetime] = None

    #: Timestamp when this position was moved to a frozen state
    frozen_at: Optional[datetime.datetime] = None

    last_trade_at: Optional[datetime.datetime] = None

    def __post_init__(self):
        assert self.position_id > 0
        assert self.last_pricing_at is not None
        assert self.reserve_currency is not None
        assert self.last_token_price > 0
        assert self.last_reserve_price > 0

    def is_open(self) -> bool:
        """This is an open trading position."""
        return self.closed_at is None

    def is_closed(self) -> bool:
        """This position has been closed and does not have any capital tied to it."""
        return not self.is_open()

    def is_frozen(self) -> bool:
        """This position has had a failed trade and can no longer be automatically moved around."""
        return self.frozen_at is not None

    def get_first_trade(self) -> TradeExecution:
        """Get the first trade for this position.

        Considers unexecuted trades.
        """
        return next(iter(self.trades.values()))

    def get_last_trade(self) -> TradeExecution:
        """Get the the last trade for this position.

        Considers unexecuted and failed trades.
        """
        return next(reversed(self.trades.values()))

    def is_long(self) -> bool:
        """Is this position long on the underlying base asset.

        We consider the position long if the first trade is buy.
        """
        assert len(self.trades) > 0, "Cannot determine if position is long or short because there are no trades"
        return self.get_first_trade().is_buy()

    def is_short(self) -> bool:
        """Is this position short on the underlying base asset."""
        return not self.is_long()

    def has_executed_trades(self) -> bool:
        """This position represents actual holdings and has executed trades on it.

        This will return false for positions that are still planned or have zero successful trades.
        """
        t: TradeExecution
        for t in self.trades.values():
            if t.is_success():
                return True
        return False

    def get_name(self) -> str:
        """Get human readable name for this position"""
        return f"#{self.position_id} {self.pair.base.token_symbol}-{self.pair.quote.token_symbol}"

    def get_quantity_unit_name(self) -> str:
        """Get the unit name we label the quantity in this position"""
        return f"{self.pair.base.token_symbol}"

    def get_quantity(self) -> Decimal:
        """Get the tied up token quantity in all successfully executed trades.

        Does not account for trades that are currently being executd.
        """
        return sum([t.get_position_quantity() for t in self.trades.values() if t.is_success()])

    def get_live_quantity(self) -> Decimal:
        """Get all tied up token quantity.

        If there are trades being executed, us their estimated amounts.
        """
        return sum([t.quantity for t in self.trades.values() if not t.is_failed()])

    def get_current_price(self) -> USDollarAmount:
        """Get the price of the base asset based on the latest valuation."""
        return self.last_token_price

    def get_opening_price(self) -> USDollarAmount:
        """Get the price when the position was opened."""
        assert self.has_executed_trades()
        first_trade = self.get_first_trade()
        return first_trade.executed_price

    def get_equity_for_position(self) -> Decimal:
        return sum([t.get_equity_for_position() for t in self.trades.values() if t.is_success()])

    def has_unexecuted_trades(self) -> bool:
        return any([t for t in self.trades.values() if t.is_pending()])

    def has_planned_trades(self) -> bool:
        return any([t for t in self.trades.values() if t.is_planned()])

    def get_identifier(self) -> str:
        """One trading pair may have multiple open positions at the same time."""
        return f"{self.pair.get_identifier()}-{self.position_id}"

    def get_successful_trades(self) -> List[TradeExecution]:
        """Get all trades that have been successfully executed and contribute to this position"""
        return [t for t in self.trades.values() if t.is_success()]

    def calculate_value_using_price(self, token_price: USDollarAmount, reserve_price: USDollarAmount) -> USDollarAmount:
        token_quantity = sum([t.get_equity_for_position() for t in self.trades.values() if t.is_accounted_for_equity()])
        reserve_quantity = sum([t.get_equity_for_reserve() for t in self.trades.values() if t.is_accounted_for_equity()])
        return float(token_quantity) * token_price + float(reserve_quantity) * reserve_price

    def get_value(self) -> USDollarAmount:
        """Get the position value using the latest revaluation pricing."""
        return self.calculate_value_using_price(self.last_token_price, self.last_reserve_price)

    def open_trade(self,
                   ts: datetime.datetime,
                   trade_id: int,
                   quantity: Decimal,
                   assumed_price: USDollarAmount,
                   trade_type: TradeType,
                   reserve_currency: AssetIdentifier,
                   reserve_currency_price: USDollarAmount) -> TradeExecution:
        assert self.reserve_currency.get_identifier() == reserve_currency.get_identifier(), "New trade is using different reserve currency than the position has"
        assert isinstance(trade_id, int)
        assert isinstance(ts, datetime.datetime)
        trade = TradeExecution(
            trade_id=trade_id,
            position_id=self.position_id,
            trade_type=trade_type,
            pair=self.pair,
            opened_at=ts,
            planned_quantity=quantity,
            planned_price=assumed_price,
            planned_reserve=quantity * Decimal(assumed_price) if quantity > 0 else 0,
            reserve_currency=self.reserve_currency,
        )
        self.trades[trade.trade_id] = trade
        return trade

    def has_trade(self, trade: TradeExecution):
        """Check if a trade belongs to this position."""
        if trade.position_id != self.position_id:
            return False
        return trade.trade_id in self.trades

    def can_be_closed(self) -> bool:
        """There are no tied tokens in this position."""
        return self.get_equity_for_position() == 0

    def get_total_bought_usd(self) -> USDollarAmount:
        """How much money we have used on buys"""
        return sum([t.get_value() for t in self.trades.values() if t.is_success() if t.is_buy()])

    def get_total_sold_usd(self) -> USDollarAmount:
        """How much money we have received on sells"""
        return sum([t.get_value() for t in self.trades.values() if t.is_success() if t.is_sell()])

    def get_buy_quantity(self) -> Decimal:
        """How many units we have bought total"""
        return sum([t.get_position_quantity() for t in self.trades.values() if t.is_success() if t.is_buy()])

    def get_sell_quantity(self) -> Decimal:
        """How many units we have sold total"""
        return sum([abs(t.get_position_quantity()) for t in self.trades.values() if t.is_success() if t.is_sell()])

    def get_net_quantity(self) -> Decimal:
        """The difference in the quantity of assets bought and sold to date."""
        return self.get_quantity()

    def get_average_buy(self) -> Optional[USDollarAmount]:
        """Calculate average buy price.

        :return: None if no buys
        """
        q = float(self.get_buy_quantity())
        if not q:
            return None
        return self.get_total_bought_usd() / q

    def get_average_sell(self) -> Optional[USDollarAmount]:
        """Calculate average buy price.

        :return: None if no sells
        """
        q = float(self.get_sell_quantity())
        return self.get_total_sold_usd() / q

    def get_average_price(self) -> Optional[USDollarAmount]:
        """The average price paid for all assets on the long or short side.

        :return: None if no executed trades
        """
        if self.is_long():
            return self.get_average_buy()
        else:
            return self.get_average_sell()

    def get_realised_profit_usd(self) -> Optional[USDollarAmount]:
        """Calculates the profit & loss (P&L) that has been 'realised' via two opposing asset transactions in the Position to date.

        :return: profit in dollar or None if no opposite trade made
        """
        assert self.is_long(), "TODO: Only long supported"
        if self.get_sell_quantity() == 0:
            return None
        return (self.get_average_sell() - self.get_average_buy()) * float(self.get_sell_quantity())

    def get_unrealised_profit_usd(self) -> USDollarAmount:
        """Calculate the position unrealised profit.

        Calculates the profit & loss (P&L) that has yet to be 'realised'
        in the remaining non-zero quantity of assets, due to the current
        market price.

        :return: profit in dollar
        """
        avg_price = self.get_average_price()
        if avg_price is None:
            return 0
        return (self.get_current_price() - avg_price) * float(self.get_net_quantity())

    def get_total_profit_usd(self) -> USDollarAmount:
        """Realised + unrealised profit."""
        realised_profit = self.get_realised_profit_usd() or 0
        unrealised_profit = self.get_unrealised_profit_usd() or 0
        total_profit = realised_profit + unrealised_profit
        return total_profit

    def get_total_profit_percent(self) -> float:
        """How much % we have made profit so far.

        :return: 0 if profit calculation cannot be made yet
        """
        assert self.is_long(), f"Profit pct for shorts unimplemented, got {self}, first trade was {self.get_first_trade()}"
        profit = self.get_total_profit_usd()
        bought = self.get_total_bought_usd()
        if bought == 0:
            return 0
        return profit / bought

    def get_freeze_reason(self) -> str:
        """Return the revert reason why this position is frozen."""
        assert self.is_frozen()
        return self.get_last_trade().tx_info.revert_reason


class RevalueEvent:
    """Describe how asset was revalued"""
    position_id: str
    revalued_at: datetime.datetime
    quantity: Decimal
    old_price: USDollarAmount
    new_price: USDollarAmount


@dataclass_json
@dataclass
class Portfolio:
    """Represents a trading portfolio on DEX markets.

    Multiple trading pair issue: We do not identify the actual assets we have purchased,
    but the trading pairs that we used to purchase them. Thus, each position marks
    "openness" in a certain trading pair. For example open position of WBNB-BUSD
    means we have bought X BNB tokens through WBNB-BUSD trading pair.
    But because this is DEX market, we could enter and exit through WBNB-USDT,
    WBNB-ETH, etc. and our position is not really tied to a trading pair.
    However, because all TradFi trading view the world through trading pairs,
    we keep the tradition here.
    """

    #: Each position gets it unique running counter id.
    next_position_id = 1

    #: Each trade gets it unique id as a running counter.
    #: Trade ids are unique across different positions.
    next_trade_id = 1

    #: Currently open trading positions
    open_positions: Dict[int, TradingPosition] = field(default_factory=dict)

    #: Currently held reserve assets
    reserves: Dict[str, ReservePosition] = field(default_factory=dict)

    #: Trades completed in the past
    closed_positions: Dict[int, TradingPosition] = field(default_factory=dict)

    #: Positions that have failed sells, or otherwise immovable and need manual clean up.
    #: Failure reasons could include
    #: - blockchain halted
    #: - ERC-20 token tax fees
    #: - rug pull token - transfer disabled
    frozen_positions: Dict[int, TradingPosition] = field(default_factory=dict)

    def is_empty(self):
        """This portfolio has no open or past trades or any reserves."""
        return len(self.open_positions) == 0 and len(self.reserves) == 0 and len(self.closed_positions) == 0

    def get_all_positions(self) -> Iterable[TradingPosition]:
        """Get open and closed positions."""
        return chain(self.open_positions.values(), self.closed_positions.values())

    def get_executed_positions(self) -> Iterable[TradingPosition]:
        """Get all positions with already executed trades.

        Ignore positions that are still pending - they have only planned trades.
        """
        p: TradingPosition
        for p in self.open_positions.values():
            if p.has_executed_trades():
                yield p

    def get_open_position_for_pair(self, pair: TradingPairIdentifier) -> Optional[TradingPosition]:
        assert isinstance(pair, TradingPairIdentifier)
        pos: TradingPosition
        for pos in self.open_positions.values():
            if pos.pair.get_identifier() == pair.get_identifier():
                return pos
        return None

    def get_open_quantities_by_position_id(self) -> Dict[str, Decimal]:
        """Return the current ownerships.

        Keyed by position id -> quantity.
        """
        return {p.get_identifier(): p.get_quantity() for p in self.open_positions.values()}

    def get_open_quantities_by_internal_id(self) -> Dict[int, Decimal]:
        """Return the current holdings in different trading pairs.

        Keyed by trading pair internal id -> quantity.
        """
        result = {}
        for p in self.open_positions.values():
            assert p.pair.internal_id, f"Did not have internal id for pair {p.pair}"
            result[p.pair.internal_id] = p.get_quantity()
        return result

    def open_new_position(self, ts: datetime.datetime, pair: TradingPairIdentifier, assumed_price: USDollarAmount, reserve_currency: AssetIdentifier, reserve_currency_price: USDollarAmount) -> TradingPosition:
        p = TradingPosition(
            position_id=self.next_position_id,
            opened_at=ts,
            pair=pair,
            last_pricing_at=ts,
            last_token_price=assumed_price,
            last_reserve_price=reserve_currency_price,
            reserve_currency=reserve_currency,

        )
        self.open_positions[p.position_id] = p
        self.next_position_id += 1
        return p

    def get_position_by_trading_pair(self, pair: TradingPairIdentifier) -> Optional[TradingPosition]:
        """Get open position by a trading pair smart contract address identifier.

        For Uniswap-likes we use the pool address as the persistent identifier
        for each trading pair.
        """
        for p in self.open_positions.values():
            if p.pair.pool_address.lower() == pair.pool_address.lower():
                return p
        return None

    def get_existing_open_position_by_trading_pair(self, pair: TradingPairIdentifier) -> Optional[TradingPosition]:
        """Get a position by a trading pair smart contract address identifier.

        The position must have already executed trades (cannot be planned position(.
        """
        for p in self.open_positions.values():
            if p.has_executed_trades():
                if p.pair.pool_address == pair.pool_address:
                    return p
        return None

    def get_positions_closed_at(self, ts: datetime.datetime) -> Iterable[TradingPosition]:
        """Get positions that were closed at a specific timestamp.

        Useful to display closed positions after the rebalance.
        """
        for p in self.closed_positions.values():
            if p.closed_at == ts:
                yield p

    def create_trade(self,
                     ts: datetime.datetime,
                     pair: TradingPairIdentifier,
                     quantity: Decimal,
                     assumed_price: USDollarAmount,
                     trade_type: TradeType,
                     reserve_currency: AssetIdentifier,
                     reserve_currency_price: USDollarAmount) -> Tuple[TradingPosition, TradeExecution, bool]:

        position = self.get_position_by_trading_pair(pair)
        if position is None:
            position = self.open_new_position(ts, pair, assumed_price, reserve_currency, reserve_currency_price)
            created = True
        else:
            created = False

        trade = position.open_trade(ts, self.next_trade_id, quantity, assumed_price, trade_type, reserve_currency, reserve_currency_price)

        # Check we accidentally do not reuse trade id somehow

        self.next_trade_id += 1

        return position, trade, created

    def get_current_cash(self) -> USDollarAmount:
        """Get how much reserve stablecoins we have."""
        return sum([r.get_current_value() for r in self.reserves.values()])

    def get_open_position_equity(self) -> USDollarAmount:
        """Get the value of current trading positions."""
        return sum([p.get_value() for p in self.open_positions.values()])

    def get_frozen_position_equity(self) -> USDollarAmount:
        """Get the value of trading positions that are frozen currently."""
        return sum([p.get_value() for p in self.frozen_positions.values()])

    def get_live_position_equity(self) -> USDollarAmount:
        """Get the value of current trading positions plus unexecuted trades."""
        return sum([p.get_value() for p in self.open_positions.values()])

    def get_total_equity(self) -> USDollarAmount:
        """Get the value of the portfolio based on the latest pricing."""
        return self.get_open_position_equity() + self.get_current_cash()

    def find_position_for_trade(self, trade) -> Optional[TradingPosition]:
        """Find a position tha trade belongs for."""
        return self.open_positions[trade.position_id]

    def get_reserve_position(self, asset: AssetIdentifier) -> ReservePosition:
        return self.reserves[asset.get_identifier()]

    def get_equity_for_pair(self, pair: TradingPairIdentifier) -> Decimal:
        """Return how much equity allocation we have in a certain trading pair."""
        position = self.get_position_by_trading_pair(pair)
        if position is None:
            return 0
        return position.get_equity_for_position()

    def adjust_reserves(self, asset: AssetIdentifier, amount: Decimal):
        """Remove currency from reserved"""
        reserve = self.get_reserve_position(asset)
        assert reserve, f"No reserves available for {asset}"
        assert reserve.quantity, f"Reserve quantity missing for {asset}"
        reserve.quantity += amount

    def move_capital_from_reserves_to_trade(self, trade: TradeExecution, underflow_check=True):
        """Allocate capital from reserves to trade instance.

        Total equity of the porfolio stays the same.
        """
        assert trade.is_buy()

        reserve = trade.get_planned_reserve()
        available = self.reserves[trade.reserve_currency.get_identifier()].quantity

        # Sanity check on price calculatins
        assert abs(float(reserve) - trade.get_planned_value()) < 0.01, f"Trade {trade}: Planned value {trade.get_planned_value()}, but wants to allocate reserve currency for {reserve}"

        if underflow_check:
            if available < reserve:
                raise NotEnoughMoney(f"Not enough reserves. We have {available}, trade wants {reserve}")

        trade.reserve_currency_allocated = reserve
        self.adjust_reserves(trade.reserve_currency, -reserve)

    def return_capital_to_reserves(self, trade: TradeExecution, underflow_check=True):
        """Return capital to reserves after a sell."""
        assert trade.is_sell()
        self.adjust_reserves(trade.reserve_currency, +trade.executed_reserve)

    def has_unexecuted_trades(self) -> bool:
        """Do we have any trades that have capital allocated, but not executed yet."""
        return any([p.has_unexecuted_trades() for p in self.open_positions])

    def update_reserves(self, new_reserves: List[ReservePosition]):
        """Update current reserves.

        Overrides current amounts of reserves.

        E.g. in the case users have deposited more capital.
        """

        assert not self.has_unexecuted_trades(), "Updating reserves while there are trades in progress will mess up internal account"

        for r in new_reserves:
            self.reserves[r.get_identifier()] = r

    def check_for_nonce_reuse(self, nonce: int):
        """A helper assert to see we are not generating invalid transactions somewhere.

        :raise: AssertionError
        """
        for p in self.get_all_positions():
            for t in p.trades.values():
                if t.tx_info:
                    assert t.tx_info.nonce != nonce, f"Nonce {nonce} is already being used by trade {t} with txinfo {t.tx_info}"

    def revalue_positions(self, ts: datetime.datetime, valuation_method: Callable, revalue_frozen=True):
        """Revalue all open positions in the portfolio.

        Reserves are not revalued.

        :param revalue_frozen: Revalue frozen positions as well
        """
        for p in self.open_positions.values():
            ts, price = valuation_method(ts, p)
            assert ts.tzinfo is None
            p.last_pricing_at = ts
            p.last_token_price = price

        if revalue_frozen:
            for p in self.frozen_positions.values():
                ts, price = valuation_method(ts, p)
                assert ts.tzinfo is None
                p.last_pricing_at = ts
                p.last_token_price = price

    def get_default_reserve_currency(self) -> Tuple[AssetIdentifier, USDollarAmount]:
        """Gets the default reserve currency associated with this state.

        For strategies that use only one reserve currency.
        This is the first in the reserve currency list.
        """

        assert len(self.reserves) > 0, "Portfolio has no reserve currencies"

        res_pos = next(iter(self.reserves.values()))
        return res_pos.asset, res_pos.reserve_token_price

    def get_all_trades(self) -> Iterable[TradeExecution]:
        """Iterate through all trades: completed, failed and in progress"""
        pos: TradingPosition
        for pos in self.open_positions.values():
            for t in pos.trades.values():
                yield t
        for pos in self.closed_positions.values():
            for t in pos.trades.values():
                yield t


@dataclass_json
@dataclass
class State:
    """The current state of the trading strategy execution."""

    portfolio: Portfolio = field(default_factory=Portfolio)

    #: Strategy can store its internal thinking over different signals
    strategy_thinking: dict = field(default_factory=dict)

    #: Assets that the strategy is not allowed to touch,
    #: or have failed to trade in the past, resulting to a frozen position.
    #: Besides this internal black list, the executor can have other blacklists
    #: based on the trading universe and these are not part of the state.
    #: The main motivation of this list is to avoid assets that caused a freeze in the future.
    #: Key is Ethereum address, lowercased.
    asset_blacklist: Set[str] = field(default_factory=set)

    def is_empty(self) -> bool:
        """This state has no open or past trades or reserves."""
        return self.portfolio.is_empty()

    def is_good_pair(self, pair: TradingPairIdentifier) -> bool:
        """Check if the trading pair is blacklisted."""
        assert isinstance(pair, TradingPairIdentifier)
        return (pair.base.get_identifier() not in self.asset_blacklist) and (pair.quote.get_identifier() not in self.asset_blacklist)

    def create_trade(self,
                     ts: datetime.datetime,
                     pair: TradingPairIdentifier,
                     quantity: Decimal,
                     assumed_price: USDollarAmount,
                     trade_type: TradeType,
                     reserve_currency: AssetIdentifier,
                     reserve_currency_price: USDollarAmount) -> Tuple[TradingPosition, TradeExecution, bool]:
        """Creates a request for a new trade.

        If there is no open position, marks a position open.

        :return: Tuple position, trade, was a new position created
        """
        position, trade, created = self.portfolio.create_trade(ts, pair, quantity, assumed_price, trade_type, reserve_currency, reserve_currency_price)
        return position, trade, created

    def start_execution(self, ts: datetime.datetime, trade: TradeExecution, txid: str, nonce: int):
        """Update our balances and mark the trade execution as started.

        Called before a transaction is broadcasted.
        """

        assert trade.get_status() == TradeStatus.planned

        position = self.portfolio.find_position_for_trade(trade)
        assert position, f"Trade does not belong to an open position {trade}"

        self.portfolio.check_for_nonce_reuse(nonce)

        if trade.is_buy():
            self.portfolio.move_capital_from_reserves_to_trade(trade)

        trade.started_at = ts

        trade.txid = txid
        trade.nonce = nonce

    def mark_broadcasted(self, broadcasted_at: datetime.datetime, trade: TradeExecution):
        """"""
        assert trade.get_status() == TradeStatus.started
        trade.broadcasted_at = broadcasted_at

    def mark_trade_success(self, executed_at: datetime.datetime, trade: TradeExecution, executed_price: USDollarAmount, executed_amount: Decimal, executed_reserve: Decimal, lp_fees: USDollarAmount, native_token_price: USDollarAmount):
        """"""

        position = self.portfolio.find_position_for_trade(trade)

        if trade.is_buy():
            assert executed_amount > 0
        else:
            assert executed_reserve > 0, f"Executed amount must be negative for sell, got {executed_amount}, {executed_reserve}"
            assert executed_amount < 0

        trade.mark_success(executed_at, executed_price, executed_amount, executed_reserve, lp_fees, native_token_price)

        if trade.is_sell():
            self.portfolio.return_capital_to_reserves(trade)

        if position.can_be_closed():
            # Move position to closed
            position.closed_at = executed_at
            del self.portfolio.open_positions[position.position_id]
            self.portfolio.closed_positions[position.position_id] = position

    def mark_trade_failed(self, failed_at: datetime.datetime, trade: TradeExecution):
        """Unroll the allocated capital."""
        trade.mark_failed(failed_at)
        self.portfolio.adjust_reserves(trade.reserve_currency, trade.reserve_currency_allocated)

    def update_reserves(self, new_reserves: List[ReservePosition]):
        self.portfolio.update_reserves(new_reserves)

    def revalue_positions(self, ts: datetime.datetime, valuation_method: Callable):
        """Revalue all open positions in the portfolio.

        Reserves are not revalued.
        """
        self.portfolio.revalue_positions(ts, valuation_method)

    def blacklist_asset(self, asset: AssetIdentifier):
        """Add a asset to the blacklist."""
        address = asset.get_identifier()
        self.asset_blacklist.add(address)
