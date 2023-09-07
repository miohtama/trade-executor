from decimal import Decimal
from typing import Dict, Tuple


class OutOfSimulatedBalance(Exception):
    pass


class SimulatedWallet:
    """A wallet that keeps token balances by ERC-20 address.

    - We also simulate incoming and outgoing aToken and vToken amounts with :py:meth:`rebalance`.
    """

    def __init__(self):
        self.balances: Dict[str, Decimal] = {}
        self.nonce = 0

    def update_balance(self, token_address: str, delta: Decimal):
        """Change the token balance of some delta.

        Check that balance does not go zero.
        """
        assert token_address.lower() == token_address, "No checksummed addresses"
        assert isinstance(delta, Decimal), f"Expected decimal got: {delta.__class__}: {delta}"
        old_balance = self.balances.get(token_address, Decimal(0))
        new_balance = old_balance + delta
        if new_balance < 0:
            raise OutOfSimulatedBalance(f"Simulated wallet balance went negative {new_balance} for token {token_address}")
        self.balances[token_address] = new_balance

    def set_balance(self, token_address: str, amount: Decimal):
        """Directly set balance."""
        assert token_address.lower() == token_address, "No checksummed addresses"
        assert isinstance(amount, Decimal), f"Expected decimal got: {amount.__class__}: {amount}"
        self.balances[token_address] = amount

    def rebase(self, token_address: str, new_amount: Decimal):
        """Set rebase token amount."""
        self.set_balance(token_address, new_amount)

    def get_balance(self, token_address: str) -> Decimal:
        assert token_address.lower() == token_address, "No checksummed addresses"
        return self.balances.get(token_address, Decimal(0))

    def fetch_nonce_and_tx_hash(self) -> Tuple[int, str]:
        """Allocates a dummy nonce for a transaction.

        :return:
            Tuple (nonce, tx_hash)
        """
        nonce = self.nonce
        tx_hash = hex(nonce)
        self.nonce += 1
        return nonce, tx_hash


