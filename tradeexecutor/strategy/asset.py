"""Asset management helpers.

Figure how to map different tokens related to their trading positions.
"""
from dataclasses import field, dataclass
from decimal import Decimal
from typing import List, Collection, Set, Dict, Tuple

from tradeexecutor.state.generic_position import GenericPosition
from tradeexecutor.state.portfolio import Portfolio
from tradingstrategy.pair import PandasPairUniverse

from tradeexecutor.state.identifier import AssetIdentifier, TradingPairIdentifier
from tradeexecutor.state.position import TradingPosition
from tradeexecutor.state.reserve import ReservePosition
from tradeexecutor.state.state import State
from tradeexecutor.strategy.trading_strategy_universe import TradingStrategyUniverse, translate_trading_pair


@dataclass
class AssetToPositionsMapping:
    """Tell us which positions hold the asset in a portfolio."""

    #: Token we are checking
    asset: AssetIdentifier

    #: Positions using this token
    positions: Set[GenericPosition] = field(default_factory=set)

    #: Expected amount of tokens we will find on chain
    #:
    #: This is the quantity across all positions.
    #:
    quantity: Decimal = Decimal(0)

    def is_one_to_one_asset_to_position(self) -> bool:
        return len(self.positions) == 1

    def is_for_reserve(self) -> bool:
        return len(self.positions) == 1 and isinstance(self.get_only_position(), ReservePosition)

    def get_only_position(self) -> GenericPosition:
        assert len(self.positions) == 1
        return next(iter(self.positions))

    def get_first_position(self) -> GenericPosition:
        return next(iter(self.positions))


def _is_open_ended_universe(pair_universe: PandasPairUniverse):
    # TODO: Have this properly defined in a strategy module
    return pair_universe.get_count() > 20


def get_relevant_assets(
        pair_universe: PandasPairUniverse,
        reserve_assets: Collection[AssetIdentifier],
        state: State,
) -> Set[AssetIdentifier]:
    """Get list of tokens that are relevant for the straegy.

    We need to know the list of tokens we need to scan for the strategy
    to do the accounting checks.

    A token is relevant if it

    - Can be used in a trading position

    - Can be used as a reserve currency

    For open-ended trading universes we only consider trading pairs that have been traded
    at least once.

    :return:
        A list of tokens of which balances we need to check when doing accounting
    """

    assert isinstance(pair_universe, PandasPairUniverse)
    assert isinstance(state, State)

    assets = set()

    for asset in reserve_assets:
        assets.add(asset)

    if _is_open_ended_universe(pair_universe):
        # For open ended universe we can have thousands of assets
        # so we cannot query them all
        for p in state.portfolio.get_all_positions():
            assets.add(p.pair.base)
    else:
        for p in pair_universe.iterate_pairs():
            pair = translate_trading_pair(p)
            assert pair.is_spot(), f"Can only match spot positions, got {pair}"
            assets.add(pair.base)

    return assets


def map_onchain_asset_to_position(
    asset: AssetIdentifier,
    state: State,
) -> TradingPosition | ReservePosition | None:
    """Map an on-chain found asset to a trading position.

    - Any reserve currency deposits go to the reserve

    - Any trading position assets go to their respective open trading
      and frozen position

    - If there are trading position assets and no position is open,
      then panic

    - Always check reserve first

    - If multiple positions are sharing the asset e.g. collateral
      return the firs position

    :param asset:
        On-chain read token we should make

    :param state:
        The current strategy state

    :return:
        The position we think the asset belongs to.

        None if there is no reserve, open or frozen
        positions we know of.
    """

    r: ReservePosition
    for r in state.portfolio.reserves.values():
        if asset == r.asset:
            return r

    for p in state.portfolio.get_open_and_frozen_positions():
        if asset == p.pair.base:
            return p
        if asset == p.pair.quote:
            return p

    return None


def get_asset_amounts(p: TradingPosition) -> List[Tuple[AssetIdentifier, Decimal]]:
    """What tokens this position should hold in a wallet."""
    if p.is_spot():
        return [(p.pair.base, p.get_quantity())]
    elif p.is_short():
        return [
            (p.pair.base, p.loan.get_borrowed_quantity()),
            (p.pair.quote, p.loan.get_collateral_quantity()),
        ]
    else:
        raise NotImplementedError()


def get_expected_assets(portfolio: Portfolio) -> Dict[AssetIdentifier, AssetToPositionsMapping]:
    """Get list of tokens that the portfolio should hold.

    :return:
        Token -> (Amount, positions hold across mappings)
    """

    mappings: Dict[AssetIdentifier, AssetToPositionsMapping] = {}

    r: ReservePosition
    for r in portfolio.reserves.values():
        if r.asset not in mappings:
            mappings[r.asset] = AssetToPositionsMapping(asset=r.asset)

        mappings[r.asset].positions.add(r)
        mappings[r.asset].quantity += r.quantity

    for p in portfolio.get_open_and_frozen_positions():
        for asset, amount in get_asset_amounts(p):
            if asset not in mappings:
                mappings[asset] = AssetToPositionsMapping(asset=asset)

            mappings[asset].positions.add(p)
            mappings[asset].quantity += amount

    return mappings
