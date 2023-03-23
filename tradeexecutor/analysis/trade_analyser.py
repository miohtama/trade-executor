"""Analyze the trade performance of algorithm.

Calculate success/fail rate of trades and plot success distribution.

Example analysis include:

- Table: Summary of all trades

- Graph: Trade won/lost distribution

- Timeline: Analysis of each individual trades made

.. note ::

    A lot of this code has been lifted off from trading-strategy package
    where it had to deal with different trading frameworks.
    It could be simplified greatly now.

"""

import datetime
import enum
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Iterable, Optional, Tuple, Callable, Set

import numpy as np
import pandas as pd
from IPython.core.display_functions import display
from dataclasses_json import dataclass_json, config
from statistics import median

from tradeexecutor.state.position import TradingPosition
from tradeexecutor.state.portfolio import Portfolio
from tradeexecutor.state.trade import TradeExecution, TradeType
from tradeexecutor.state.types import USDollarPrice, Percent
from tradeexecutor.utils.format import calculate_percentage
from tradeexecutor.utils.timestamp import json_encode_timedelta, json_decode_timedelta
from tradingstrategy.timebucket import TimeBucket

from tradingstrategy.exchange import Exchange
from tradingstrategy.pair import PandasPairUniverse
from tradingstrategy.types import PrimaryKey, USDollarAmount
from tradingstrategy.utils.format import format_value, format_price, format_duration_days_hours_mins, \
    format_percent_2_decimals
from tradingstrategy.utils.summarydataframe import as_dollar, as_integer, create_summary_table, as_percent, as_duration, as_bars

try:
    import quantstats as qs
    HAS_QUANTSTATS = True
except Exception:
    HAS_QUANTSTATS = False


logger = logging.getLogger(__name__)


@dataclass
class SpotTrade:
    """Track spot trades to construct position performance.

    For sells, quantity is negative.
    """

    #: Internal running counter to uniquely label all trades in trade analysis
    trade_id: PrimaryKey

    #: Trading pair for this trade
    pair_id: PrimaryKey

    #: When this trade was made, the backtes simulation thick
    timestamp: pd.Timestamp

    #: Asset mid-price
    price: USDollarPrice

    executed_price: USDollarPrice

    #: How much we bought the asset. Negative value for sells.
    quantity: float

    #: How much fees we paid to the exchange
    commission: USDollarAmount

    #: How much we lost against the midprice due to the slippage
    slippage: USDollarAmount

    #: Any hints applied for this trade why it was performed
    trade_type: Optional[TradeType] = None

    #: Internal state dump of the algorithm when this trade was made.
    #: This is mostly useful when doing the trade analysis try to understand
    #: why some trades were made.
    #: It also allows you to reconstruct the portfolio state over the time.
    state_details: Optional[Dict] = None

    #: LP fees paid, currency convereted to the USD.
    #:
    #: The value is read back from the realised trade.
    #: LP fee is usually % of the trade. For Uniswap style exchanges
    #: fees are always taken from `amount in` token
    #: and directly passed to the LPs as the part of the swap,
    #: these is no separate fee information.
    lp_fees_paid: Optional[list[USDollarAmount] | USDollarAmount] = None

    #: Set for legacy trades with legacy data.
    #:
    #: Mark that calculations on this trade might be incorrect
    bad_data_issues: bool = False

    def is_buy(self):
        return self.quantity > 0

    def is_sell(self):
        return self.quantity < 0

    @property
    def value(self) -> USDollarAmount:
        return abs(self.executed_price * float(self.quantity))


@dataclass
class TradePosition:
    """How a particular asset traded.

    Each asset can have multiple entries (buys) and exits (sells)

    For a simple strategies there can be only one or two trades per position.

    * Enter (buy)

    * Exit (sell optionally)
    """

    #: Position id of the trade
    #: Used to be self.trades[0].trade_id
    position_id: int

    #: List of all trades done for this position
    trades: List[SpotTrade] = field(default_factory=list)

    #: Closing the position could be deducted from the trades themselves,
    #: but we cache it by hand to speed up processing
    opened_at: Optional[pd.Timestamp] = None

    #: Closing the position could be deducted from the trades themselves,
    #: but we cache it by hand to speed up processing
    closed_at: Optional[pd.Timestamp] = None

    #: The total dollar value of the portfolio when the position
    #: was opened
    portfolio_value_at_open: Optional[USDollarAmount] = None

    #: What is the maximum risk of this position.
    #: Risk relative to the portfolio size.
    loss_risk_at_open_pct: Optional[float] = None
    
    #: How much portfolio capital was risk when this position was opened.
    #: This is based on the opening values, any position adjustment after open is ignored
    #: Assume capital is tied to the position and we can never release it.
    #: Assume no stop loss is used, or it cannot be trigged
    capital_tied_at_open_pct: Optional[float] = None

    #: Trigger a stop loss if this price is reached,
    #:
    #: We use mid-price as the trigger price.
    stop_loss: Optional[USDollarAmount] = None

    #: Related to loss_risk_at_open_pct
    #: This is the related dollar value for maximum risk of the position
    maximum_risk: Optional[USDollarAmount] = None

    def __eq__(self, other: "TradePosition"):
        """Trade positions are unique by opening timestamp and pair id.]

        We assume there cannot be a position opened for the same asset at the same time twice.
        """
        return self.position_id == other.position_id

    def __hash__(self):
        """Allows easily create index (hash map) of all positions"""
        return hash((self.position_id))

    @property
    def pair_id(self) -> PrimaryKey:
        """Position id is the same as the opening trade id."""
        return self.trades[0].pair_id

    @property
    def duration(self) -> Optional[datetime.timedelta]:
        """How long this position was held.

        :return: None if the position is still open
        """
        if not self.is_closed():
            return None
        return self.closed_at - self.opened_at

    def is_open(self):
        return self.closed_at is None

    def is_closed(self):
        return not self.is_open()

    @property
    def open_quantity(self) -> float:
        return sum([t.quantity for t in self.trades])

    @property
    def open_value(self) -> float:
        """The current value of this open position, with the price at the time of opening."""
        assert self.is_open()
        return sum([t.value for t in self.trades])

    @property
    def open_price(self) -> float:
        """At what price we opened this position.

        Supports only simple enter/exit positions.
        """
        return self.get_first_entry_price()

    def get_first_entry_price(self) -> float:
        """What was the price when the first entry buy for this position was made.
        """
        buys = list(self.buys)
        return buys[0].price

    def get_last_exit_price(self) -> float:
        """What was the time when the last sell for this position was executd.
        """
        sells = list(self.sells)
        return sells[-1].price

    @property
    def close_price(self) -> float:
        """At what price we exited this position.

        Supports only simple enter/exit positions.
        """
        return self.get_last_exit_price()

    @property
    def buys(self) -> Iterable[SpotTrade]:
        return [t for t in self.trades if t.is_buy()]

    @property
    def sells(self) -> Iterable[SpotTrade]:
        return [t for t in self.trades if t.is_sell()]

    @property
    def buy_value(self) -> USDollarAmount:
        return sum([t.value - t.commission for t in self.trades if t.is_buy()])

    @property
    def sell_value(self) -> USDollarAmount:
        return sum([t.value - t.commission for t in self.trades if t.is_sell()])

    @property
    def realised_profit(self) -> USDollarAmount:
        """Calculated life-time profit over this position."""
        assert not self.is_open()
        return -sum([float(t.quantity) * t.executed_price - t.commission for t in self.trades])

    @property
    def realised_profit_percent(self) -> float:
        """Calculated life-time profit over this position."""
        assert not self.is_open()
        buy_value = self.buy_value
        sell_value = self.sell_value
        return sell_value / buy_value - 1

    def is_win(self):
        """Did we win this trade."""
        assert not self.is_open()
        return self.realised_profit > 0

    def is_lose(self):
        assert not self.is_open()
        return self.realised_profit < 0

    def is_stop_loss(self) -> bool:
        """Was stop loss triggered for this position"""
        for t in self.trades:
            if t.trade_type == TradeType.stop_loss:
                return True
        return False

    def is_take_profit(self) -> bool:
        """Was trake profit triggered for this position"""
        for t in self.trades:
            if t.trade_type == TradeType.take_profit:
                return True
        return False

    def add_trade(self, t: SpotTrade):
        if self.trades:
            last_trade = self.trades[-1]
            assert t.timestamp >= last_trade.timestamp, f"Tried to do trades in wrong order. Last: {last_trade}, got {t}"
        self.trades.append(t)

    def can_trade_close_position(self, t: SpotTrade):
        assert self.is_open()
        if not t.is_sell():
            return False
        open_quantity = self.open_quantity
        closing_quantity = -t.quantity
        assert closing_quantity <= open_quantity, "Cannot sell more than we have in balance sheet"
        return closing_quantity == open_quantity

    def get_max_size(self) -> USDollarAmount:
        """Get the largest size of this position over the time"""
        cur_size = 0
        max_size = 0

        if len(self.trades) > 2:
            logger.warning("Position has %d trades so this method might produce wrong result", self.trades)

        for t in self.trades:
            cur_size = t.value
            max_size = max(cur_size, max_size)
        return max_size

    def get_trade_count(self) -> int:
        """How many individual trades was done to manage this position."""
        return len(self.trades)

    def get_total_lp_fees_paid(self) -> int:
        """Get the total amount of swap fees paid in the position. Includes all trades."""
        lp_fees_paid = 0
        
        for trade in self.trades:
            if type(trade.lp_fees_paid) == list:
                lp_fees_paid += sum(filter(None,trade.lp_fees_paid))
            else:
                lp_fees_paid += trade.lp_fees_paid or 0
        
        return lp_fees_paid

    def has_bad_data_issues(self) -> bool:
        """Do we have legacy / incompatible data issues."""
        for t in self.trades:
            if t.bad_data_issues:
                return True
        return False



@dataclass
class AssetTradeHistory:
    """How a particular asset traded.

    Each position can have increments or decrements.
    When position is decreased to zero, it is considered closed, and a new buy open a new position.
    """
    positions: List[TradePosition] = field(default_factory=list)

    def get_first_opened_at(self) -> Optional[pd.Timestamp]:
        if self.positions:
            return self.positions[0].opened_at
        return None

    def get_last_closed_at(self) -> Optional[pd.Timestamp]:
        for position in reversed(self.positions):
            if not position.is_open():
                return position.closed_at

        return None

    def add_trade(
        self, t: SpotTrade, 
        position_id: int | None,
        portfolio_value_at_open: USDollarAmount | None,
        loss_risk_at_open_pct: float | None,
        capital_tied_at_open_pct: float | None,
        stop_loss: USDollarAmount | None,
        maximum_risk: USDollarAmount | None
    ):
        """Adds a new trade to the asset history.

        If there is an open position the trade is added against this,
        otherwise a new position is opened for tracking.
        """
        current_position = None
        if self.positions and self.positions[-1].is_open():
            current_position = self.positions[-1]

        if current_position:
            # For live trading

            if current_position.can_trade_close_position(t):
                # Close the existing position
                current_position.closed_at = t.timestamp
                current_position.add_trade(t)
                assert current_position.open_quantity == 0
            else:
                # Add to the existing position
                current_position.add_trade(t)
        else:
            # For backtesting
            # Open new position
            assert position_id is not None, "position id must be provided when opening a new position for backtesting"
            new_position = TradePosition(
                opened_at=t.timestamp, 
                position_id=position_id,
                portfolio_value_at_open=portfolio_value_at_open,
                loss_risk_at_open_pct=loss_risk_at_open_pct,
                capital_tied_at_open_pct=capital_tied_at_open_pct,
                stop_loss=stop_loss,
                maximum_risk=maximum_risk
            )
            new_position.add_trade(t)
            self.positions.append(new_position)


@dataclass_json
@dataclass(slots=True)
class TradeSummary:
    """Some generic statistics over all the trades"""
    won: int
    lost: int
    zero_loss: int
    stop_losses: int
    undecided: int
    realised_profit: USDollarAmount
    open_value: USDollarAmount
    uninvested_cash: USDollarAmount

    initial_cash: USDollarAmount
    extra_return: USDollarAmount
    duration: datetime.timedelta = field(metadata=config(
        encoder=json_encode_timedelta,
        decoder=json_decode_timedelta,
    ))

    average_winning_trade_profit_pc: float
    average_losing_trade_loss_pc: float
    biggest_winning_trade_pc: Optional[float]
    biggest_losing_trade_pc: Optional[float]

    average_duration_of_winning_trades: datetime.timedelta = field(metadata=config(
        encoder=json_encode_timedelta,
        decoder=json_decode_timedelta,
    ))
    average_duration_of_losing_trades: datetime.timedelta = field(metadata=config(
        encoder=json_encode_timedelta,
        decoder=json_decode_timedelta,
    ))
    time_bucket: Optional[TimeBucket] = None

    total_positions: int = field(init=False)
    win_percent: float = field(init=False)
    return_percent: float = field(init=False)
    annualised_return_percent: float = field(init=False)
    all_stop_loss_percent: float = field(init=False)
    lost_stop_loss_percent: float = field(init=False)

    all_take_profit_percent: float = field(init=False)
    won_take_profit_percent: float = field(init=False)

    average_net_profit: USDollarAmount = field(init=False)
    end_value: USDollarAmount = field(init=False)

    # used if raw_timeline is provided as argument to calculate_summary_statistics
    average_trade: Optional[float] = None
    median_trade: Optional[float] = None
    max_pos_cons: Optional[int] = None
    max_neg_cons: Optional[int] = None
    max_pullback: Optional[float] = None
    max_loss_risk: Optional[float] = None
    max_realised_loss: Optional[float] = None
    avg_realised_risk: Optional[Percent] = None

    take_profits: int = field(default=0)

    trade_volume: USDollarAmount = field(default=0.0)

    lp_fees_paid: Optional[USDollarPrice] = 0
    lp_fees_average_pc: Optional[USDollarPrice] = 0

    #: advanced users can use this property instead of the
    #: provided quantstats helper methods
    daily_returns: Optional[pd.Series] = None

    def __post_init__(self):

        self.total_positions = self.won + self.lost + self.zero_loss
        self.win_percent = calculate_percentage(self.won, self.total_positions)
        self.all_stop_loss_percent = calculate_percentage(self.stop_losses, self.total_positions)
        self.all_take_profit_percent = calculate_percentage(self.take_profits, self.total_positions)
        self.lost_stop_loss_percent = calculate_percentage(self.stop_losses, self.lost)
        self.won_take_profit_percent = calculate_percentage(self.take_profits, self.won)
        self.average_net_profit = self.realised_profit / self.total_positions if self.total_positions else None
        self.end_value = self.open_value + self.uninvested_cash
        initial_cash = self.initial_cash or 0
        self.return_percent = calculate_percentage(self.end_value - initial_cash, initial_cash)
        self.annualised_return_percent = calculate_percentage(self.return_percent * datetime.timedelta(days=365),
                                                              self.duration) if self.return_percent else None


    def to_dataframe(self) -> pd.DataFrame:
        """Convert the data to a human readable summary table.

        """
        if(self.time_bucket is not None):
            avg_duration_winning = as_bars(self.average_duration_of_winning_trades)
            avg_duration_losing = as_bars(self.average_duration_of_losing_trades)
        else:
            avg_duration_winning = as_duration(self.average_duration_of_winning_trades)
            avg_duration_losing = as_duration(self.average_duration_of_losing_trades)

        """Creates a human-readable Pandas dataframe table from the object."""

        human_data = {
            "Trading period length": as_duration(self.duration),
            "Return %": as_percent(self.return_percent),
            "Annualised return %": as_percent(self.annualised_return_percent),
            "Cash at start": as_dollar(self.initial_cash),
            "Value at end": as_dollar(self.end_value),
            "Trade volume": as_dollar(self.trade_volume),
            "Trade win percent": as_percent(self.win_percent),
            "Total positions": as_integer(self.total_positions),
            "Won trades": as_integer(self.won),
            "Lost trades": as_integer(self.lost),
            "Stop losses triggered": as_integer(self.stop_losses),
            "Stop loss % of all": as_percent(self.all_stop_loss_percent),
            "Stop loss % of lost": as_percent(self.lost_stop_loss_percent),
            "Take profits triggered": as_integer(self.take_profits),
            "Take profit % of all": as_percent(self.all_take_profit_percent),
            "Take profit % of win": as_percent(self.won_take_profit_percent),
            "Zero profit trades": as_integer(self.zero_loss),
            "Positions open at the end": as_integer(self.undecided),
            "Realised profit and loss": as_dollar(self.realised_profit),
            "Portfolio unrealised value": as_dollar(self.open_value),
            "Extra returns on lending pool interest": as_dollar(self.extra_return),
            "Cash left at the end": as_dollar(self.uninvested_cash),
            "Average winning trade profit %": as_percent(self.average_winning_trade_profit_pc),
            "Average losing trade loss %": as_percent(self.average_losing_trade_loss_pc),
            "Biggest winning trade %": as_percent(self.biggest_winning_trade_pc),
            "Biggest losing trade %": as_percent(self.biggest_losing_trade_pc),
            "Average duration of winning trades": avg_duration_winning,
            "Average duration of losing trades": avg_duration_losing,
            "LP fees paid": as_dollar(self.lp_fees_paid),
            "LP fees paid % of volume": as_percent(self.lp_fees_average_pc),
        }

        def add_prop(value, key: str, formatter: Callable):
            human_data[key] = (
                formatter(value)
                if value is not None
                else formatter(0)
            )

        add_prop(self.average_trade, 'Average trade:', as_percent)
        add_prop(self.median_trade, 'Median trade:', as_percent)
        add_prop(self.max_pos_cons, 'Consecutive wins', as_integer)
        add_prop(self.max_neg_cons, 'Consecutive losses', as_integer)
        add_prop(self.max_realised_loss, 'Biggest realized risk', as_percent)
        add_prop(self.avg_realised_risk, 'Avg realised risk', as_percent)
        add_prop(self.max_pullback, 'Max pullback of total capital', as_percent)
        add_prop(self.max_loss_risk, 'Max loss risk at opening of position', as_percent)

        df = create_summary_table(human_data)
        return df

    def show(self):
        """Render a summary table in IPython notebook."""
        with pd.option_context("display.max_row", None):
            df = self.to_dataframe()
            display(df.style.set_table_styles([{'selector': 'thead', 'props': [('display', 'none')]}]))
    
    def get_full_report(self):
        """Show basic and advanced stats and plots"""
        return (
            qs.reports.full(self.daily_returns) 
            if HAS_QUANTSTATS and self.daily_returns is not None
            else None
        )
    
    def get_basic_stats(self):
        """Show basic stats only"""
        return (
            qs.reports.metrics(self.daily_returns)
            if HAS_QUANTSTATS and self.daily_returns is not None
            else None
        )

    def get_full_stats(self):
        """Show basic and advanced stats"""
        return (
            qs.reports.metrics(self.daily_returns, mode='full')
            if HAS_QUANTSTATS and self.daily_returns is not None
            else None
        )

    def get_basic_plots(self):
        """Show basic plots"""
        return (
            qs.reports.plots(self.daily_returns)
            if HAS_QUANTSTATS and self.daily_returns is not None
            else None
        )

    def get_full_plots(self):
        """Show basic and advanced plots"""
        return (
            qs.reports.plots(self.daily_returns, mode='full')
            if HAS_QUANTSTATS and self.daily_returns is not None
            else None
        )


@dataclass
class TradeAnalysis:
    """Analysis of trades in a portfolio."""

    portfolio: Portfolio

    #: How a particular asset traded. Asset id -> Asset history mapping
    asset_histories: Dict[object, AssetTradeHistory] = field(default_factory=dict)

    def get_first_opened_at(self) -> Optional[pd.Timestamp]:
        def all_opens():
            for history in self.asset_histories.values():
                yield history.get_first_opened_at()

        return min(all_opens())

    def get_last_closed_at(self) -> Optional[pd.Timestamp]:
        def all_closes():
            for history in self.asset_histories.values():
                closed = history.get_last_closed_at()
                if closed:
                    yield closed

        return max(all_closes())

    def get_all_positions(self) -> Iterable[Tuple[PrimaryKey, TradePosition]]:
        """Return open and closed positions over all traded assets."""
        for pair_id, history in self.asset_histories.items():
            for position in history.positions:
                yield pair_id, position

    def get_open_positions(self) -> Iterable[Tuple[PrimaryKey, TradePosition]]:
        """Return open and closed positions over all traded assets."""
        for pair_id, history in self.asset_histories.items():
            for position in history.positions:
                if position.is_open():
                    yield pair_id, position

    def calculate_summary_statistics(
        self, 
        time_bucket: Optional[TimeBucket] = None,
        state = None
    ) -> TradeSummary:
        """Calculate some statistics how our trades went.

            :param time_bucket:
                Optional, used to display average duration as 'number of bars' instead of 'number of days'.

            :return:
                TradeSummary instance
        """

        if(time_bucket is not None):
            assert isinstance(time_bucket, TimeBucket), "Not a valid time bucket"

        # TODO cannot add assertion or typing for state since circular import error

        def get_avg_profit_pct_check(trades: List | None):
            return float(np.mean(trades)) if trades else None

        def get_avg_trade_duration(duration_list: List | None, time_bucket: TimeBucket | None):
            if duration_list:
                if isinstance(time_bucket, TimeBucket):
                    return np.mean(duration_list)/time_bucket.to_timedelta()
                else:
                    return np.mean(duration_list)
            else:
                return datetime.timedelta(0)

        def max_check(lst):
            return max(lst) if lst else None
        def min_check(lst):
            return min(lst) if lst else None
        def avg_check(lst):
            return avg(lst) if lst else None
        def median_check(lst):
            return median(lst) if lst else None

        initial_cash = self.portfolio.get_initial_deposit()

        uninvested_cash = self.portfolio.get_current_cash()

        # EthLisbon hack
        extra_return = 0

        duration = datetime.timedelta(0)

        winning_trades = []
        losing_trades = []
        winning_trades_duration = []
        losing_trades_duration = []
        loss_risk_at_open_pc = []
        realised_losses = []
        biggest_winning_trade_pc = None
        biggest_losing_trade_pc = None
        average_duration_of_losing_trades = datetime.timedelta(0)
        average_duration_of_winning_trades = datetime.timedelta(0)

        first_trade, last_trade = self.portfolio.get_first_and_last_executed_trade()
        if first_trade and first_trade != last_trade:
            duration = last_trade.executed_at - first_trade.executed_at

        won = lost = zero_loss = stop_losses = take_profits = undecided = 0
        open_value: USDollarAmount = 0
        profit: USDollarAmount = 0
        trade_volume = 0
        lp_fees_paid = 0

        positions = []
        position: TradePosition
        for pair_id, position in self.get_all_positions():
            
            lp_fees_paid += position.get_total_lp_fees_paid() or 0
            
            for t in position.trades:
                trade_volume += abs(float(t.quantity) * t.executed_price)


            if position.is_open():
                open_value += position.open_value
                undecided += 1
                continue

            if position.is_stop_loss():
                stop_losses += 1

            if position.is_take_profit():
                take_profits += 1

            if position.is_win():
                won += 1
                winning_trades.append(position.realised_profit_percent)
                winning_trades_duration.append(position.duration)

            elif position.is_lose():
                lost += 1
                losing_trades.append(position.realised_profit_percent)
                losing_trades_duration.append(position.duration)

                if position.portfolio_value_at_open:
                    realised_loss = position.realised_profit / position.portfolio_value_at_open
                else:
                    # Bad data
                    realised_loss = 0
                realised_losses.append(realised_loss)

            else:
                # Any profit exactly balances out loss in slippage and commission
                zero_loss += 1

            profit += position.realised_profit

            if position.stop_loss:
                loss_risk_at_open_pc.append(position.loss_risk_at_open_pct)
            else:
                loss_risk_at_open_pc.append(position.capital_tied_at_open_pct)

            positions.append(position)

        all_trades = winning_trades + losing_trades + [0 for i in range(zero_loss)]
        average_trade = avg_check(all_trades)
        median_trade = median_check(all_trades)

        average_winning_trade_profit_pc = get_avg_profit_pct_check(winning_trades)
        average_losing_trade_loss_pc = get_avg_profit_pct_check(losing_trades)

        max_realised_loss = min_check(realised_losses)
        avg_realised_risk = avg_check(realised_losses)

        max_loss_risk_at_open_pc = max_check(loss_risk_at_open_pc)

        biggest_winning_trade_pc = max_check(winning_trades)

        biggest_losing_trade_pc = min_check(losing_trades)

        average_duration_of_winning_trades = get_avg_trade_duration(winning_trades_duration, time_bucket)
        average_duration_of_losing_trades = get_avg_trade_duration(losing_trades_duration, time_bucket)

        # sort positions by position id (chronologically)
        # should be unnecessary to sort since build_trade_analysis sorts same way
        # but just in case used directly
        positions.sort(key=lambda x: x.position_id)
        max_pos_cons, max_neg_cons, max_pullback = self.get_max_consective(positions)

        lp_fees_average_pc = lp_fees_paid / trade_volume if trade_volume else 0

        # for advanced statistics
        # import here to avoid circular import error
        if state is not None and HAS_QUANTSTATS:
            from tradeexecutor.visual.equity_curve import get_daily_returns
            daily_returns = get_daily_returns(state)
        else:
            daily_returns = None

        return TradeSummary(
            won=won,
            lost=lost,
            zero_loss=zero_loss,
            stop_losses=stop_losses,
            take_profits=take_profits,
            undecided=undecided,
            realised_profit=profit + extra_return,
            open_value=open_value,
            uninvested_cash=uninvested_cash,
            initial_cash=initial_cash,
            extra_return=extra_return,
            duration=duration,
            average_winning_trade_profit_pc=average_winning_trade_profit_pc,
            average_losing_trade_loss_pc=average_losing_trade_loss_pc,
            biggest_winning_trade_pc=biggest_winning_trade_pc,
            biggest_losing_trade_pc=biggest_losing_trade_pc,
            average_duration_of_winning_trades=average_duration_of_winning_trades,
            average_duration_of_losing_trades=average_duration_of_losing_trades,
            average_trade=average_trade,
            median_trade=median_trade,
            max_pos_cons=max_pos_cons,
            max_neg_cons=max_neg_cons,
            max_pullback=max_pullback,
            max_loss_risk=max_loss_risk_at_open_pc,
            max_realised_loss=max_realised_loss,
            avg_realised_risk=avg_realised_risk,
            time_bucket=time_bucket,
            trade_volume=trade_volume,
            lp_fees_paid=lp_fees_paid,
            lp_fees_average_pc=lp_fees_average_pc,
            daily_returns=daily_returns
        )

    def create_timeline(self) -> pd.DataFrame:
        """Create a timeline feed how we traded over a course of time.

        Note: We assume each position has only one enter and exit event, not position increases over the lifetime.

        :return: DataFrame with timestamp and timeline_event columns
        """

        def gen_events():
            for pair_id, position in self.get_all_positions():
                yield (position.position_id, position)

        df = pd.DataFrame(gen_events(), columns=["position_id", "position"])
        return df

    def get_timeline_stats(self):
        """create ordered timeline of trades for stats that need it"""
        timeline = self.create_timeline()
        raw_timeline = expand_timeline_raw(timeline)

        # Max capital at risk at SL (don't confuse stop_losses and stop_loss_rows)
        # max_capital_at_risk_sl = None
        # stop_loss_rows = raw_timeline.loc[raw_timeline['Remarks'] == 'SL']
        # if (stop_loss_pct is not None) and stop_loss_rows:
        #     #raise ValueError("Missing argument: if raw_timeline is provided, then stop loss must also be provided")
        #     max_capital_at_risk_sl = max(((1-stop_loss_pct)*stop_loss_rows['position_max_size'])/stop_loss_rows['opening_capital'])

        return self.get_max_consective(raw_timeline)

    @staticmethod
    def get_max_consective(positions: List[TradePosition]) -> tuple[int, int ,int] | tuple[None, None, None]:
        """May be used in calculate_summary_statistics

        :param positions:
        An list of trading positions, ordered by opened_at time. Note, must be ordered to be correct.
        """

        if not positions:
            return None, None, None

        max_pos_cons = 0
        max_neg_cons = 0
        max_pullback_pct = 0
        pos_cons = 0
        neg_cons = 0
        pullback = 0

        for position in positions:
            realized_profit = position.realised_profit

            # don't do anything if profit = $0
            if(realized_profit > 0):
                    neg_cons = 0
                    pullback = 0
                    pos_cons += 1
            elif(realized_profit < 0):
                    pos_cons = 0
                    neg_cons += 1
                    pullback += realized_profit

            if(neg_cons > max_neg_cons):
                    max_neg_cons = neg_cons
            if(pos_cons > max_pos_cons):
                    max_pos_cons = pos_cons

            value_at_open = position.portfolio_value_at_open
            if value_at_open:
                pullback_pct = pullback / (value_at_open + realized_profit)
                if(pullback_pct < max_pullback_pct):
                        # pull back is in the negative direction
                        max_pullback_pct = pullback_pct
            else:
                # Bad input data / legacy data
                max_pullback_pct = 0

        return max_pos_cons, max_neg_cons, max_pullback_pct

class TimelineRowStylingMode(enum.Enum):
    #: Style using Pandas background_gradient
    gradient = "gradient"

    #: Simple
    #: Profit = green, loss = red
    simple = "simple"


class TimelineStyler:
    """Style the expanded trades timeline table.

    Give HTML hints for DataFrame how it should be rendered
    in the notebook output.
    """

    def __init__(self,
                 row_styling: TimelineRowStylingMode,
                 hidden_columns: List[str],
                 vmin: float,
                 vmax: float,
                 ):
        self.row_styling = row_styling
        self.hidden_columns = hidden_columns
        self.vmin = vmin
        self.vmax = vmax

    def colour_timelime_row_simple(self, row: pd.Series) -> pd.Series:
        """Set colour for each timeline row based on its profit.

        - +/- 5% colouring

        - More information: https://stackoverflow.com/a/49745352/315168

        - CSS colours: https://htmlcolorcodes.com/color-names/
        """

        pnl_raw = row["PnL % raw"]

        if pnl_raw < -0.05:
            return pd.Series('background-color: Salmon', row.index)
        elif pnl_raw < 0:
            return pd.Series('background-color: LightSalmon', row.index)
        elif pnl_raw > 0.05:
            return pd.Series('background-color: LawnGreen', row.index)
        else:
            return pd.Series('background-color: PaleGreen', row.index)

    def __call__(self, df: pd.DataFrame):
        """Applies styles on a dataframe

        :param df:
            Dataframe as returned by :py:func`expand_timeline`.
        """
        # Create a Pandas Styler with multiple styling options applied
        try:
            styles = df.style \
                .hide(axis="index") \
                .hide(axis="columns", subset=self.hidden_columns)
        except KeyError:
            # The input df was empty (no trades)
            styles = df.style

        # Don't let the text inside a cell to wrap
        styles = styles.set_table_styles({
            "Opened at": [{'selector': 'td', 'props': [('white-space', 'nowrap')]}],
            "Exchange": [{'selector': 'td', 'props': [('white-space', 'nowrap')]}],
        })

        if self.row_styling == TimelineRowStylingMode.gradient:
            # Dynamically color the background of trade outcome coluns # https://pandas.pydata.org/docs/reference/api/pandas.io.formats.style.Styler.background_gradient.html
            # TODO: This gradient styling is confusing
            # get rid of it long term
            styles = styles.background_gradient(
                axis=0,
                gmap=df['PnL % raw'],
                cmap='RdYlGn',
                vmin=self.vmin,  # We can only lose 100% of our money on position
                vmax=self.vmax)  # 50% profit is 21.5 position. Assume this is the max success color we can hit over
        else:
            styles = styles.apply(self.colour_timelime_row_simple, axis=1)

        return styles


def expand_timeline(
        exchanges: Set[Exchange],
        pair_universe: PandasPairUniverse,
        timeline: pd.DataFrame,
        vmin=-0.3,
        vmax=0.2,
        timestamp_format="%Y-%m-%d",
        hidden_columns=["Id", "PnL % raw"],
        row_styling_mode=TimelineRowStylingMode.simple,
) -> Tuple[pd.DataFrame, TimelineStyler]:
    """Expand trade history timeline to human readable table.

    This will the outputting much easier in Python Notebooks.

    Currently does not incrementing/decreasing positions gradually.

    Instaqd of applying styles or returning a styled dataframe, we return a callable that applies the styles.
    This is because of Pandas issue https://github.com/pandas-dev/pandas/issues/40675 - hidden indexes, columns,
    etc. are not exported.

    :param exchanges: Needed for exchange metadata

    :param pair_universe: Needed for trading pair metadata

    :param vmax: Trade success % to have the extreme green color.

    :param vmin: The % of lost capital on the trade to have the extreme red color.

    :param timestamp_format: How to format Opened at column, as passed to `strftime()`

    :param hidden_columns: Hide columns in the output table

    :return: DataFrame with human=readable position win/loss information, having DF indexed by timestamps and a styler function
    """

    exchange_map = {e.exchange_id: e for e in exchanges}

    # https://stackoverflow.com/a/52363890/315168
    def expander(row):
        position: TradePosition = row["position"]
        # timestamp = row.name  # ???
        pair_id = position.pair_id
        pair_info = pair_universe.get_pair_by_id(pair_id)
        exchange = exchange_map.get(pair_info.exchange_id)
        if not exchange:
            raise RuntimeError(f"No exchange for id {pair_info.exchange_id}, pair {pair_info}")

        if position.is_stop_loss():
            remarks = "SL"
        elif position.is_take_profit():
            remarks = "TP"
        else:
            remarks = ""

        # Hack around to work with legacy data issue.
        # Not an issue for new strategies.
        if position.has_bad_data_issues():
            remarks += "BAD"

        r = {
            # "timestamp": timestamp,
            "Id": position.position_id,
            "Remarks": remarks,
            "Opened at": position.opened_at.strftime(timestamp_format),
            "Duration": format_duration_days_hours_mins(position.duration) if position.duration else np.nan,
            "Exchange": exchange.name,
            "Base asset": pair_info.base_token_symbol,
            "Quote asset": pair_info.quote_token_symbol,
            "Position max value": format_value(position.get_max_size()),
            "PnL USD": format_value(position.realised_profit) if position.is_closed() else np.nan,
            "PnL %": format_percent_2_decimals(position.realised_profit_percent) if position.is_closed() else np.nan,
            "PnL % raw": position.realised_profit_percent if position.is_closed() else 0,
            "Open mid price USD": format_price(position.open_price),
            "Close mid price USD": format_price(position.close_price) if position.is_closed() else np.nan,
            "Trade count": position.get_trade_count(),
            "LP fees": f"${position.get_total_lp_fees_paid():,.2f}"
        }
        return r

    applied_df = timeline.apply(expander, axis='columns', result_type='expand')

    if len(applied_df) > 0:
        # https://stackoverflow.com/a/52720936/315168
        applied_df \
            .sort_values(by=['Id'], ascending=[True], inplace=True)

    # Get rid of NaN labels
    # https://stackoverflow.com/a/28390992/315168
    applied_df.fillna('', inplace=True)

    styling = TimelineStyler(
        row_styling=row_styling_mode,
        hidden_columns=hidden_columns,
        vmin=vmin,
        vmax=vmax,
    )

    return applied_df, styling


def expand_timeline_raw(
        timeline: pd.DataFrame,
        timestamp_format="%Y-%m-%d"
    ) -> pd.DataFrame:  # sourcery skip: remove-unreachable-code
        """A simplified version of expand_timeline that does not care about
        pair info, exchanges, or opening capital, and also provides raw figures"""

        # https://stackoverflow.com/a/52363890/315168
        def expander(row):
            position: TradePosition = row["position"]
            # timestamp = row.name  # ???
            pair_id = position.pair_id

            if position.is_stop_loss():
                remarks = "SL"
            elif position.is_take_profit():
                remarks = "TP"
            else:
                remarks = ""

            pnl_usd = position.realised_profit if position.is_closed() else np.nan

            r = {
                # "timestamp": timestamp,
                "Id": position.position_id,
                "Remarks": remarks,
                "Opened at": position.opened_at.strftime(timestamp_format),
                "Duration": format_duration_days_hours_mins(position.duration) if position.duration else np.nan,
                "position_max_size": position.get_max_size(),
                "pnl_usd": pnl_usd,
                "pnl_pct_raw": position.realised_profit_percent if position.is_closed() else 0,
                "open_price_usd": position.open_price,
                "close_price_usd": position.close_price if position.is_closed() else np.nan,
                "trade_count": position.get_trade_count(),
            }

            return r

        applied_df = timeline.apply(expander, axis='columns', result_type='expand')

        if len(applied_df) > 0:
            # https://stackoverflow.com/a/52720936/315168
            applied_df \
                .sort_values(by=['Id'], ascending=[True], inplace=True)

        # Get rid of NaN labels
        # https://stackoverflow.com/a/28390992/315168
        applied_df.fillna('', inplace=True)

        return applied_df


def build_trade_analysis(
    portfolio: Portfolio
) -> TradeAnalysis:
    """Build a trade analysis from list of positions.

    - Read positions from backtesting or live state

    - Create TradeAnalysis instance that can be used to display Jupyter notebook
      data on the performance

    :param state:
        optional parameter that should be specified if user would like to see advanced statistics
    """

    histories = {}

    positions = list(portfolio.get_all_positions())

    # Sort positions based on their id
    # because open, closed and frozen positions might be in a mixed order
    positions = sorted(positions, key=lambda p: p.position_id)

    # Each Backtrader Trade instance presents a position
    # Trade instances contain TradeHistory entries that present change to this position
    # with Order instances attached
    for position in positions:

        pair = position.pair
        pair_id = pair.internal_id
        assert type(pair_id) == int

        trade: TradeExecution

        trades = list(position.trades.values())

        for trade in trades:

            if trade.is_repaired() or trade.is_repair_trade():
                # These trades have quantity set to zero
                continue

            history = histories.get(pair_id)
            if not history:
                history = histories[pair_id] = AssetTradeHistory()

            # filter out failed trade
            if trade.executed_at is None:
                continue

            # Internally negative quantities are for sells
            bad_data_issues = False
            quantity = trade.executed_quantity
            timestamp = pd.Timestamp(trade.executed_at)
            if trade.planned_mid_price not in (0, None):
                price = trade.planned_mid_price
            else:
                # TODO: Legacy trades.
                # mid_price is filled to all latest trades
                price = trade.executed_price
                bad_data_issues = True

            assert quantity != 0, f"Got bad quantity for {trade}"
            assert (price is not None) and price > 0, f"Got invalid trade {trade.get_full_debug_dump_str()} - price is {price}"

            spot_trade = SpotTrade(
                pair_id=pair_id,
                trade_id=trade.trade_id,
                timestamp=timestamp,
                price=price,
                executed_price=trade.executed_price,
                quantity=quantity,
                commission=0,
                slippage=0,  # TODO
                trade_type=trade.trade_type,
                lp_fees_paid=trade.lp_fees_paid,
                bad_data_issues=bad_data_issues,
            )

            # used in get_max_consecutive
            portfolio_value_at_open = position.portfolio_value_at_open

            # used in calculate_summary_statistics()
            stop_loss = position.stop_loss
            
            if position.portfolio_value_at_open:
                capital_tied_at_open_pct=position.get_capital_tied_at_open_pct()
            else:
                capital_tied_at_open_pct=None
            
            if stop_loss:
                maximum_risk = position.get_loss_risk_at_open()
                loss_risk_at_open_pct = position.get_loss_risk_at_open_pct()
            else:
                maximum_risk = None
                loss_risk_at_open_pct = None

            history.add_trade(
                spot_trade, 
                position_id=position.position_id,
                portfolio_value_at_open=portfolio_value_at_open,
                loss_risk_at_open_pct=loss_risk_at_open_pct,
                capital_tied_at_open_pct=capital_tied_at_open_pct,
                stop_loss=stop_loss,
                maximum_risk=maximum_risk
            )

    return TradeAnalysis(
        portfolio, 
        asset_histories=histories
    )

def avg(lst: list[int]):
    return sum(lst) / len(lst)
