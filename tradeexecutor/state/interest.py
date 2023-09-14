"""Interest tracking data structures."""
import datetime
from dataclasses import dataclass
from decimal import Decimal

from dataclasses_json import dataclass_json

from tradeexecutor.utils.accuracy import ZERO_DECIMAL


@dataclass_json
@dataclass(slots=True)
class Interest:
    """Interest data tracking for positions that where the amount changes over time.

    - Credit positions (depositing in Aave reservess)

    - Longs / shorts
    """

    #: How many tokens we deposited to this position at amount.
    #:
    #: Use this only for calculation verifications,
    #: because the amount can be increased/reduced over time
    #:
    opening_amount: Decimal

    #: How many atokens/votkens we had on the previous read.
    #:
    #: This is principal + interest.
    #:
    last_token_amount: Decimal

    #: When the denormalised data was last updated.
    #:
    #: Wall clock time.
    #:
    last_updated_at: datetime.datetime

    #: When the denormalised data was last updated.
    #:
    #: Event time (block mined timestamp).
    #:
    last_event_at: datetime.datetime

    #: How much interest we have gained
    #:
    #:
    last_accrued_interest: Decimal

    #: Block number for the update
    #:
    #: When was the last time we read aToken balance.
    #:
    last_updated_block_number: int | None = None

    #: How much repayments this loan has received.
    #:
    #: - If this is collateral, then this is how much interest we have claimed
    #:
    #: - If this is borrow, then this is how much interest we have paid back
    #:
    #: TODO: This must be reset when there is change to underlying aToken/vToken amount
    #  e.g. when partially closing a position.
    #:
    interest_payments: Decimal = ZERO_DECIMAL

    def __repr__(self):
        return f"<Interest, current principal + interest {self.last_token_amount}, current tracked interest gains {self.last_accrued_interest}>"

    def __post_init__(self):
        assert isinstance(self.opening_amount, Decimal)
        assert isinstance(self.last_accrued_interest, Decimal)

    def get_principal_and_interest_quantity(self) -> Decimal:
        """Return how many tokens exactly we have on the loan.

        Assuming any aToken/vToken will be fully converted to the underlying.
        """
        return self.last_token_amount

    @staticmethod
    def open_new(opening_amount: Decimal, timestamp: datetime.datetime) -> "Interest":
        assert opening_amount > 0
        return Interest(
            opening_amount=opening_amount,
            last_updated_at=timestamp,
            last_event_at=timestamp,
            last_accrued_interest=Decimal(0),
            last_token_amount=opening_amount,
        )

    def get_remaining_interest(self) -> Decimal:
        """GEt the amount of interest this position has still left.

        This is total lifetime interest + repayments / claims.
        """
        return self.last_accrued_interest - self.interest_payments

    def claim_interest(self, quantity: Decimal):
        """Update interest claims from profit from accuring interest on collateral/"""
        self.interest_payments += quantity

    def repay_interest(self, quantity: Decimal):
        """Update interest payments needed to maintain the borrowed debt."""
        self.interest_payments += quantity
