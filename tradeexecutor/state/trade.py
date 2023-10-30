"""Trade execution state info."""

import datetime
import enum
import pprint
import logging
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from typing import Optional, Tuple, List
from types import NoneType

from dataclasses_json import dataclass_json

from eth_defi.tx import AssetDelta

from tradeexecutor.ethereum.revert import clean_revert_reason_message
from tradeexecutor.state.blockhain_transaction import BlockchainTransaction
from tradeexecutor.state.identifier import TradingPairIdentifier, AssetIdentifier
from tradeexecutor.state.loan import Loan
from tradeexecutor.state.types import USDollarAmount, USDollarPrice, BPS, LeverageMultiplier
from tradeexecutor.strategy.trade_pricing import TradePricing


logger = logging.getLogger()


#: Absolute minimum units we are willing to trade regardless of an asset
#:
#: Used to catch floating point rounding errors
QUANTITY_EPSILON = Decimal(10**-18)


class TradeType(enum.Enum):
    """What kind of trade execution this was."""

    #: A normal trade with strategy decision
    rebalance = "rebalance"

    #: The trade was made because stop loss trigger reached
    stop_loss = "stop_loss"

    #: The trade was made because take profit trigger reached
    take_profit = "take_profit"

    #: This is an accounting counter trade to cancel a broken trade.
    #:
    #: - The original trade is marked as repaied
    #:
    #: - This trade contains any reverse accounting variables needed to fix the position total
    repair = "repair"

    #: Internal state balances are updated to match on-chain balances
    accounting_correction = "accounting_correction"


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

    #: This trade was originally failed, but later repaired.
    #:
    #: A counter entry was made in the position and this trade was marked as repaired.
    repaired = "repaired"

    #: A virtual trade to reverse any balances of a repaired trade.
    #:
    repair_entry = "repair_entry"


@dataclass_json
@dataclass()
class TradeExecution:
    """Trade execution tracker. 
    
    - One TradeExecution instance can only represent one swap

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

    #: Trade id is unique among all trades in the same portfolio
    trade_id: int

    #: Position id is unique among all trades in the same portfolio
    position_id: int

    #: Spot, margin, lending, etc.
    trade_type: TradeType

    #: Which trading pair this trade was for
    pair: TradingPairIdentifier

    #: What was the strategy cycle timestamp for it was created.
    #:
    #: Naive UTC timestamp.
    #:
    #: If the trade was executed by a take profit/stop loss trigger
    #: then this is the trigger timestamp (not wall clock time)
    #:
    #: Not available if the trade was done outside the strategy execution (manual trades, accounting corrections).
    #:
    #: See also
    #:
    #: - :py:attr:`started_at`
    #:
    #: - :py:meth:`get_execution_lag`
    #:
    #: - :py:meth:`get_decision_lag`
    #:
    opened_at: datetime.datetime | None

    #: Positive for buy, negative for sell.
    #:
    #: - Always accurately known for sells.
    #:
    #: - This can be zero for leveraged trades if
    #:   the trade only adjusts the collateral.
    #:
    planned_quantity: Decimal

    #: How many reserve tokens (USD) we use in this trade
    #:
    #: **For spot market**
    #:
    #: - Always a position number (only the sign of :py:attr:`planned_quantity` changes between buy/sell)
    #:
    #: - For buys, Always known accurately for buys.
    #:
    #: - For sells, an estimation based on :py:attr:`planned_price`
    #:
    #: Expressed in :py:attr:`reserve_currency`.
    #:
    #: **For leverage**
    #:
    #: - Positive: we need to move our reserves to collateral to increase margin
    #:
    #: - Negative: position exposure is reduced, so we need less collateral and are getting collateral back
    #:
    planned_reserve: Decimal

    #: What we thought the execution price for this trade would have been
    #: at the moment of strategy decision.
    #:
    #: This price includes any fees we pay for LPs,
    #: and should become executed_price if the execution is perfect.
    #:
    #: For the market price see :py:attr:`planned_mid_price`.
    planned_price: USDollarPrice

    #: Which reserve currency we are going to take.
    #: Note that pair.quote might be different from reserve currency.
    #: This is because we can do three-way trades like BUSD -> BNB -> Cake
    #: when our routing model supports this.
    reserve_currency: AssetIdentifier

    #: Planned amount of reserve currency that goes in or out to collateral.
    #:
    #: - Negative if collateral is released and added to the reserves
    #:
    #: - Positive if reserve currency is added as the collateral
    #:
    #: This is different from :py:attr:`planned_reserve`,
    #: as any collateral adjustment is done after the trade has been executed,
    #: where as :py:attr:`planned_reserve` needs to be deposited
    #: before the trade is executed.
    #:
    #: Must be `None` for spot positions.
    #:
    planned_collateral_allocation: Optional[Decimal] = None

    #: Executed amount of collateral we spend for the debt token buyback
    #:
    #: See :py:attr:`planned_collateral_allocation` for details.
    #:
    executed_collateral_allocation: Optional[Decimal] = None

    #: Planned amount of collateral that we use to pay back the debt
    #:
    #: - Negative if collateral is spend to pay back the debt
    #:
    #: - Positive if reserve currency is added as the collateral
    #:
    #: Must be `None` for spot positions.
    #:
    planned_collateral_consumption: Optional[Decimal] = None

    #: Planned amount of collateral that we use to pay back the debt
    #:
    #: #: :py:attr:`planned_collateral_consumption` for details.
    #:
    executed_collateral_consumption: Optional[Decimal] = None

    #: What is the reserve currency exchange rate used for this trade
    #:
    #: - Access using :py:attr:`get_reserve_currency_exchange_rate`.
    #:
    #: - USDC/USD exchange rate.
    #:
    #: - If not set (legacy) assume 1.0 reset assets / USD
    #:
    reserve_currency_exchange_rate: Optional[USDollarPrice] = None

    #: What we thought was the mid-price when we made the decision to tale this trade
    #:
    #: This is the market price of the asset at the time of the trade decision.
    planned_mid_price: Optional[USDollarPrice] = None

    #: How much slippage we could initially tolerate,
    #: 0.01 is 1% slippage.
    planned_max_slippage: Optional[BPS] = None

    #: When this trade was decided
    #:
    #: Wall clock time.
    #:
    #: For backtested trades, this is always set to
    #: opened_at.
    started_at: Optional[datetime.datetime] = None

    #: How much reserves was moved on this trade before execution
    reserve_currency_allocated: Optional[Decimal] = None

    #: When this trade entered mempool
    broadcasted_at: Optional[datetime.datetime] = None

    #: Timestamp of the block where the txid was first mined.
    #:
    #: For failed trades, this is not set until repaired,
    #: but instead we set :py:attr:`failed_at`.
    executed_at: Optional[datetime.datetime] = None

    #: The trade did not go through.
    #: The timestamp when we figured this out.
    failed_at: Optional[datetime.datetime] = None

    #: What was the actual price we received
    #:
    #: This may be `None` for even if :py:attr:`closed_at` is set.
    #: This is because invalid trades (execution failed) may be marked
    #: close.
    #:
    #: Set in :py:meth:`tradeexecutor.state.State.mark_trade_success`.
    #:
    executed_price: Optional[USDollarPrice] = None

    #: How much underlying token we traded, the actual realised amount.
    #: Positive for buy, negative for sell.
    #:
    executed_quantity: Optional[Decimal] = None

    #: How much reserves we spend for this traded, the actual realised amount.
    #:
    #: Always positive.
    #:
    #: - In the case of spot buy, this is how much reserve currency
    #:   we spent to buy tokens
    #:
    #: - In the case of sell, this is how much reserve currency
    #:   we will receive after the sell is closed
    #:
    executed_reserve: Optional[Decimal] = None

    #: Slippage tolerance for this trade.
    #:
    #: Examples
    #:
    #: - `0`: no slippage toleranc eallowed at all
    #:
    #: - `0.01`: 1% slippage tolerance
    #:
    #: - `1`: no slippage tolerance. MEV bots can steal all your money
    #:
    #: We estimate `executed_quantity = planned_quantity * slippage_tolerance`.
    #: If any trade outcome exceeds the slippage tolerance the trade fails.
    #:
    #: If you are usinga vault-based trading, slippage tolerance must be always set
    #: to calculate the asset delta.
    #:
    #: See also :py:meth:`calculate_asset_deltas`.
    #:
    slippage_tolerance: Optional[float] = None

    #: LP fee % recorded before the execution starts.
    #:
    #: Recorded as multiplier
    #:
    #: Not available in the case this is ignored
    #: in backtesting or not supported by routers/trading pairs.
    #:
    #: Used to calculate :py:attr:`lp_fees_estimated`.
    #:
    #: Sourced from Uniswap v2 router or Uniswap v3 pool information.
    #:
    fee_tier: Optional[float] = None

    #: LP fees paid, in terms of the amount in token
    #:
    #: The value is read back from the realised trade.
    #: LP fee is usually % of the trade. For Uniswap style exchanges
    #: fees are always taken from `amount in` token
    #: and directly passed to the LPs as the part of the swap,
    #: these is no separate fee information.
    lp_fees_paid: Optional[USDollarAmount] = None

    #: LP fees estimated in the USD
    #:
    #: This is set before the execution and is mostly useful
    #: for backtesting.
    lp_fees_estimated: Optional[USDollarAmount] = None

    #: What is the conversation rate between quote token and US dollar used in LP fee conversion.
    #:
    #: We set this exchange rate before the trade is started.
    #: Both `lp_fees_estimated` and `lp_fees_paid` need to use the same exchange rate,
    #: even though it would not be timestamp accurte.
    lp_fee_exchange_rate: Optional[USDollarPrice] = None

    #: USD price per blockchain native currency unit, at the time of execution
    #:
    #: Used for converting tx fees and gas units to dollars
    native_token_price: Optional[USDollarPrice] = None

    # Trade retries
    retry_of: Optional[int] = None

    #: Associated blockchain transaction details.
    #: Each trade contains 1 ... n blockchain transactions.
    #: Typically this is approve() + swap() for Uniswap v2
    #: or just swap() if we have the prior approval and approve does not need to be
    #: done for the hot wallet anymore.
    blockchain_transactions: List[BlockchainTransaction] = field(default_factory=list)

    #: Human readable notes about this trade
    #:
    #: Used to mark test trades from command line.
    #: Special case; not worth to display unless the field is filled in.
    #:
    #: See :py:meth:`add_note`.
    #:
    notes: Optional[str] = None

    #: Trade was manually repaired
    #:
    #: E.g. failed broadcast issue was fixed.
    #: Marked when the repair command is called.
    repaired_at: Optional[datetime.datetime] = None

    #: Which is the trade that this trade is repairing.
    #:
    #: This trade makes a opposing trade to the trade referred here,
    #: making accounting match again and unfreezing the position.
    #:
    #: For the repair trade
    #:
    #: - Strategy cycle is set to the original broken trade
    #:
    #: - Trade at ``repaired_trade_id`` may or may not have ``repaired_at`` set
    #:
    #: - This is used to mark position closed in accounting correction,
    #:   although the trade itself does not carry any value
    #:
    repaired_trade_id: Optional[datetime.datetime] = None

    #: Related TradePricing instance
    #:
    #: TradePricing instance can refer to more than one swap
    price_structure: Optional[TradePricing] = None

    #: TradePricing after the trade was executed.
    #:
    #: Collected by :py:class:`StrategyRunner` after all trades have been executed.
    #:
    #: Mostly interesting on failed trades, so we can diagnose why they might have failed
    #: due to slippage tolerance.
    #:
    #: For backtesting, this data is filled in, but is not meaningful.
    #:
    post_execution_price_structure: Optional[TradePricing] = None

    #: Cost of gas in terms of native token
    #:
    #: E.g. on Ethereum, this is the amount of ETH spent on gas
    cost_of_gas: Optional[Decimal] = None

    #: The total porfolio value when this position was opened
    #:
    #: Used for risk metrics and other statistics.
    portfolio_value_at_creation: Optional[USDollarAmount] = None

    #: Leverage factor used when opening a position
    #:
    leverage: Optional[LeverageMultiplier] = None

    #: New position loan parameters that will become effective if this trade executes
    #:
    #: If the trade execution fails then the loan parameters do not change
    #:
    planned_loan_update: Optional[Loan] = None

    #: New position loan parameters that become effective if this trade executes
    #:
    #: Because of slippage, etc. this can differ from :py:attr:`planned_loan_update`
    #:
    executed_loan_update: Optional[Loan] = None

    #: Closing flag set on leveraged positions.
    #:
    #: After this trade is executed, any position
    #: collateral should return to the cash reserves.
    #:
    closing: Optional[bool] = None

    #: How much interest we have claimed from collateral for this position.
    #:
    #: - For interest bearing positions, how much of accrued interest was moved to reserves in this trade.
    #:
    #: - Concerns only collateral, not borrow
    #:
    #: - Borrow interest is closed in the final trade (trade size == position quantity
    #:   == principal + interest)
    #:
    #: This is the realised interest. Any accured interest
    #: must be realised by claiming it in some trade.
    #: Usually this is the trade that closes the position.
    #: See `mark_trade_success` for the logic.
    #:
    #: Expressed in reserve tokens.
    #:
    #: See also :py:attr:`paid_interest`
    #:
    #: TODO This must be converted to planned_paid_interest / executed_paid_interest,
    #: because the actual amount may change after execution.
    #:
    claimed_interest: Optional[Decimal] = None

    #: How much interest this trade paid back on the debt.
    #:
    #: Expressed in base token quantity.
    #:
    #: This is used to mark the token debt in the last trade
    #: that closes a leveraged position.
    #:
    #: See also :py:attr:`claimed_interest`
    #:
    #: TODO This must be converted to planned_paid_interest / executed_paid_interest
    #: because the actual amount may change after execution.
    #:
    paid_interest: Optional[Decimal] = None

    #: The DEX name where this trade was executed.
    #:
    #: A human-readable debugging hint.
    #: :py:attr:`pair` will contain the exchange address itself.
    #:
    #: Optional, to be more used in the future versions.
    #:
    exchange_name: Optional[str] = None

    def __repr__(self) -> str:
        if self.is_spot():
            if self.is_buy():
                return f"<Buy #{self.trade_id} {self.planned_quantity} {self.pair.base.token_symbol} at {self.planned_price}, {self.get_status().name} phase>"
            else:
                return f"<Sell #{self.trade_id} {abs(self.planned_quantity)} {self.pair.base.token_symbol} at {self.planned_price}, {self.get_status().name} phase>"
        elif self.is_short():

                if self.planned_quantity < 0:
                    kind = "Increase"
                else:
                    kind = "Reduce"

                underlying = self.pair.get_pricing_pair()

                return f"<{kind} short #{self.trade_id} \n" \
                       f"   {self.planned_quantity} {underlying.base.token_symbol} at {self.planned_price} USD, {self.get_status().name} phase\n" \
                       f"   collateral consumption: {self.planned_collateral_consumption} {underlying.quote.token_symbol}, collateral allocation: {self.planned_collateral_allocation} {underlying.quote.token_symbol}\n" \
                       f"   reserve: {self.planned_reserve}\n" \
                       f"   >"
        else:
            if self.is_buy():
                return f"<Supply credit #{self.trade_id} \n" \
                       f"   {self.planned_quantity} {self.pair.base.token_symbol} at {self.planned_price}, {self.get_status().name} phase\n" \
                       f"   >"
            else:
                return f"<Recall credit collateral #{self.trade_id} \n" \
                       f"   {self.planned_quantity} {self.pair.base.token_symbol} at {self.planned_price}, {self.get_status().name} phase\n" \
                       f"   >"

    def pretty_print(self) -> str:
        """Get diagnostics output for the trade.

        Use Python `pprint` module.
        """
        d = asdict(self)
        return pprint.pformat(d)

    def get_debug_dump(self) -> str:
        """Return class contents for logging.

        :return:
            Indented JSON-like content
        """
        return pprint.pformat(asdict(self), width=160)

    def __hash__(self):
        # TODO: Hash better?
        return hash(self.trade_id)

    def __eq__(self, other):
        """Note that we do not support comparison across different portfolios ATM."""
        assert isinstance(other, TradeExecution)
        return self.trade_id == other.trade_id

    def __post_init__(self):

        assert self.trade_id > 0

        if self.trade_type != TradeType.repair and not self.is_credit_based():
            # Leveraged trade can have quantity in zero,
            # if they just adjust the collateral amount
            assert self.planned_quantity != 0

            # TODO: We have additional check in open_position()
            assert abs(self.planned_quantity) > QUANTITY_EPSILON, f"We got a planned quantity that does not look like a good number: {self.planned_quantity}, trade is: {self}"

        assert self.planned_price > 0

        if not self.is_credit_based():
            assert self.planned_reserve >= 0, "Spot market trades must have planned reserve position"

        assert type(self.planned_price) in {float, int}, f"Price was given as {self.planned_price.__class__}: {self.planned_price}"
        assert self.opened_at.tzinfo is None, f"We got a datetime {self.opened_at} with tzinfo {self.opened_at.tzinfo}"

        if self.slippage_tolerance:
            # Sanity check
            assert self.slippage_tolerance < 0.15, f"Cannot set slippage tolerance more than 15%, got {self.slippage_tolerance * 100}"
        
    @property
    def strategy_cycle_at(self) -> datetime.datetime:
        """Alias for :py:attr:`opened_at`.

        - Get the timestamp of the strategy cycle this trade is related to

        - If this trade is outside the strategy cycle, then it is wall clock time
        """
        return self.opened_at
    
    @property
    def fee_tier(self) -> (float | None):
        """LP fee % recorded before the execution starts.
        
        :return:
            float (fee multiplier) or None if no fee was provided.

        """
        return self._fee_tier

    @fee_tier.setter
    def fee_tier(self, value):
        """Setter for fee_tier.
        Ensures fee_tier is a float"""
        
        if type(value) is property:
            # hack
            # See comment on this post: https://florimond.dev/en/posts/2018/10/reconciling-dataclasses-and-properties-in-python/
            value = None
        
        assert (type(value) in {float, NoneType}) or (value == 0), "If fee tier is specified, it must be provided as a float to trade execution"

        if value is None and (self.pair.fee or self.pair.fee == 0):
            assert type(self.pair.fee) == float, f"trading pair fee not in float format, got {self.pair.fee} ({type(self.pair.fee)}"
            # Low verbosity as otherwise this message is filling test logs
            logger.debug("No fee_tier provided but fee was found on associated trading pair, using trading pair fee")
            self._fee_tier = self.pair.fee
        else:
            self._fee_tier = value

    @property
    def lp_fees_estimated(self) -> float:
        """LP fees estimated in the USD.
        This is set before the execution and is mostly useful
        for backtesting.
        """
        return self._lp_fees_estimated

    @lp_fees_estimated.setter
    def lp_fees_estimated(self, value):
        """Setter for lp_fees_estimated"""
        
        if type(value) is property:
            # hack
            # See comment on this post: https://florimond.dev/en/posts/2018/10/reconciling-dataclasses-and-properties-in-python/
            value = None
        
        # TODO standardize
        # assert type(value) == float, f"Received lp_fees_estimated: {value} - {type(value)}"
        self._lp_fees_estimated = value
    
    def get_human_description(self) -> str:
        """User friendly description for this trade"""
        if self.is_buy():
            return f"Buy {self.planned_quantity} {self.pair.base.token_symbol} <id:{self.pair.base.internal_id}> at {self.planned_price}"
        else:
            return f"Sell {abs(self.planned_quantity)} {self.pair.base.token_symbol} <id:{self.pair.base.internal_id}> at {self.planned_price}"

    def get_reserve_currency_exchange_rate(self) -> USDollarPrice:
        """What was the reserve stablecoin exchange trade for this trade.

        :return:
            1.0 if not set
        """
        return self.reserve_currency_exchange_rate or 1.0

    def get_sign(self) -> int:
        """Return the sign of the trade.

        - ``1`` for buy

        - ``-1` for sell
        """
        sign = 1 if self.get_position_quantity() > 0 else -1
        return sign

    def is_sell(self) -> bool:
        """Trade is considered sell or short if planned quantity is set negative.

        For some of the repaired trades the quantity is set to zero,
        just to create accounting reference when correcting the total balances.
        These trades are considered sell trades.
        """
        return self.planned_quantity <= 0

    def is_buy(self) -> bool:
        """Trade is considered spot buy or long if the planned quantity is positive.
        """
        return self.planned_quantity > 0

    def is_success(self) -> bool:
        """This trade was succcessfully completed."""
        return self.executed_at is not None

    def is_failed(self) -> bool:
        """This trade was succcessfully completed."""
        return (self.failed_at is not None) and (self.repaired_at is None)

    def is_pending(self) -> bool:
        """This trade was succcessfully completed."""
        return self.get_status() in (TradeStatus.started, TradeStatus.broadcasted)

    def is_planned(self) -> bool:
        """This trade is still in planning, unallocated."""
        return self.get_status() in (TradeStatus.planned,)

    def is_started(self) -> bool:
        """This trade has a txid allocated."""
        return self.get_status() in (TradeStatus.started,)

    def is_rebalance(self) -> bool:
        """This trade is part of the normal strategy rebalance."""
        return self.trade_type == TradeType.rebalance

    def is_stop_loss(self) -> bool:
        """This trade is made to close stop loss on a position."""
        return self.trade_type == TradeType.stop_loss

    def is_take_profit(self) -> bool:
        """This trade is made to close take profit on a position."""
        return self.trade_type == TradeType.take_profit

    def is_triggered(self) -> bool:
        """Was this trade based on a trigger signal."""
        return self.trade_type == TradeType.take_profit or self.trade_type == TradeType.stop_loss

    def is_accounted_for_equity(self) -> bool:
        """Does this trade contribute towards the trading position equity.

        Failed trades are reverted. Only their fees account.
        """
        return self.get_status() in (TradeStatus.started, TradeStatus.broadcasted, TradeStatus.success)

    def is_unfinished(self) -> bool:
        """We could not confirm this trade back from the blockchain after broadcasting."""
        return self.get_status() in (TradeStatus.broadcasted,)

    def is_repaired(self) -> bool:
        """The automatic execution failed and this was later repaired.

        A manual repair command was issued and it manaeged to correctly repair this trade
        and underlying transactions.

        See also :py:meth:`is_repair_trade`.
        """
        return self.repaired_at is not None

    def is_repair_trade(self) -> bool:
        """This trade repairs another trade in the same position.

        See also :py:meth:`is_repair_trade`.
        """
        return self.repaired_trade_id is not None

    def is_executed(self) -> bool:
        """Did this trade ever execute."""
        return self.executed_at is not None

    def is_repair_needed(self) -> bool:
        """This trade needs repair, but is not repaired yet."""
        return self.is_failed() and not self.is_repaired()

    def is_repair_trade(self) -> bool:
        """This trade is fixes a frozen position and counters another trade.
        """
        return self.repaired_trade_id is not None

    def is_repair_trade(self) -> bool:
        """This trade is fixes a frozen position and counters another trade.
        """
        return self.repaired_trade_id is not None

    def is_accounting_correction(self) -> bool:
        """This trade is an accounting correction.

        This is a virtual trade made to have the
        """
        self.balance_update_id is not None

    def is_leverage(self) -> bool:
        """This is margined trade."""
        return self.pair.kind.is_leverage()

    def is_credit_based(self) -> bool:
        """This trade uses loans"""
        return self.pair.kind.is_credit_based()

    def is_credit_supply(self) -> bool:
        """This is a credit supply trade."""
        return self.pair.kind.is_credit_supply()

    def is_spot(self) -> bool:
        """This is a spot marget trade."""
        return not self.is_credit_based()

    def is_short(self) -> bool:
        """This is margined short trade."""
        return self.pair.kind.is_shorting()

    def is_long(self) -> bool:
        """This is margined long trade."""
        return self.pair.kind.is_longing()

    def is_reduce(self) -> bool:
        """This trade decreases the exposure of existing leveraged position."""
        if self.is_short():
            return self.planned_quantity > 0
        elif self.is_long():
            return self.planned_quantity < 0
        else:
            raise NotImplementedError(f"Not leveraged trade: {self}")

    def get_input_asset(self) -> AssetIdentifier:
        """How we fund this trade."""
        assert self.is_spot()
        if self.is_buy():
            return self.pair.quote
        else:
            return self.pair.base

    def get_output_asset(self) -> AssetIdentifier:
        """What asset we receive out from this trade"""
        assert self.is_spot()
        if self.is_buy():
            return self.pair.base
        else:
            return self.pair.quote

    def get_status(self) -> TradeStatus:
        """Resolve the trade status.

        Based on the different state variables set on this item,
        figure out what is the best status for this trade.
        """
        if self.repaired_trade_id:
            # Bookkeeping trades are only internal and thus always success
            return TradeStatus.success
        elif self.repaired_at:
            return TradeStatus.repaired
        elif self.failed_at:
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
        """Estimate the USD value of this trade.

        Based on the

        - Exact quantity and exact crypto price we got executed at

        - USD exchange rate known at the time of the execution

        :return:
            0.0 if the trade is not executed, or repaired without proper quantity

        """
        # self.executed_price can be zero for frozen positions,
        # but the position may still be closed (self.closed_at set)

        executed_quantity = self.executed_quantity or 0
        executed_price = self.executed_price or 0

        return abs(float(executed_quantity) * float(executed_price))

    def get_planned_value(self) -> USDollarAmount:
        """How much we plan to swap in this trade."""
        return abs(self.planned_price * float(abs(self.planned_quantity)))

    def get_planned_reserve(self) -> Decimal:
        return self.planned_reserve

    def get_raw_planned_reserve(self) -> int:
        """Return the amount of USD token for the buy as raw token units."""
        return self.reserve_currency.convert_to_raw_amount(self.planned_reserve)

    def get_raw_planned_quantity(self) -> int:
        """Return the amount of USD token for the buy as raw token units."""
        return self.pair.base.convert_to_raw_amount(self.planned_quantity)

    def get_allocated_value(self) -> USDollarAmount:
        return self.reserve_currency_allocated

    def get_position_quantity(self) -> Decimal:
        """Get the planned or executed quantity of the base token.

        - Positive for buy, negative for sell.

        - Return executed quantity if the trade has been done

        - Otherwise return planned quantity
        """

        if self.repaired_trade_id or self.repaired_at:
            # Repaired trades are shortcuted to zero
            return Decimal(0)
        elif self.executed_quantity is not None:
            return self.executed_quantity
        else:
            return self.planned_quantity

    def get_reserve_quantity(self) -> Decimal:  
        """Get the planned or executed quantity of the quote token.

        Negative for buy, positive for sell.
        """
        if self.repaired_trade_id or self.repaired_at:
            # Repaired trades are shortctted to zero
            return Decimal(0)
        elif self.executed_reserve is not None:
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

        if self.repaired_at:
            # Repaired trades have their value set to zero
            return 0.0
        elif self.executed_at:
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

    def get_claimed_interest(self) -> USDollarAmount:
        """How much US interest we have claimed in this trade.

        - Interest can be only claimed on collateral

        - Borrow interest is paid back in the final trade
        """
        if not self.claimed_interest:
            return 0.0
        return float(self.claimed_interest) * self.reserve_currency_exchange_rate

    def get_repaid_interest(self) -> USDollarAmount:
        """How much USD interest payments this trade made.

        - Interest can be only claimed on collateral

        - Borrow interest is paid back in the final trade
        """
        if not self.paid_interest:
            return 0.0

        assert self.executed_price, f"executed_price missing: {self}"

        return float(self.paid_interest) * self.executed_price

    def get_fees_paid(self) -> USDollarAmount:
        """
        Get total swap fees paid for trade. Returns 0 instead of `None`
        
        :return: total amount of lp fees (swap fees) paid in US dollars
        """
        
        status = self.get_status()
        if status == TradeStatus.success:
            return self.lp_fees_paid or 0
        elif status == TradeStatus.failed:
            return 0
        else:
            raise AssertionError(f"Unsupported trade state to query fees: {self.get_status()}")

    def get_execution_sort_position(self) -> int:
        """When this trade should be executed.

        - Closing trades should go first

        - Selling trades should go first

        We need to execute sells first because we need to have cash in hand to execute buys.

        :return:
            Sortable int
        """

        if self.closing:
            # Magic number
            return -self.trade_id - 100_000_000
        elif self.is_sell():
            return -self.trade_id
        else:
            return self.trade_id

    def get_decision_lag(self) -> datetime.timedelta:
        """How long it took between strategy decision cycle starting and the strategy to make a decision."""
        return self.started_at - self.opened_at

    def get_execution_lag(self) -> Optional[datetime.timedelta]:
        """How long it took between strategy decision cycle starting and the trade executed."""
        if self.started_at and self.opened_at:
            return self.started_at - self.opened_at
        return None

    def get_failed_transaction(self) -> Optional[BlockchainTransaction]:
        """Get the transaction that failed this trade.

        Extract the EVM revert reason from the underlying transactions.

        - Each trade can consist of multiple transactions

        - Iterate transactions from the last to first and return the first failure reason
        """
        for tx in reversed(self.blockchain_transactions):
            if tx.is_reverted():
                return tx

        return None

    def get_revert_reason(self) -> Optional[str]:
        """Get the transaction failure reason.

        Extract the EVM revert reason from the underlying transactions.

        - Each trade can consist of multiple transactions

        - Iterate transactions from the last to first and return the first failure reason

        See also

        - :py:func:`tradeexecutor.ethereum.revert.clean_revert_reason_message`.

        :return:
            Cleaned revert reason message
        """
        tx = self.get_failed_transaction()
        if tx:
            return clean_revert_reason_message(tx.revert_reason)

        return None

    def get_volume(self) -> USDollarAmount:
        """Get the volume this trade generated.

        If the trade is not yet executed,
        or failed to execute, assume zero volume.
        """
        if self.is_success():
            return self.get_executed_value()
        return 0

    def mark_broadcasted(
            self,
            broadcasted_at: datetime.datetime,
            rebroadcast=False,
    ):
        """Mark this trade to enter a brodcast phase.

        - Move the trade to the broadcasting phase

        - Aftr this, the execution model will attempt to perform blockchain txs
          and read the results back

        :param broadcasted_at:
            Wall-clock time

        :param rebroadcast:
            Is this a second attempt to try to get the tx to the chain

        """
        if not rebroadcast:
            assert self.get_status() == TradeStatus.started, f"Trade in bad state: {self.get_status()}"
        self.broadcasted_at = broadcasted_at

    def mark_success(self,
                     executed_at: datetime.datetime,
                     executed_price: USDollarAmount,
                     executed_quantity: Decimal,
                     executed_reserve: Decimal,
                     lp_fees: USDollarAmount,
                     native_token_price: USDollarAmount,
                     force=False,
                     cost_of_gas: USDollarAmount | None = None,
                     executed_collateral_consumption: Decimal | None = None,
                     executed_collateral_allocation: Decimal | None = None,
                     ):
        """Mark trade success.

        - Called by execution engine when we get a confirmation from the blockchain our blockchain txs where good

        - Called by repair to force trades to good state

        :param force:
            Do not check if we are in the correct previous state (broadcasted).

            Used in the accounting corrections.

        :param cost_of_gas:
            How much we paid for gas for this trade in terms of native token e.g. Eth in Ethereum.
        """
        if not force:
            assert self.get_status() == TradeStatus.broadcasted, f"Cannot mark trade success if it is not broadcasted. Current status: {self.get_status()}"
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
        self.cost_of_gas = cost_of_gas
        self.executed_collateral_allocation = executed_collateral_allocation
        self.executed_collateral_consumption = executed_collateral_consumption

    def mark_failed(self, failed_at: datetime.datetime):
        assert self.get_status() == TradeStatus.broadcasted
        assert failed_at.tzinfo is None
        self.failed_at = failed_at

    def set_blockchain_transactions(self, txs: List[BlockchainTransaction]):
        """Set the physical transactions needed to perform this trade."""
        assert not self.blockchain_transactions
        self.blockchain_transactions = txs

    def get_planned_max_gas_price(self) -> int:
        """Get the maximum gas fee set to all transactions in this trade."""
        return max([t.get_planned_gas_price() for t in self.blockchain_transactions])

    def calculate_asset_deltas(self) -> List[AssetDelta]:
        """Get the expected amount fo token balance change in a wallet for this trade.

        Needed for vault based trading to work, as they run
        slippage tolerance checks on expectd inputs and outputs.

        Each trade has minimum of two asset deltas

        - The token you spent to buu/sell (input)

        - The token you receive (output)

        The output will always have :py:attr:`slippage_tolerance` applied.
        Input is passed as is.

        :return:

            List of asset deltas [input, output]

        """

        # TODO: slippage tolerance currently ignores multihop trades

        assert self.slippage_tolerance is not None, "Slippage tolerance must be set before we can calculate_asset_deltas()"
        assert 0 <= self.slippage_tolerance <= 1.0, f"Slippage tolerance must be 0...1, got {self.slippage_tolerance}"

        if self.is_buy():
            input_asset = self.reserve_currency
            input_amount = self.planned_reserve

            output_asset = self.pair.base
            output_amount = self.planned_quantity

            assert input_amount > 0, "Buy missing input amount"
            assert output_amount > 0, "Buy missing output amount"
        else:
            input_asset = self.pair.base
            input_amount = -self.planned_quantity

            output_asset = self.reserve_currency
            output_amount = self.planned_reserve

            assert input_amount > 0, "Sell missing input amount"
            assert output_amount > 0, "Sell missing output amount"

        assert input_amount > 0
        assert output_amount > 0

        return [
            AssetDelta(input_asset.address, -input_asset.convert_to_raw_amount(input_amount)),
            AssetDelta(output_asset.address, output_asset.convert_to_raw_amount(output_amount * Decimal(1 - self.slippage_tolerance))),
        ]
    
    def get_cost_of_gas_usd(self):
        """Get the cost of gas for this trade in USD"""
        raise NotImplementedError("This method is not implemented yet since we do not store native token prices yet")
        
        # assert self.cost_of_gas, "Cost of gas must be set to work out cost of gas in USD"
        # assert self.native_token_price, "Native token price must be set to work out cost of gas in USD"
        # return self.cost_of_gas * self.native_token_price

    def get_credit_supply_reserve_change(self) -> Decimal | None:
        """How many tokens this trade takes/adds to the reserves.

        - Positive for consuming reserves and moving them to
          lending pool

        - Negative for removing lending collateral and returning
          them to the reservers

        Only negative value warrants for action,
        because capital allocation for the credit supply
        is done when the transaction is prepared.

        :return:

            ``None`` if not a credit supply trade
        """

        if not self.pair.is_credit_supply():
            return None

        return self.executed_reserve

    def add_note(self, line: str):
        """Add a new-line separated note to the trade.

        Do not remove any old notes.
        """

        if not self.notes:
            self.notes = ""

        self.notes += line + "\n"
