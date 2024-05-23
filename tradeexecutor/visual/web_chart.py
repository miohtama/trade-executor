"""Chart generation for the web frontend."""
import enum
from dataclasses import dataclass
from typing import List, Tuple

import pandas as pd
from dataclasses_json import dataclass_json
from eth_defi.utils import to_unix_timestamp

from tradeexecutor.state.state import State
from tradeexecutor.visual.equity_curve import calculate_compounding_realised_trading_profitability, calculate_equity_curve, calculate_investment_flow, \
    calculate_compounding_unrealised_trading_profitability


class WebChartType(enum.Enum):
    """Different charts we can generate for frontend rendering."""

    #: See :ref:`profitability`
    compounding_realised_profitability = "compounding_realised_profitability"

    #: See :ref:`profitability`
    realised_profitability = "realised_profitability"

    #: Deposits and redemptions
    netflow = "netflow"

    #: Total equity curve
    total_equity = "total_equity"


class WebChartSource(enum.Enum):
    """Are we rendering backtest or live trading"""
    live_trading = "live_trading"
    backtest = "backtest"



class TimeWindow:
    """If frontend asks for a specific history period."""
    all = "all"


@dataclass_json
@dataclass
class WebChart:
    """JSONnable chart reply.

    Contain data and metadata for the frontend chart rendering:
    """

    #: UNIX timestamp, value tuples
    data: List[Tuple[float, float]]

    #: Chart description
    title: str

    #: Help link "Learn more"
    help_link: str

    #: Time window for this was generated
    time_window = TimeWindow.all

    source: WebChartSource = WebChartSource.live_trading


def render_web_chart(
    state: State,
    type: WebChartType,
    source: WebChartSource,
) -> WebChart:
    """Render one of various web charts.

    Used to feed Plotly.js data structures to the frontend.
    """

    match type:
        case WebChartType.compounding_realised_profitability:
            df = calculate_compounding_unrealised_trading_profitability(state)
            description = "Compounded unrealised trading position % profitability"
            help_link = "https://tradingstrategy.ai/glossary/profitability"
        case WebChartType.total_equity:
            df = calculate_equity_curve(state, fill_time_gaps=True)
            description = "Total equity"
            help_link = "https://tradingstrategy.ai/glossary/total-equity"
        case WebChartType.netflow:
            df = calculate_investment_flow(state)
            description = "Netflow"
            help_link = "https://tradingstrategy.ai/glossary/netflow"
        case _:
            raise NotImplementedError(f"{type}")

    # Convert
    return _export_chart(df, description, help_link, source)


def export_time_series(series: pd.Series) -> List[Tuple[float, float]]:
    """Export Pandas series for web frontend rendering."""

    if len(series.index) == 0:
        return []

    assert isinstance(series.index, pd.DatetimeIndex), f"Got index: {series.index.__class__}"
    return [(index.timestamp(), value) for index, value in series.items()]


def _export_chart(
        series: pd.Series,
        title: str,
        help_link: str,
        source: WebChartSource,
) -> WebChart:
    """Convert to our time-indexed series to JSON/frontend f riendly format."""
    data = export_time_series(series)
    return WebChart(
        data,
        title,
        help_link,
        source=source,
    )

