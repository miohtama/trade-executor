"""Strategy deposit and withdrawal syncing."""

import datetime
from _decimal import Decimal
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, List, Optional, Iterable, Collection

from eth_defi.hotwallet import HotWallet
from tradeexecutor.ethereum.tx import TransactionBuilder
from tradeexecutor.ethereum.wallet import ReserveUpdateEvent
from tradeexecutor.state.balance_update import BalanceUpdate, BalanceUpdateCause, BalanceUpdatePositionType
from tradeexecutor.state.identifier import AssetIdentifier
from tradeexecutor.state.portfolio import Portfolio
from tradeexecutor.state.position import TradingPosition
from tradeexecutor.state.reserve import ReservePosition
from tradeexecutor.state.state import State
from tradeexecutor.state.sync import BalanceEventRef
from tradeexecutor.state.types import JSONHexAddress
from tradeexecutor.strategy.trading_strategy_universe import TradingStrategyUniverse
from tradeexecutor.strategy.pricing_model import PricingModel

# Prototype sync method that is not applicable to the future production usage
SyncMethodV0 = Callable[[Portfolio, datetime.datetime, List[AssetIdentifier]], List[ReserveUpdateEvent]]


@dataclass
class OnChainBalance:
    """Describe on-chain token position.

    Amount of tokens held at the certain point of time.

    """
    block_number: int | None
    timestamp: datetime.datetime | None
    asset: AssetIdentifier
    amount: Decimal

    def __repr__(self):
        return f"<On-chain balance for {self.asset} is {self.amount} at {self.timestamp}>"


class SyncModel(ABC):
    """Abstract class for syncing on-chain fund movements event to the strategy treasury."""

    def get_vault_address(self) -> Optional[str]:
        """Get the vault address we are using.

        :return:
            None if the strategy is not vaulted
        """
        return None

    def get_hot_wallet(self) -> Optional[HotWallet]:
        """Get the vault address we are using.

        :return:
            None if the executor is not using hot wallet (dummy, backtesting, etc.)
        """
        return None

    def resync_nonce(self):
        """Re-read hot wallet nonce before trade execution.

        Ensures that if the private key is used outside the trade executor,
        we are not getting wrong nonce error when broadcasting the transaction.
        """

    def is_ready_for_live_trading(self, state: State) -> bool:
        """Check that the state and sync model is ready for live trading."""
        # By default not any checks are needed
        return True

    @abstractmethod
    def get_token_storage_address(self) -> Optional[JSONHexAddress]:
        """Get the address where tokens are stored.

        :return:
            Vault address for enzyme, wallet address for hot wallet.

            Return `None` for DummySyncModel e.g. in the case of
            unit and backtests.
        """

    @abstractmethod
    def sync_initial(self, state: State, **kwargs):
        """Initialize the vault connection.

        :param kwargs:
            Extra hints for the initial sync.

            Because reading event from Ethereum blockchain is piss poor mess.
        """
        pass

    @abstractmethod
    def sync_treasury(self,
                      strategy_cycle_ts: datetime.datetime,
                      state: State,
                      supported_reserves: Optional[List[AssetIdentifier]] = None
                      ) -> List[BalanceUpdate]:
        """Apply the balance sync before each strategy cycle.

        :param strategy_cycle_ts:
            The current strategy cycle.

            Resevers are synced before executing the strategy cycle.

        :param state:
            Current state of the execution.

        :param supported_reverses:
            List of assets the strategy module wants to use as its reserves.

            May be None in testing.

        :return:
            List of balance updates detected.

            - Deposits

            - Redemptions

        """

    @abstractmethod
    def create_transaction_builder(self) -> Optional[TransactionBuilder]:
        """Creates a transaction builder instance to make trades against this asset management model.

        Only needed when trades are being executed.

        :return:
            Depending on the asset management mode.

            - :py:class:`tradeexecutor.ethereum.tx.HotWalletTransactionBuilder`

            - :py:class:`tradeexecutor.ethereum.enzyme.tx.EnzymeTransactionBuilder`
        """

    @abstractmethod
    def fetch_onchain_balances(
            self,
            assets: Collection[AssetIdentifier],
            filter_zero=True
    ) -> Iterable[OnChainBalance]:
        """Read the on-chain asset details.

        - Mark the block we are reading at the start

        - Asset list is sorted to be by address to make sure
          the return order is deterministic

        :param filter_zero:
            Do not return zero balances

        :return:
            Iterator for assets by the sort order.
        """

    def sync_interests(
            self,
            timestamp: datetime.datetime,
            state: State,
            universe: TradingStrategyUniverse,
            credit_positions: List[TradingPosition],
            pricing_model: PricingModel,
    ) -> List[BalanceUpdate]:
        """Update all credit supply positions.

        :param timestamp:
            Wall clock time.

            This function can be called outside the strategy cycle.

        :param state:
            Current strategy state

        :param universe:
            Trading universe that must include lending data.

        :param credit_positions:
            Prefiltered list of credit positions to update.

        :param pricing_model:
            Used to re-value loans

        :return:
            All triggered balance update events
        """
        raise NotImplementedError()

    def reset_deposits(self, state: State):
        """Clear out pending withdrawals/deposits events."""
        raise NotImplementedError()


class DummySyncModel(SyncModel):
    """Do nothing sync model.

    - There is no integration to external systems

    - Used in unit testing
    """

    def __init__(self):
        self.fake_sync_done = False

    def sync_initial(self, state: State):
        pass

    def get_token_storage_address(self) -> Optional[JSONHexAddress]:
        return None

    def sync_treasury(self,
                      strategy_cycle_ts: datetime.datetime,
                      state: State,
                      supported_reserves: Optional[List[AssetIdentifier]] = None
                      ) -> List[BalanceUpdate]:
        if not self.fake_sync_done:
            state.sync.treasury.last_updated_at = datetime.datetime.utcnow()
            state.sync.treasury.last_cycle_at = strategy_cycle_ts
            ref = BalanceEventRef(
                0,
                datetime.datetime.utcnow(),
                BalanceUpdateCause.deposit,
                BalanceUpdatePositionType.open_position,
                None,
                None,
            )
            state.sync.treasury.balance_update_refs = [ref]  # TODO: Get rid of the checks in on_clock()
            return []
        return []

    def create_transaction_builder(self) -> Optional[TransactionBuilder]:
        return None
