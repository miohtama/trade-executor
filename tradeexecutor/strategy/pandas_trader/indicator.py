"""Indicator definitions."""
import datetime
import threading
from abc import ABC, abstractmethod

# Enable pickle patch that allows multiprocessing in notebooks
from tradeexecutor.monkeypatch import cloudpickle_patch

import concurrent
import enum
import inspect
import itertools
import os
import pickle
import shutil
import signal
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from multiprocessing import Process
from pathlib import Path
from types import NoneType
from typing import Callable, Protocol, Any, TypeAlias
import logging

import futureproof
import pandas as pd

from tqdm_loggable.auto import tqdm

from tradeexecutor.state.identifier import TradingPairIdentifier
from tradeexecutor.strategy.execution_context import ExecutionContext
from tradeexecutor.strategy.parameters import StrategyParameters
from tradeexecutor.strategy.trading_strategy_universe import TradingStrategyUniverse, UniverseCacheKey
from tradeexecutor.utils.cpu import get_safe_max_workers_count

logger = logging.getLogger(__name__)


#: Where do we keep precalculated indicator Parquet files
#:
DEFAULT_INDICATOR_STORAGE_PATH = Path(os.path.expanduser("~/.cache/indicators"))


class IndicatorCalculationFailed(Exception):
    """We could not calculate the given indicator.

    - Wrap the underlying Python exception to a friendlier error message
    """


class IndicatorFunctionSignatureMismatch(Exception):
    """Given Pythohn function cannot run on the passed parameters."""


class IndicatorSource(enum.Enum):
    """The data on which the indicator will be calculated."""

    #: Calculate this indicator based on candle close price
    close_price = "close_price"

    #: Calculate this indicator based on candle open price
    open_price = "open_price"

    #: This indicator is calculated once per the strategy universe
    #:
    #: These indicators are custom and do not have trading pair set
    #:
    strategy_universe = "strategy_universe"

    def is_per_pair(self) -> bool:
        """This indicator is calculated to all trading pairs."""
        return self in (IndicatorSource.open_price, IndicatorSource.close_price)



@dataclass(slots=True)
class IndicatorDefinition:
    """A definition for a single indicator.

    - Indicator definitions are static - they do not change between the strategy runs

    - Used as id for the caching the indicator results

    - Definitions are used to calculate indicators for all trading pairs,
      or once over the whole trading universe

    - Indicators are calcualted independently from each other -
      a calculation cannot access cached values of other calculation
    """

    #: Name of this indicator.
    #:
    #: Later in `decide_trades()` you use this name to access the indicator data.
    #:
    name: str

    #: The underlying method we use to
    #:
    #: Same function can part of multiple indicators with different parameters (length).
    #:
    #: Because function pickling issues, this may be set to ``None`` in results.
    #:
    func: Callable | None

    #: Parameters for building this indicator.
    #:
    #: - Each key is a function argument name for :py:attr:`func`.
    #: - Each value is a single value
    #:
    #: - Grid search multiple parameter ranges are handled outside indicator definition
    #:
    parameters: dict

    #: On what trading universe data this indicator is calculated
    #:
    source: IndicatorSource = IndicatorSource.close_price

    def __repr__(self):
        return f"<Indicator {self.name} using {self.func.__name__ if self.func else '?()'} for {self.parameters}>"

    def __eq__(self, other):
        return self.name == other.name and self.parameters == other.parameters and self.source == other.source

    def __hash__(self):
        # https://stackoverflow.com/a/5884123/315168
        return hash((self.name, frozenset(self.parameters.items()), self.source))

    def __post_init__(self):
        assert type(self.name) == str
        assert type(self.parameters) == dict

        if self.func is not None:
            assert callable(self.func)
            validate_function_kwargs(self.func, self.parameters)

    def is_needed_for_pair(self, pair: TradingPairIdentifier) -> bool:
        """Currently indicators are calculated for spont pairs only."""
        return pair.is_spot()

    def is_per_pair(self) -> bool:
        return self.source.is_per_pair()

    def calculate_by_pair(self, input: pd.Series) -> pd.DataFrame | pd.Series:
        """Calculate the underlying indicator value.

        :param input:
            Price series used as input.

        :return:
            Single or multi series data.

            - Multi-value indicators return DataFrame with multiple columns (BB).
            - Single-value indicators return Series (RSI, SMA).

        """
        try:
            ret = self.func(input, **self.parameters)
            return self._check_good_return_value(ret)
        except Exception as e:
            raise IndicatorCalculationFailed(f"Could not calculate indicator {self.name} ({self.func}) for parameters {self.parameters}, input data is {len(input)} rows") from e

    def calculate_universe(self, input: TradingStrategyUniverse) -> pd.DataFrame | pd.Series:
        """Calculate the underlying indicator value.

        :param input:
            Price series used as input.

        :return:
            Single or multi series data.

            - Multi-value indicators return DataFrame with multiple columns (BB).
            - Single-value indicators return Series (RSI, SMA).

        """
        try:
            ret = self.func(input, **self.parameters)
            return self._check_good_return_value(ret)
        except Exception as e:
            raise IndicatorCalculationFailed(f"Could not calculate indicator {self.name} ({self.func}) for parameters {self.parameters}, input universe is {input}") from e

    def _check_good_return_value(self, df):
        assert isinstance(df, (pd.Series, pd.DataFrame)), f"Indicator did not return pd.DataFrame or pd.Series: {self.name}, we got {type(df)}"
        return df


@dataclass(slots=True, frozen=True)
class IndicatorKey:
    """Cache key used to read indicator results.

    - Used to describe all indicator combinations we need to create

    - Used as the key in the indicator result caching

    """

    #: Trading pair if this indicator is specific to a pair
    #:
    #: Note if this indicator is for the whole strategy
    #:
    pair: TradingPairIdentifier | None

    #: The definition of this indicator
    definition: IndicatorDefinition

    def __post_init__(self):
        assert isinstance(self.pair, (TradingPairIdentifier, NoneType))
        assert isinstance(self.definition, IndicatorDefinition)

    def __repr__(self):
        return f"<IndicatorKey {self.get_cache_key()}>"

    def get_cache_id(self) -> str:
        if self.pair is not None:
            return self.pair.get_ticker()
        else:
            # Indicator calculated over the universe
            assert self.definition.source == IndicatorSource.strategy_universe
            return "universe"

    def __eq__(self, other):
        return self.pair == other.pair and self.definition == other.definition

    def __hash__(self):
        return hash((self.pair, self.definition))

    def get_cache_key(self) -> str:
        if self.pair:
            slug = self.pair.get_ticker()
        else:
            slug = "universe"

        def norm_value(v):
            if isinstance(v, enum.Enum):
                v = str(v.value)
            else:
                v = str(v)
            return v

        parameters = ",".join([f"{k}={norm_value(v)}" for k, v in self.definition.parameters.items()])
        return f"{self.definition.name}({parameters})-{slug}"


class IndicatorSet:
    """Define the indicators that are needed by a trading strategy.

    - For backtesting, indicators are precalculated

    - For live trading, these indicators are recalculated for the each decision cycle

    - Indicators are calculated for each given trading pair, unless specified otherwise

    See :py:class:`CreateIndicatorsProtocol` for usage.
    """

    def __init__(self):
        #: Map indicators by the indicator name to their definition
        self.indicators: dict[str, IndicatorDefinition] = {}

    def has_indicator(self, name: str) -> bool:
        return name in self.indicators

    def get_label(self):

        if len(self.indicators) == 0:
            return "<zero indicators defined>"

        return ", ".join(k for k in self.indicators.keys())

    def get_count(self) -> int:
        """How many indicators we have"""
        return len(self.indicators)

    def get_indicator(self, name: str) -> IndicatorDefinition | None:
        """Get a named indicator definition."""
        return self.indicators.get(name)

    def add(
        self,
        name: str,
        func: Callable,
        parameters: dict | None = None,
        source: IndicatorSource=IndicatorSource.close_price,
    ):
        """Add a new indicator to this indicator set.

        Builds an indicator set for the trading strategy,
        called from `create_indicators`.

        See :py:class:`CreateIndicatorsProtocol` for usage.

        :param name:
            Name of the indicator.

            Human-readable name. If the same function is calculated multiple times, e.g. EMA,
            you can have names like `ema_short` and `ema_long`.

        :param func:
            Python function to be called.

            Function takes arguments from `parameters` dict.
            It must return either :py:class:`pd.DataFrame` or :py:class:`pd.Series`.

        :param parameters:
            Parameters to be passed to the Python function.

            Raw `func` Python arguments.

            You can pass parameters as is from `StrategyParameters`.

        :param source:
            Data source on this indicator is calculated.

            Defaults to the close price for each trading pair.
            To calculate universal indicators set to :py:attr:`IndicatorSource.strategy_universe`.
        """
        assert type(name) == str
        assert callable(func), f"{func} is not callable"

        if parameters is None:
            parameters = {}
        assert type(parameters) == dict, f"parameters must be dictionary, we got {parameters.__class__}"
        assert isinstance(source, IndicatorSource), f"Expected IndicatorSource, got {type(source)}"
        assert name not in self.indicators, f"Indicator {name} already added"
        self.indicators[name] = IndicatorDefinition(name, func, parameters, source)

    def iterate(self) -> Iterable[IndicatorDefinition]:
        yield from self.indicators.values()

    def generate_combinations(self, strategy_universe: TradingStrategyUniverse) -> Iterable[IndicatorKey]:
        """Create all indiviual indicator (per pair) we need to calculate for this trading universe."""
        for name, indicator in self.indicators.items():
            if indicator.is_per_pair():
                for pair in strategy_universe.iterate_pairs():
                    yield IndicatorKey(pair, indicator)
            else:
                yield IndicatorKey(None, indicator)

    @staticmethod
    def from_indicator_keys(indicator_keys: set["IndicatorKey"]) -> "IndicatorSet":
        """Reconstruct the original indicator set from keys.

        - Used when grid search passes data around processes
        """
        indicator_set = IndicatorSet()
        indicator_set.indicators = {key.definition.name: key.definition for key in indicator_keys}
        return indicator_set


class CreateIndicatorsProtocolV1(Protocol):
    """Call signature for create_indicators function.

    This Protocol class defines `create_indicators()` function call signature.
    Strategy modules and backtests can provide on `create_indicators` function
    to define what indicators a strategy needs.
    Used with :py:class`IndicatorSet` to define the indicators
    the strategy can use.

    .. note ::

        Legacy. See :py:class:`CreateIndicatorsProtocol2` instead.

    These indicators are precalculated and cached for fast performance.

    Example for a grid search:

    .. code-block:: python

        class MyParameters:
            stop_loss_pct = [0.9, 0.95]
            cycle_duration = CycleDuration.cycle_1d
            initial_cash = 10_000

            # Indicator values that are searched in the grid search
            slow_ema_candle_count = 7
            fast_ema_candle_count = [1, 2]


        def create_indicators(parameters: StrategyParameters, indicators: IndicatorSet, strategy_universe: TradingStrategyUniverse, execution_context: ExecutionContext):
            indicators.add("slow_ema", pandas_ta.ema, {"length": parameters.slow_ema_candle_count})
            indicators.add("fast_ema", pandas_ta.ema, {"length": parameters.fast_ema_candle_count})

    Indicators can be custom, and do not need to be calculated per trading pair.
    Here is an example of creating indicators "ETH/BTC price" and "ETC/BTC price RSI with length of 20 bars":

    .. code-block:: python

        def calculate_eth_btc(strategy_universe: TradingStrategyUniverse):
            weth_usdc = strategy_universe.get_pair_by_human_description((ChainId.ethereum, "test-dex", "WETH", "USDC"))
            wbtc_usdc = strategy_universe.get_pair_by_human_description((ChainId.ethereum, "test-dex", "WBTC", "USDC"))
            btc_price = strategy_universe.data_universe.candles.get_candles_by_pair(wbtc_usdc.internal_id)
            eth_price = strategy_universe.data_universe.candles.get_candles_by_pair(weth_usdc.internal_id)
            series = eth_price["close"] / btc_price["close"]  # Divide two series
            return series

        def calculate_eth_btc_rsi(strategy_universe: TradingStrategyUniverse, length: int):
            weth_usdc = strategy_universe.get_pair_by_human_description((ChainId.ethereum, "test-dex", "WETH", "USDC"))
            wbtc_usdc = strategy_universe.get_pair_by_human_description((ChainId.ethereum, "test-dex", "WBTC", "USDC"))
            btc_price = strategy_universe.data_universe.candles.get_candles_by_pair(wbtc_usdc.internal_id)
            eth_price = strategy_universe.data_universe.candles.get_candles_by_pair(weth_usdc.internal_id)
            eth_btc = eth_price["close"] / btc_price["close"]
            return pandas_ta.rsi(eth_btc, length=length)

        def create_indicators(parameters: StrategyParameters, indicators: IndicatorSet, strategy_universe: TradingStrategyUniverse, execution_context: ExecutionContext):
            indicators.add("eth_btc", calculate_eth_btc, source=IndicatorSource.strategy_universe)
            indicators.add("eth_btc_rsi", calculate_eth_btc_rsi, parameters={"length": parameters.eth_btc_rsi_length}, source=IndicatorSource.strategy_universe)
    """

    def __call__(
        self,
        parameters: StrategyParameters,
        indicators: IndicatorSet,
        strategy_universe: TradingStrategyUniverse,
        execution_context: ExecutionContext,
    ):
        """Build technical indicators for the strategy.

        :param parameters:
            Passed from the backtest / live strategy parametrs.

            If doing a grid search, each paramter is simplified.

        :param indicators:
            Indicator builder helper class.

            Call :py:meth:`IndicatorBuilder.create` to add new indicators to the strategy.

        :param strategy_universe:
            The loaded strategy universe.

            Use to resolve symbolic pair information if needed

        :param execution_context:
            Information about if this is a live or backtest run.

        :return:
            This function does not return anything.

            Instead `indicators.add` is used to attach new indicators to the strategy.

        """


class CreateIndicatorsProtocolV2(Protocol):
    """Call signature for create_indicators function.

    This Protocol class defines `create_indicators()` function call signature.
    Strategy modules and backtests can provide on `create_indicators` function
    to define what indicators a strategy needs.
    Used with :py:class`IndicatorSet` to define the indicators
    the strategy can use.

    This protocol class is second (v2) iteration of the function signature.

    These indicators are precalculated and cached for fast performance.

    Example for a grid search:

    .. code-block:: python

        class Parameters:
            stop_loss_pct = [0.9, 0.95]
            cycle_duration = CycleDuration.cycle_1d
            initial_cash = 10_000

            # Indicator values that are searched in the grid search
            slow_ema_candle_count = 7
            fast_ema_candle_count = [1, 2]


        def create_indicators(
            timestamp: datetime.datetime | None,
            parameters: StrategyParameters,
            strategy_universe: TradingStrategyUniverse,
            execution_context: ExecutionContext
        ):
            indicators = IndicatorSet()
            indicators.add("slow_ema", pandas_ta.ema, {"length": parameters.slow_ema_candle_count})
            indicators.add("fast_ema", pandas_ta.ema, {"length": parameters.fast_ema_candle_count})
            return indicators

    Indicators can be custom, and do not need to be calculated per trading pair.
    Here is an example of creating indicators "ETH/BTC price" and "ETC/BTC price RSI with length of 20 bars":

    .. code-block:: python

        def calculate_eth_btc(strategy_universe: TradingStrategyUniverse):
            weth_usdc = strategy_universe.get_pair_by_human_description((ChainId.ethereum, "test-dex", "WETH", "USDC"))
            wbtc_usdc = strategy_universe.get_pair_by_human_description((ChainId.ethereum, "test-dex", "WBTC", "USDC"))
            btc_price = strategy_universe.data_universe.candles.get_candles_by_pair(wbtc_usdc.internal_id)
            eth_price = strategy_universe.data_universe.candles.get_candles_by_pair(weth_usdc.internal_id)
            series = eth_price["close"] / btc_price["close"]  # Divide two series
            return series

        def calculate_eth_btc_rsi(strategy_universe: TradingStrategyUniverse, length: int):
            weth_usdc = strategy_universe.get_pair_by_human_description((ChainId.ethereum, "test-dex", "WETH", "USDC"))
            wbtc_usdc = strategy_universe.get_pair_by_human_description((ChainId.ethereum, "test-dex", "WBTC", "USDC"))
            btc_price = strategy_universe.data_universe.candles.get_candles_by_pair(wbtc_usdc.internal_id)
            eth_price = strategy_universe.data_universe.candles.get_candles_by_pair(weth_usdc.internal_id)
            eth_btc = eth_price["close"] / btc_price["close"]
            return pandas_ta.rsi(eth_btc, length=length)

        def create_indicators(parameters: StrategyParameters, strategy_universe: TradingStrategyUniverse, execution_context: ExecutionContext) -> IndicatorSet:
            indicators = IndicatorSet()
            indicators.add("eth_btc", calculate_eth_btc, source=IndicatorSource.strategy_universe)
            indicators.add("eth_btc_rsi", calculate_eth_btc_rsi, parameters={"length": parameters.eth_btc_rsi_length}, source=IndicatorSource.strategy_universe)
            return indicators
    """

    def __call__(
        self,
        timestamp: datetime.datetime | None,
        parameters: StrategyParameters,
        strategy_universe: TradingStrategyUniverse,
        execution_context: ExecutionContext,
    ) -> IndicatorSet:
        """Build technical indicators for the strategy.

        :param timestamp:
            The current live execution timestamp.

            Set ``None`` for backtesting, as `create_indicators()` is called only once during the backtest setup.

        :param parameters:
            Passed from the backtest / live strategy parametrs.

            If doing a grid search, each paramter is simplified.

        :param strategy_universe:
            The loaded strategy universe.

            Use to resolve symbolic pair information if needed

        :param execution_context:
            Information about if this is a live or backtest run.

        :return:
            Indicators the strategy is going to need.
        """


#: Use this in function singatures
CreateIndicatorsProtocol: TypeAlias = CreateIndicatorsProtocolV1 | CreateIndicatorsProtocolV2


def call_create_indicators(
    create_indicators_func: Callable,
    parameters: StrategyParameters,
    strategy_universe: TradingStrategyUniverse,
    execution_context: ExecutionContext,
    timestamp: datetime.datetime = None,
) -> IndicatorSet:
    """Backwards compatible wrapper for create_indicators().

    - Check `create_indicators_func` version

    - Handle legacy / backwards compat
    """
    assert callable(create_indicators_func)
    args = inspect.getfullargspec(create_indicators_func)
    if "indicators" in args.args:
        # v1 backwards
        indicators = IndicatorSet()
        create_indicators_func(parameters, indicators, strategy_universe, execution_context)
        return indicators

    # v2
    return create_indicators_func(timestamp, parameters, strategy_universe, execution_context)


@dataclass
class IndicatorResult:
    """One result of an indicator calculation we can store on a disk.

    - Allows storing and reading output of a single precalculated indicator

    - Parameters is a single combination of parameters
    """

    #: The universe for which we calculated the result
    #:
    #:
    universe_key: UniverseCacheKey

    #: The pair for which this result was calculated
    #:
    #: Set to ``None`` for indicators without a trading pair, using
    #: :py:attr:`IndicatorSource.strategy_universe`
    #:
    indicator_key: IndicatorKey

    #: Indicator output is one time series, but in some cases can be multiple as well.
    #:
    #: For example BB indicator calculates multiple series from one close price value.
    #:
    #:
    data: pd.DataFrame | pd.Series

    #: Was this indicator result cached or calculated on this run.
    #:
    #: Always cached in a grid search, as indicators are precalculated.
    #:
    cached: bool

    @property
    def pair(self) -> TradingPairIdentifier:
        return self.indicator_key.pair

    @property
    def definition(self) -> IndicatorDefinition:
        return self.indicator_key.definition


IndicatorResultMap: TypeAlias = dict[IndicatorKey, IndicatorResult]


class IndicatorStorage(ABC):
    """Base class for cached indicators and live trading indicators."""

    @abstractmethod
    def is_available(self, key: IndicatorKey) -> bool:
        pass

    @abstractmethod
    def load(self, key: IndicatorKey) -> IndicatorResult:
        pass

    @abstractmethod
    def save(self, key: IndicatorKey, df: pd.DataFrame | pd.Series) -> IndicatorResult:
        pass

    @abstractmethod
    def get_disk_cache_path(self) -> Path | None:
        pass

    @abstractmethod
    def get_universe_cache_path(self) -> Path | None:
        pass


class DiskIndicatorStorage(IndicatorStorage):
    """Store calculated indicator results on disk.

    Used in

    - Backtesting

    - Grid seacrh

    Indicators are calculated once and the calculation results can be recycled across multiple backtest runs.

    TODO: Cannot handle multichain universes at the moment, as serialises trading pairs by their ticker.
    """

    def __init__(self, path: Path, universe_key: UniverseCacheKey):
        assert isinstance(path, Path)
        assert type(universe_key) == str
        self.path = path
        self.universe_key = universe_key

    def __repr__(self):
        return f"<IndicatorStorage at {self.path}>"

    def get_universe_cache_path(self) -> Path:
        return self.path / Path(self.universe_key)

    def get_disk_cache_path(self) -> Path:
        return self.path

    def get_indicator_path(self, key: IndicatorKey) -> Path:
        """Get the Parquet file where the indicator data is stored.

        :return:
            Example `/tmp/.../test_indicators_single_backtes0/ethereum,1d,WETH-USDC-WBTC-USDC,2021-06-01-2021-12-31/sma(length=21).parquet`
        """
        assert isinstance(key, IndicatorKey)
        return self.get_universe_cache_path() / Path(f"{key.get_cache_key()}.parquet")

    def is_available(self, key: IndicatorKey) -> bool:
        return self.get_indicator_path(key).exists()

    def load(self, key: IndicatorKey) -> IndicatorResult:
        """Load cached indicator data from the disk."""
        assert self.is_available(key), f"Data does not exist: {key}"
        path = self.get_indicator_path(key)
        df = pd.read_parquet(path)

        if len(df.columns) == 1:
            # Convert back to series
            df = df[df.columns[0]]

        return IndicatorResult(
            self.universe_key,
            key,
            df,
            cached=True,
        )

    def save(self, key: IndicatorKey, df: pd.DataFrame | pd.Series) -> IndicatorResult:
        """Atomic replacement of the existing data.

        - Avoid leaving partially written files
        """
        assert isinstance(key, IndicatorKey)

        if isinstance(df, pd.Series):
            # For saving, create a DataFrame with a single column "value"
            save_df = pd.DataFrame({"value": df})
        else:
            save_df = df

        assert isinstance(save_df, pd.DataFrame), f"Expected DataFrame, got: {type(df)}"
        path = self.get_indicator_path(key)
        dirname, basename = os.path.split(path)

        os.makedirs(dirname, exist_ok=True)

        temp = tempfile.NamedTemporaryFile(mode='wb', delete=False, dir=dirname)
        save_df.to_parquet(temp)
        temp.close()
        # https://stackoverflow.com/a/3716361/315168
        shutil.move(temp.name, path)

        logger.info("Saved %s", path)

        return IndicatorResult(
            universe_key=self.universe_key,
            indicator_key=key,
            data=df,
            cached=False,
        )

    @staticmethod
    def create_default(
        universe: TradingStrategyUniverse,
        default_path=DEFAULT_INDICATOR_STORAGE_PATH,
    ) -> "DiskIndicatorStorage":
        """Get the indicator storage with the default cache path."""
        return DiskIndicatorStorage(default_path, universe.get_cache_key())


class MemoryIndicatorStorage(IndicatorStorage):
    """Store calculated indicator results on disk.

    Used in

    - Live trading

    - Indicators are calculated just before `decide_trades()` is called

    - Indicators are recalculated on every decision cycle
    """

    def __init__(self, universe_key: UniverseCacheKey):
        self.universe_key = universe_key
        self.results: dict[IndicatorKey, IndicatorResult] = {}

    def is_available(self, key: IndicatorKey) -> bool:
        return key in self.results

    def load(self, key: IndicatorKey) -> IndicatorResult:
        return self.results[key]

    def save(self, key: IndicatorKey, df: pd.DataFrame | pd.Series) -> IndicatorResult:
        result = IndicatorResult(
            universe_key=self.universe_key,
            indicator_key=key,
            data=df,
            cached=False,
        )

        self.results[key] = result
        return result

    def get_disk_cache_path(self) -> Path | None:
        return None

    def get_universe_cache_path(self) -> Path | None:
        return None


def _serialise_parameters_for_cache_key(parameters: dict) -> str:
    for k, v in parameters.items():
        assert type(k) == str
        assert type(v) not in (list, tuple)  # Don't leak test ranges here - must be a single value
    return "".join([f"{k}={v}" for k, v in parameters.items()])


def _load_indicator_result(storage: DiskIndicatorStorage, key: IndicatorKey) -> IndicatorResult:
    logger.info("Loading %s", key)
    assert storage.is_available(key), f"Tried to load indicator that is not in the cache: {key}"
    return storage.load(key)


def _calculate_and_save_indicator_result(
    strategy_universe: TradingStrategyUniverse,
    storage: DiskIndicatorStorage,
    key: IndicatorKey,
) -> IndicatorResult:

    indicator = key.definition

    if indicator.is_per_pair():
        match indicator.source:
            case IndicatorSource.open_price:
                column = "open"
            case IndicatorSource.close_price:
                column = "close"
            case _:
                raise AssertionError(f"Unsupported input source {key.pair} {key.definition} {indicator.source}")

        assert key.pair.internal_id

        input = strategy_universe.data_universe.candles.get_samples_by_pair(key.pair.internal_id)[column]
        data = indicator.calculate_by_pair(input)

    else:
        data = indicator.calculate_universe(strategy_universe)

    assert data is not None, f"Indicator function {indicator.name} ({indicator.func}) did not return any result, received Python None instead"

    result = storage.save(key, data)
    return result


def load_indicators(
    strategy_universe: TradingStrategyUniverse,
    storage: DiskIndicatorStorage,
    indicator_set: IndicatorSet,
    all_combinations: set[IndicatorKey],
    max_readers=8,
    show_progress=True,
) -> IndicatorResultMap:
    """Load cached indicators.

    - Use a thread pool to speed up IO

    :param all_combinations:
        Load all cached indicators of this set if they are available in the storage.

    :param storage:
        The cache backend we use for the storage

    :param max_readers:
        Number of reader threads we allocate for the task
    """

    task_args = []
    for key in all_combinations:
        if storage.is_available(key):
            task_args.append((storage, key))

    logger.info(
        "Loading cached indicators indicators, we have %d indicator combinations out of %d available in the cache %s",
        len(task_args),
        len(all_combinations),
        storage.get_universe_cache_path()
    )

    if len(task_args) == 0:
        return {}

    results = {}
    label = indicator_set.get_label()
    key: IndicatorKey

    if show_progress:
        progress_bar = tqdm(total=len(task_args), desc=f"Reading cached indicators {label} for {strategy_universe.get_pair_count()} pairs, using {max_readers} threads")
    else:
        progress_bar = None

    try:
        if max_readers > 1:
            logger.info("Multi-thread reading")

            executor = futureproof.ThreadPoolExecutor(max_workers=max_readers)
            tm = futureproof.TaskManager(executor, error_policy=futureproof.ErrorPolicyEnum.RAISE)

            # Run the checks parallel using the thread pool
            tm.map(_load_indicator_result, task_args)

            # Extract results from the parallel task queue
            for task in tm.as_completed():
                result = task.result
                key = result.indicator_key
                assert key not in results
                results[key] = result
                if progress_bar:
                    progress_bar.update()
        else:
            logger.info("Single-thread reading")
            for result in itertools.starmap(_load_indicator_result, task_args):
                key = result.indicator_key
                assert key not in results
                results[key] = result

        return results
    finally:
        if progress_bar:
            progress_bar.close()


def calculate_indicators(
    strategy_universe: TradingStrategyUniverse,
    storage: DiskIndicatorStorage,
    indicators: IndicatorSet | None,
    execution_context: ExecutionContext,
    remaining: set[IndicatorKey],
    max_workers=8,
    label: str | None = None,
    verbose=True,
) -> IndicatorResultMap:
    """Calculate indicators for which we do not have cached data yet.

    - Use a thread pool to speed up IO

    :param indicators:
        Indicator set we calculate for.

        Can be ``None`` for a grid search, as each individual combination may has its own set.

    :param remaining:
        Remaining indicator combinations for which we do not have a cached rresult

    :param verbose:
        Stdout user printing with helpful messages.
    """

    assert isinstance(execution_context, ExecutionContext), f"Expected ExecutionContext, got {type(execution_context)}"

    results: IndicatorResultMap

    if label is None:
        if indicators is not None:
            label = indicators.get_label()
        else:
            label = "Indicator calculation"

    logger.info("Calculating indicators: %s", label)

    if len(remaining) == 0:
        logger.info("Nothing to calculate")
        return {}

    task_args = []
    for key in remaining:
        task_args.append((strategy_universe, storage, key))

    results = {}

    if max_workers > 1:

        # Do a parallel scan for the maximum speed
        #
        # Set up a futureproof task manager
        #
        # For futureproof usage see
        # https://github.com/yeraydiazdiaz/futureproof

        #
        # Run individual searchers in child processes
        #

        # Copy universe data to child processes only once when the child process is created
        #
        pickled_universe = pickle.dumps(strategy_universe)
        logger.info("Doing a multiprocess indicator calculation, picked universe is %d bytes", len(pickled_universe))

        # Set up a process pool executing structure
        executor = futureproof.ProcessPoolExecutor(max_workers=max_workers, initializer=_process_init, initargs=(pickled_universe,))
        tm = futureproof.TaskManager(executor, error_policy=futureproof.ErrorPolicyEnum.RAISE)

        # Set up a signal handler to stop child processes on quit
        setup_indicator_multiprocessing(executor)

        # Run the tasks
        tm.map(_calculate_and_save_indicator_result, task_args)

        # Track the child process completion using tqdm progress bar
        with tqdm(total=len(task_args), desc=f"Calculating indicators {label} using {max_workers} processes") as progress_bar:
            # Extract results from the parallel task queue
            for task in tm.as_completed():
                result = task.result
                results[result.indicator_key] = result
                progress_bar.update()

    else:
        # Do single thread - good for debuggers like pdb/ipdb
        #

        _universe = strategy_universe

        logger.info("Doing a single thread indicator calculation")
        iter = itertools.starmap(_calculate_and_save_indicator_result, task_args)

        # Force workers to finish
        result: IndicatorResult
        for result in iter:
            results[result.indicator_key] = result

    logger.info("Total %d indicator results calculated", len(results))

    return results


def prepare_indicators(
    create_indicators: CreateIndicatorsProtocol,
    parameters: StrategyParameters,
    strategy_universe: TradingStrategyUniverse,
    execution_context: ExecutionContext,
    timestamp: datetime.datetime = None,
):
    """Call the strategy module indicator builder."""

    indicators = call_create_indicators(
        create_indicators,
        parameters,
        strategy_universe,
        execution_context,
        timestamp=timestamp,
    )

    if indicators.get_count() == 0:
        # TODO: Might have legit use cases?
        logger.warning(f"create_indicators() did not create a single indicator")

    return indicators


def calculate_and_load_indicators(
    strategy_universe: TradingStrategyUniverse,
    storage: IndicatorStorage,
    execution_context: ExecutionContext,
    parameters: StrategyParameters | None = None,
    indicators: IndicatorSet | None = None,
    create_indicators: CreateIndicatorsProtocolV1 | None = None,
    max_workers: int | Callable = get_safe_max_workers_count,
    max_readers: int | Callable = get_safe_max_workers_count,
    verbose=True,
    timestamp: datetime.datetime = None,
) -> IndicatorResultMap:
    """Precalculate all indicators.

    - Calculate indicators using multiprocessing

    - Display TQDM progress bars for loading cached indicators and calculating new ones

    - Use cached indicators if available

    :param cache_warmup_only:
        Only fill the disk cache, do not load results in the memory.

    :param verbose:
        Stdout printing with heplful messages to the user
    """

    # Resolve CPU count
    if callable(max_workers):
        max_workers = max_workers()

    if callable(max_readers):
        max_readers = max_readers()

    assert create_indicators or indicators, "You must give either create_indicators or indicators argument"

    if create_indicators:
        assert indicators is None, f"Give either indicators or create_indicators, not both"
        assert parameters is not None, f"parameters argument must be given if you give create_indicators"
        indicators = prepare_indicators(create_indicators, parameters, strategy_universe, execution_context, timestamp=timestamp)

    assert isinstance(indicators, IndicatorSet), f"Got class {type(indicators)} when IndicatorSet expected"

    all_combinations = set(indicators.generate_combinations(strategy_universe))

    logger.info("Loading indicators %s for the universe %s, storage is %s", indicators.get_label(), strategy_universe.get_cache_key(), storage.get_disk_cache_path())
    cached = load_indicators(strategy_universe, storage, indicators, all_combinations, max_readers=max_readers)

    for key in cached.keys():
        # Check we keyed this right
        assert key in all_combinations, f"Loaded a cached result {key} is not in part of the all combinations we expected"

    if verbose:
        if len(all_combinations) > 0:
            print(f"Using indicator cache {storage.get_universe_cache_path()}")

    calculation_needed = all_combinations - set(cached.keys())
    calculated = calculate_indicators(
        strategy_universe,
        storage,
        indicators,
        execution_context,
        calculation_needed,
        max_workers=max_workers,
    )

    result = cached | calculated

    for key in result.keys():
        # Check we keyed this right
        assert key in all_combinations

    return result


def warm_up_indicator_cache(
    strategy_universe: TradingStrategyUniverse,
    storage: DiskIndicatorStorage,
    execution_context: ExecutionContext,
    indicators: set[IndicatorKey],
    max_workers=8,
) -> tuple[set[IndicatorKey], set[IndicatorKey]]:
    """Precalculate all indicators.

    - Used for grid search

    - Calculate indicators using multiprocessing

    - Display TQDM progress bars for loading cached indicators and calculating new ones

    - Use cached indicators if available

    :return:
        Tuple (Cached indicators, calculated indicators)
    """

    cached = set()
    needed = set()
    pair_indicators_needed = set()
    universe_indicators_needed = set()

    for key in indicators:
        if storage.is_available(key):
            cached.add(key)
        else:
            needed.add(key)
            if key.pair is None:
                universe_indicators_needed.add(key)
            else:
                pair_indicators_needed.add(key)

    logger.info(
        "warm_up_indicator_cache(), we have %d cached pair-indicators results and need to calculate %d results, of which %d pair indicators and %d universe indicators",
        len(cached),
        len(needed),
        len(pair_indicators_needed),
        len(universe_indicators_needed),
    )

    calculated = calculate_indicators(
        strategy_universe,
        storage,
        None,
        execution_context,
        needed,
        max_workers=max_workers,
        label=f"Calculating {len(needed)} indicators for the grid search"
    )

    logger.info(
        "Calculated %d indicator results",
        len(calculated),
    )

    return cached, needed


#: Process global stored universe for multiprocess workers
_universe: TradingStrategyUniverse | None = None

_process_pool: concurrent.futures.ProcessPoolExecutor | None = None

def _process_init(pickled_universe):
    """Child worker process initialiser."""
    # Transfer ove the universe to the child process
    global _universe
    _universe = pickle.loads(pickled_universe)


def _handle_sigterm(*args):
    # TODO: Despite all the effort, this does not seem to work with Visual Studio Code's Interrupt Kernel button
    processes: list[Process] = list(_process_pool._processes.values())
    _process_pool.shutdown()
    for p in processes:
        p.kill()
    sys.exit(1)


def setup_indicator_multiprocessing(executor):
    """Set up multiprocessing for indicators.

    - We use multiprocessing to calculate indicators

    - We want to be able to abort (CTRL+C) gracefully
    """
    global _process_pool_executor
    _process_pool_executor = executor._executor

    # Enable graceful multiprocessing termination only if we run as a backtesting noteboook
    # pytest work around for: test_trading_strategy_engine_v050_live_trading
    # ValueError: signal only works in main thread of the main interpreter
    # https://stackoverflow.com/a/23207116/315168
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _handle_sigterm)


def validate_function_kwargs(func: Callable, kwargs: dict):
    """Check that we can pass the given kwargs to a function.

    Designed to be used with pandas_ta functions -
    many special cases needs to be added.

    :param func:
        TA function

    :param kwargs:
        Parameters we think function can take

    :raise IndicatorFunctionSignatureMismatch:
        You typoed
    """

    # https://stackoverflow.com/a/64504028/315168

    assert callable(func)

    sig = inspect.signature(func)
    allowed_params = sig.parameters

    for our_param, our_value in kwargs.items():
        if our_param not in allowed_params:
            raise IndicatorFunctionSignatureMismatch(f"Function {func} does not take argument {our_param}. Available arguments are: {allowed_params}.")

