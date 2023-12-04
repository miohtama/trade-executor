import datetime

from tradeexecutor.strategy.trading_strategy_universe import (
    Dataset,
    TradingStrategyUniverse,
)
from tradeexecutor.strategy.pandas_trader.alternative_market_data import (
    load_candle_universe_from_dataframe,
)
from tradingstrategy.timebucket import TimeBucket

from tradingstrategy.binance.constants import (
    BINANCE_CHAIN_ID,
    BINANCE_EXCHANGE_SLUG,
)
from tradingstrategy.binance.utils import (
    generate_pairs_for_binance,
    generate_exchange_universe_for_binance,
    add_info_columns_to_ohlc,
    generate_lending_reserve_for_binance,
)

from tradingstrategy.binance.downloader import BinanceDownloader
from tradingstrategy.lending import LendingReserveUniverse, LendingCandleUniverse
from tradingstrategy.pair import DEXPair


def load_binance_dataset(
    symbols: list[str] | str,
    candle_time_bucket: TimeBucket,
    stop_loss_time_bucket: TimeBucket,
    start_at: datetime.datetime | None = None,
    end_at: datetime.datetime | None = None,
    include_lending: bool = False,
    force_download: bool = False,
) -> Dataset:
    """Load a Binance dataset.

    This is the one-stop shop function for loading all your Binance data. It can include
    candlestick, stop loss, lending and supply data for all valid symbols.

    If start_at and end_at are not provided, the entire dataset will be loaded.

    :param symbols: List of symbols to load
    :param candle_time_bucket: Time bucket for candle data
    :param stop_loss_time_bucket: Time bucket for stop loss data
    :param start_at: Start time for data
    :param end_at: End time for data
    :param include_lending: Whether to include lending data or not
    :param force_download: Force download of data
    :return: Dataset
    """
    if isinstance(symbols, str):
        symbols = [symbols]

    downloader = BinanceDownloader()

    pairs = generate_pairs_for_binance(symbols)

    # use stop_loss_time_bucket since, in this case, it's more granular data than the candle_time_bucket
    # we later resample to the higher time bucket for the backtest candles
    df = downloader.fetch_candlestick_data(
        symbols,
        stop_loss_time_bucket,
        start_at,
        end_at,
        force_download=force_download,
    )

    spot_symbol_map = {symbol: i + 1 for i, symbol in enumerate(symbols)}

    candle_df = add_info_columns_to_ohlc(
        df, {symbol: pair for symbol, pair in zip(symbols, pairs)}
    )

    candle_df["pair_id"].replace(spot_symbol_map, inplace=True)

    candle_universe, stop_loss_candle_universe = load_candle_universe_from_dataframe(
        df=candle_df,
        include_as_trigger_signal=True,
        resample=candle_time_bucket,
    )

    exchange_universe = generate_exchange_universe_for_binance(pair_count=len(pairs))

    pairs_df = DEXPair.convert_to_dataframe(pairs)
    pairs_df["pair_id"].replace(spot_symbol_map, inplace=True)

    # pairs_df = candle_universe.pairs.first().reset_index()
    # pairs_df['fee'] = pairs_df['fee'] * 10_000
    # del pairs_df["timestamp"]
    # del pairs_df["open"]
    # del pairs_df["high"]
    # del pairs_df["low"]
    # del pairs_df["close"]
    # del pairs_df["volume"]

    if include_lending:
        reserves = []
        reserve_id = 1
        for pair in pairs:
            reserves.append(
                generate_lending_reserve_for_binance(
                    pair.base_token_symbol, pair.token0_address, reserve_id
                )
            )
            reserves.append(
                generate_lending_reserve_for_binance(
                    pair.quote_token_symbol, pair.token1_address, reserve_id + 1
                )
            )
            reserve_id += 2

        lending_reserve_universe = LendingReserveUniverse(
            {reserve.reserve_id: reserve for reserve in reserves}
        )

        lending_candle_type_map = downloader.load_lending_candle_type_map(
            {reserve.reserve_id: reserve.asset_symbol for reserve in reserves},
            candle_time_bucket,
            start_at,
            end_at,
            force_download=force_download,
        )

        lending_candle_universe = LendingCandleUniverse(
            lending_candle_type_map, lending_reserve_universe
        )
    else:
        lending_reserve_universe = None
        lending_candle_universe = None

    dataset = Dataset(
        time_bucket=candle_time_bucket,
        exchanges=exchange_universe,
        pairs=pairs_df,
        candles=candle_universe.df,
        backtest_stop_loss_time_bucket=stop_loss_time_bucket,
        backtest_stop_loss_candles=stop_loss_candle_universe.df,
        lending_candles=lending_candle_universe,
        lending_reserves=lending_reserve_universe,
    )

    return dataset


def create_binance_universe(
    symbols: list[str] | str,
    candle_time_bucket: TimeBucket,
    stop_loss_time_bucket: TimeBucket,
    start_at: datetime.datetime | None = None,
    end_at: datetime.datetime | None = None,
    reserve_pair_ticker: str | None = None,
    include_lending: bool = False,
    force_download: bool = False,
) -> TradingStrategyUniverse:
    """Create a Binance universe that can be used for backtesting.

    Similarly to `load_binance_dataset`, this function loads all the data needed for backtesting,
    including candlestick, stop loss, lending and supply data for all valid symbols.

    :param symbols: List of symbols to load
    :param candle_time_bucket: Time bucket for candle data
    :param stop_loss_time_bucket: Time bucket for stop loss data
    :param start_at: Start time for data
    :param end_at: End time for data
    :param reserve_pair_ticker: Pair ticker to use as the reserve asset
    :param include_lending: Whether to include lending data or not
    :param force_download: Whether to force download of data or get it from cache
    :return: Trading strategy universe
    """
    dataset = load_binance_dataset(
        symbols,
        candle_time_bucket,
        stop_loss_time_bucket,
        start_at,
        end_at,
        include_lending=include_lending,
        force_download=force_download,
    )

    selected_columns = dataset.pairs[["base_token_symbol", "quote_token_symbol"]]
    pair_tickers = [tuple(x) for x in selected_columns.to_numpy()]

    if reserve_pair_ticker is None:
        reserve_asset_ticker = pair_tickers[0]

    universe = TradingStrategyUniverse.create_limited_pair_universe(
        dataset=dataset,
        chain_id=BINANCE_CHAIN_ID,
        exchange_slug=BINANCE_EXCHANGE_SLUG,
        pairs=pair_tickers,
        reserve_asset_pair_ticker=reserve_asset_ticker,
    )

    return universe