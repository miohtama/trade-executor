"""Dust amounts and epsilon rounding.

Because of very small and very accurate token units,
a lot of trades may end up having rounding artifacts.
We need to deal with these rounding artifacts by checking for "dust".

"""
from decimal import Decimal

from tradeexecutor.state.identifier import TradingPairIdentifier, AssetIdentifier

#: The absolute number of tokens we consider the value to be zero
#:
#:
#: Because of funny %s of values divided near zero,
#: we cannot use relative comparison near zero values.
#:
#:
DEFAULT_DUST_EPSILON = Decimal(10 ** -4)



def get_dust_epsilon_for_pair(pair: TradingPairIdentifier) -> Decimal:
    """Get the dust threshold for a trading pair.

    :param pair:
        Trading pair identifier.

    :return:
        Maximum amount of units we consider "zero".

    """
    return get_dust_epsilon_for_asset(pair.base)


def get_dust_epsilon_for_asset(asset: AssetIdentifier) -> Decimal:
    """Get the dust threshold for a trading pair.

    :param pair:
        Trading pair identifier.

    :return:
        Maximum amount of units we consider "zero".

    """

    # Hardcoded rules for now.
    # Some practical problems we have run across in backtesting.
    # We have wrapped and non-wrapped token symbols as we are backtesting both on DEX and CEX data
    if asset.token_symbol in ("WBTC", "BTC"):
        return Decimal(10 ** -7)
    elif asset.token_symbol in ("ETH", "WETH"):
        return Decimal(10 ** -7)
    elif asset.token_symbol in ("USDC", "USDC.e"):
        return Decimal(0.1)
    else:
        return DEFAULT_DUST_EPSILON