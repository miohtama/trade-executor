"""Loan data structures.

- When you do short and leveraged position by borrowing assets

"""
import copy
import math
from _decimal import Decimal
from dataclasses import dataclass
from typing import TypeAlias, Tuple

from dataclasses_json import dataclass_json

from tradeexecutor.state.identifier import AssetIdentifier, AssetWithTrackedValue, TradingPairIdentifier
from tradeexecutor.state.interest import Interest
from tradeexecutor.state.types import LeverageMultiplier, USDollarAmount
from tradeexecutor.utils.accuracy import ZERO_DECIMAL

#: Health Factor: hF=dC/d, if lower than 1, the account can be liquidated
#:
#: Health factor is infinite for loans that do not borrow
#: (doing only credit supply for interest).
#:
HealthFactor: TypeAlias = float


class LiquidationRisked(Exception):
    """The planned loan health factor is too low.

    You would be immediately liquidated if the parameter
    changes would be applied.
    """


@dataclass_json
@dataclass
class Loan:
    """Borrowed out assets.

    See also

    - :py:class:`tradeexcutor.state.position.TradingPosition` for tracking a long/short

    - :py:class:`tradeexcutor.state.interest.Interest` for position interest calculations

    - `1delta documentation <https://docs.1delta.io/lenders/metrics>`__

    - `leverage calculations <https://amplifiedlambda.com/leverage-with-defi-calculations/>`__
    """

    #: Our trading pair data for this position
    pair: TradingPairIdentifier

    #: What collateral we used for this loan
    #:
    #: This is aToken for Aave
    #:
    collateral: AssetWithTrackedValue

    #: Tracker for collateral interest events
    #:
    collateral_interest: Interest

    #: What collateral we used for this loan
    #:
    #: This is vToken for Aave.
    #:
    #: Not set if the loan is only for credit supply position.
    #:
    borrowed: AssetWithTrackedValue | None = None

    #: Tracker for borrowed asset interest events
    #:
    borrowed_interest: Interest | None = None

    #: How much accrued interest we have moved to the cast reserves from this position.
    #:
    realised_interest: Decimal = ZERO_DECIMAL

    def __repr__(self):
        asset_symbol = self.borrowed.asset.token_symbol if self.borrowed else ""
        return f"<Loan, borrowed ${self.get_borrow_value()} {asset_symbol} for collateral ${self.get_collateral_value()}, at leverage {self.get_leverage()}>"

    def clone(self) -> "Loan":
        """Clone this data structure for mutating."""
        return copy.deepcopy(self)

    def get_collateral_interest(self) -> USDollarAmount:
        """How much interest we have received on collateral."""
        return float(self.collateral_interest.last_accrued_interest) * self.collateral.last_usd_price

    def get_collateral_value(self, include_interest=True) -> USDollarAmount:
        if include_interest:
            return self.collateral.get_usd_value() + self.get_collateral_interest()
        return self.collateral.get_usd_value()

    def get_borrow_value(self, include_interest=True) -> USDollarAmount:
        """Get the outstanding debt amount.

        :param include_interest:
            With interest.
        """
        if not self.borrowed:
            return 0
        if include_interest:
            return self.borrowed.get_usd_value() + self.get_borrow_interest()
        return self.borrowed.get_usd_value()

    def get_borrow_interest(self) -> USDollarAmount:
        """How much interest we have paid on borrows

        :return:
            Always positive
        """
        if self.borrowed:
            return float(self.borrowed_interest.last_accrued_interest) * self.borrowed.last_usd_price
        return 0

    def get_net_interest(self) -> USDollarAmount:
        """How many dollars of interest we have accumulated.

        - We gain money on collateral

        - We lost money by maintaining borrow

        """
        if self.borrowed:
            # Margined trading
            return self.get_collateral_interest() - self.get_borrow_interest()
        else:
            # Credit supply
            return self.get_collateral_interest()

    def get_net_asset_value(self, include_interest=True) -> USDollarAmount:
        """What's the withdrawable amount of the position is closed."""

        if self.borrowed:
            # Margined trading
            return self.get_collateral_value(include_interest) - self.get_borrow_value(include_interest)
        else:
            # Credit supply
            return self.get_collateral_value(include_interest)

    def get_leverage(self) -> LeverageMultiplier:
        """How leveraged this loan is.

        Using formula ``(collateral / (collateral - borrow))``.

        """
        return self.collateral.get_usd_value() / self.get_net_asset_value()

    def get_free_margin(self) -> USDollarAmount:
        raise NotImplementedError()

    def get_health_factor(self) -> HealthFactor:
        """Get loan health factor.

        Safety of your deposited collateral against the borrowed assets and its underlying value.

        If the health factor goes below 1, the liquidation of your collateral might be triggered.
        """
        if self.borrowed is None or self.borrowed.quantity == 0:
            return math.inf
        return self.collateral.asset.liquidation_threshold * self.collateral.get_usd_value() / self.borrowed.get_usd_value()

    def get_max_size(self) -> USDollarAmount:
        raise NotImplementedError()

    def get_loan_to_value(self):
        """Get LTV of this loan.

        LTV should stay below the liquidation threshold.
        For Aave ETH the liquidation threshold is 80%.
        """
        return self.borrowed.get_usd_value() / self.collateral.get_usd_value()

    def claim_interest(self, quantity: Decimal | None = None) -> Decimal:
        """Claim intrest from this position.

        Interest should be moved to reserves.

        :param quantity:
            How many reserve tokens worth to claim.

            If not given claim all accrued interest.

        :return:
            Claimed interest in reserve tokens
        """

        if not quantity:
            quantity = self.collateral_interest.last_accrued_interest
        self.realised_interest += quantity
        return quantity

    def calculate_collateral_for_target_ltv(
            self,
            target_ltv: float,
            borrowed_quantity: Decimal | float,
    ) -> Decimal:
        """Calculate the collateral amount we need to hit a target LTV.

        Assuming our debt stays the same, how much collateral we need
        to hit the target LTV.

        .. note ::

            Watch out for rounding/epsilon errors.

        :param borrowed_quantity:
            What is expected outstanding loan amount

        :return:
            US dollars worth of collateral needed
        """
        borrowed_usd = self.borrowed.last_usd_price  * float(borrowed_quantity)
        usd_value = borrowed_usd/ target_ltv
        return Decimal(usd_value / self.collateral.last_usd_price)

    def calculate_collateral_for_target_leverage(
            self,
            leverage: LeverageMultiplier,
            borrowed_quantity: Decimal | float,
    ) -> Decimal:
        """Calculate the collateral amount we need to hit a target leverage.

        Assuming our debt stays the same, how much collateral we need
        to hit the target LTV.

        .. code-block:: text

            col / (col - borrow) = leverage
            col = (col - borrow) * leverage
            col = col * leverage - borrow * leverage
            col - col * leverage = - borrow * levereage
            col(1 - leverage) = - borrow * leverage
            col = -(borrow * leverage) / (1 - leverage)

        See also :py:func:`calculate_leverage_for_target_size`

        :param borrowed_quantity:
            What is expected outstanding loan amount

        :return:
            US dollars worth of collateral needed
        """
        borrowed_usd = self.borrowed.last_usd_price  * float(borrowed_quantity)
        usd_value = -(borrowed_usd * leverage) / (1 - leverage)
        return Decimal(usd_value / self.collateral.last_usd_price)

    def check_health(self, desired_health_factor=1):
        """Check if this loan is healthy.

        Health factor must stay above 1 or you get liquidated.

        :raise LiquidationRisked:
            If the loan would be instantly liquidated

        """
        health_factor = self.get_health_factor()
        if health_factor <= desired_health_factor:
            raise LiquidationRisked(
                f"Loan health factor: {health_factor:.2f}.\n"
                f"You would be liquidated.\n"
                f"Desired health factor is {desired_health_factor:.2f}.\n"
                f"Collateral {self.collateral.get_usd_value()} USD.\n"
                f"Borrowed {self.borrowed.quantity} {self.borrowed.asset.token_symbol} {self.borrowed.get_usd_value()} USD.\n"
            )


def calculate_sizes_for_leverage(
    starting_reserve: USDollarAmount,
    leverage: LeverageMultiplier,
) -> Tuple[USDollarAmount, USDollarAmount]:
    """Calculate the collateral and borrow loan size to hit the target leverage with a starting capital.

    - When calculating the loan size using this function,
      the loan net asset value will be the same as starting capital

    - Because loan net asset value is same is deposited reserve,
      portfolio total NAV stays intact

    Notes:

    .. code-block:: text

            col / (col - borrow) = leverage
            col = (col - borrow) * leverage
            col = col * leverage - borrow * leverage
            col - col * leverage = - borrow * levereage
            col(1 - leverage) = - borrow * leverage
            col = -(borrow * leverage) / (1 - leverage)

            # Calculate leverage for 4x and 1000 USD collateral
            col - borrow = 1000
            col = 1000
            leverage = 3

            col / (col - borrow) = 3
            3(col - borrow) = col
            3borrow = 3col - col
            borrow = col - col/3

            col / (col - (col - borrow)) = leverage
            col / borrow = leverage
            borrow = leverage * 1000


    :param starting_reserve:
        Initial deposit in lending protocol

    :return:
        Tuple (borrow value, collateral value) in dollars
    """

    collateral_size = starting_reserve * leverage
    borrow_size = (collateral_size - (collateral_size / leverage))
    return borrow_size, collateral_size

