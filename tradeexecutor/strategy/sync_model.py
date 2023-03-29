"""Strategy deposit and withdrawal syncing."""

import datetime
from abc import ABC, abstractmethod
from typing import Callable, List

from tradeexecutor.ethereum.wallet import ReserveUpdateEvent
from tradeexecutor.state.identifier import AssetIdentifier
from tradeexecutor.state.portfolio import Portfolio
from tradeexecutor.state.state import State


# Prototype sync method that is not applicable to the future production usage
SyncMethodV0 = Callable[[Portfolio, datetime.datetime, List[AssetIdentifier]], List[ReserveUpdateEvent]]


class SyncMethod(ABC):
    """Describe the sync adapter."""

    @abstractmethod
    def sync_initial(self, state: State):
        """Initialize the vault connection."""
        pass

    @abstractmethod
    def sync_treasuty(self,
                 strategy_cycle_ts: datetime.datetime,
                 state: State,
                 ):
        """Apply the balance sync before each strategy cycle."""
        pass
