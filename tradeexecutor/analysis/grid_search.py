"""Grid search result analysis.

- Breaddown of performance of different grid search combinations

- Heatmap and other comparison methods

"""
import textwrap
from typing import List

import numpy as np
import pandas as pd
from IPython.core.display_functions import display

import plotly.express as px
from plotly.graph_objs import Figure, Scatter

from tradeexecutor.backtest.grid_search import GridSearchResult
from tradeexecutor.state.types import USDollarAmount
from tradeexecutor.visual.benchmark import visualise_all_cash, visualise_portfolio_equity_curve

VALUE_COLS = ["CAGR", "Max drawdown", "Sharpe", "Sortino", "Average position", "Median position"]

PERCENT_COLS = ["CAGR", "Max drawdown", "Average position", "Median position"]

DATA_COLS = ["Positions", "Trades"]


def analyse_combination(
        r: GridSearchResult,
        min_positions_threshold: int,
) -> dict:
    """Create a grid search result table row.

    - Create columns we can use to compare different grid search combinations

    :param min_positions_threshold:
        If we did less positions than this amount, do not consider this a proper strategy.

        Filter out one position outliers.

    """

    row = {}
    param_names = []
    for param in r.combination.parameters:

        # Skip parameters that are single fixed value
        # and do not affect the grid search results
        if param.single:
            continue

        row[param.name] = param.value
        param_names.append(param.name)

    def clean(x):
        if x == "-":
            return np.NaN
        elif x == "":
            return np.NaN
        return x

    # import ipdb ; ipdb.set_trace()

    row.update({
        # "Combination": r.combination.get_label(),
        "Positions": r.summary.total_positions,
        "Trades": r.summary.total_trades,
        # "Return": r.summary.return_percent,
        # "Return2": r.summary.annualised_return_percent,
        #"Annualised profit": clean(r.metrics.loc["Expected Yearly"][0]),
        "CAGR": clean(r.metrics.loc["Annualised return (raw)"][0]),
        "Max drawdown": clean(r.metrics.loc["Max Drawdown"][0]),
        "Sharpe": clean(r.metrics.loc["Sharpe"][0]),
        "Sortino": clean(r.metrics.loc["Sortino"][0]),
        "Average position": r.summary.average_trade,
        "Median position": r.summary.median_trade,
    })

    # Clear all values except position count if this is not a good trade series
    if r.summary.total_positions < min_positions_threshold:
        for k in row.keys():
            if k != "Positions" and k not in param_names:
                row[k] = np.NaN

    return row


def analyse_grid_search_result(
        results: List[GridSearchResult],
        min_positions_threshold: int = 5,
) -> pd.DataFrame:
    """Create aa table showing grid search result of each combination.

    - Each row have labeled parameters of its combination

    - Each row has some metrics extracted from the results by :py:func:`analyse_combination`

    The output has the following row for each parameter combination:

    - Combination parameters
    - Positions and trade counts
    - CAGR (Communicative annualized growth return, compounding)
    - Max drawdown
    - Sharpe
    - Sortino

    See also :py:func:`analyse_combination`.

    :param results:
        Output from :py:meth:`tradeexecutor.backtest.grid_search.perform_grid_search`.

    :param min_positions_threshold:
        If we did less positions than this amount, do not consider this a proper strategy.

        Filter out one position outliers.

    :return:
        Table of grid search combinations
    """
    assert len(results) > 0, "No results"
    rows = [analyse_combination(r, min_positions_threshold) for r in results]
    df = pd.DataFrame(rows)
    r = results[0]
    param_names = [p.name for p in r.combination.searchable_parameters]
    df = df.set_index(param_names)
    df = df.sort_index()
    return df


def visualise_table(df: pd.DataFrame):
    """Render a grid search combination table to notebook output.

    - Highlight winners and losers

    - Gradient based on the performance of a metric

    - Stripes for the input
    """

    # https://stackoverflow.com/a/57152529/315168

    # TODO:
    # Diverge color gradient around zero
    # https://stackoverflow.com/a/60654669/315168

    formatted = df.style.background_gradient(
        axis = 0,
        subset = VALUE_COLS,
    ).highlight_min(
        color = 'pink',
        axis = 0,
        subset = VALUE_COLS,
    ).highlight_max(
        color = 'darkgreen',
        axis = 0,
        subset = VALUE_COLS,
    ).format(
        formatter="{:.2%}",
        subset = PERCENT_COLS,
    ).set_properties(
        subset=DATA_COLS, 
        **{'background-color': '#333'}
    )

    # formatted = df.style.highlight_max(
    #     color = 'lightgreen',
    #     axis = 0,
    #     subset = VALUE_COLS,
    # ).highlight_min(
    #     color = 'pink',
    #     axis = 0,
    #     subset = VALUE_COLS,
    # ).format(
    #     formatter="{:.2%}",
    #     subset = PERCENT_COLS,
    # )

    display(formatted)


def visualise_heatmap_2d(
    result: pd.DataFrame,
    parameter_1: str,
    parameter_2: str,
    metric: str,
    color_continuous_scale='Bluered_r',
    continuous_scale: bool | None = None,
) -> Figure:
    """Draw a heatmap square comparing two different parameters.

    Directly shows the resulting matplotlib figure.

    :param parameter_1:
        Y axis

    :param parameter_2:
        X axis

    :param metric:
        Value to examine

    :param result:
        Grid search results as a DataFrame.

        Created by :py:func:`analyse_grid_search_result`.

    :param color_continuous_scale:
        The name of Plotly gradient used for the colour scale.

    :param continuous_scale:
        Are the X and Y scales continuous.

        X and Y scales cannot be continuous if they contain values like None or NaN.
        This will stretch the scale to infinity or zero.

        Set `True` to force continuous, `False` to force discreet steps, `None` to autodetect.

    :return:
        Plotly Figure object
    """

    # Reset multi-index so we can work with parameter 1 and 2 as series
    df = result.reset_index()

    # Backwards compatibiltiy
    if metric == "Annualised return" and ("Annualised return" not in df.columns) and "CAGR" in df.columns:
        metric = "CAGR"

    # Detect any non-number values on axes
    if continuous_scale is None:
        continuous_scale = not(df[parameter_1].isna().any() or df[parameter_2].isna().any())

    # setting all column values to string will hint
    # Plotly to make all boxes same size regardless of value
    if not continuous_scale:
        df[parameter_1] = df[parameter_1].astype(str)
        df[parameter_2] = df[parameter_2].astype(str)

    df = df.pivot(index=parameter_1, columns=parameter_2, values=metric)

    # Format percents inside the cells and mouse hovers
    if metric in PERCENT_COLS:
        text = df.applymap(lambda x: f"{x * 100:,.2f}%")
    else:
        text = df.applymap(lambda x: f"{x:,.2f}")

    fig = px.imshow(
        df,
        labels=dict(x=parameter_2, y=parameter_1, color=metric),
        aspect="auto",
        title=metric,
        color_continuous_scale=color_continuous_scale,
    )

    fig.update_traces(text=text, texttemplate="%{text}")

    fig.update_layout(
        title={"text": metric},
        height=600,
    )
    return fig


def visualise_3d_scatter(
    flattened_result: pd.DataFrame,
    parameter_x: str,
    parameter_y: str,
    parameter_z: str,
    measured_metric: str,
    color_continuous_scale="Bluered_r",  # Reversed, blue = best
    height=600,
) -> Figure:
    """Draw a 3D scatter plot for grid search results.

    Create an interactive 3d chart to explore three different parameters and one performance measurement
    of the grid search results.

    Example:

    .. code-block:: python

        from tradeexecutor.analysis.grid_search import analyse_grid_search_result
        table = analyse_grid_search_result(grid_search_results)
        flattened_results = table.reset_index()
        flattened_results["Annualised return %"] = flattened_results["Annualised return"] * 100
        fig = visualise_3d_scatter(
            flattened_results,
            parameter_x="rsi_days",
            parameter_y="rsi_high",
            parameter_z="rsi_low",
            measured_metric="Annualised return %"
        )
        fig.show()

    :param flattened_result:
        Grid search results as a DataFrame.

        Created by :py:func:`analyse_grid_search_result`.

    :param parameter_x:
        X axis

    :param parameter_y:
        Y axis

    :param parameter_z:
        Z axis

    :param parameter_colour:
        Output we compare.

        E.g. `Annualised return`

    :param color_continuous_scale:
        The name of Plotly gradient used for the colour scale.

        `See the Plotly continuos scale color gradient options <https://plotly.com/python/builtin-colorscales/>`__.

    :return:
        Plotly figure to display
    """

    assert isinstance(flattened_result, pd.DataFrame)
    assert type(parameter_x) == str
    assert type(parameter_y) == str
    assert type(parameter_z) == str
    assert type(measured_metric) == str

    fig = px.scatter_3d(
        flattened_result,
        x=parameter_x,
        y=parameter_y,
        z=parameter_z,
        color=measured_metric,
        color_continuous_scale=color_continuous_scale,
        height=height,
    )

    return fig


def _get_hover_template(
    result: GridSearchResult,
    key_metrics = ("CAGR﹪", "Max Drawdown", "Time in Market", "Sharpe", "Sortino"),  # See quantstats
    percent_metrics = ("CAGR﹪", "Max Drawdown", "Time in Market"),
):

    # Get metrics calculated with QuantStats
    data = result.metrics["Strategy"]
    metrics = {}
    for name in key_metrics:
        metrics[name] = data[name]

    template = textwrap.dedent(f"""<b>{result.get_label()}</b><br><br>""")

    for k, v in metrics.items():
        if type(v) == int:
            v = float(v)

        if v in ("", None, "-"):  # Messy third party code does not know how to mark no value
            template += f"{k}: -<br>"
        elif k in percent_metrics:
            assert type(v) == float, f"Got unknown type: {k}: {v} ({type(v)}"
            v *= 100
            template += f"{k}: {v:.2f}%<br>"
        else:
            assert type(v) == float, f"Got unknown type: {k}: {v} ({type(v)}"
            template += f"{k}: {v:.2f}<br>"

    # Get trade metrics
    for k, v in result.summary.get_trading_core_metrics().items():
        template += f"{k}: {v}<br>"

    return template


def visualise_grid_search_equity_curves(
    results: List[GridSearchResult],
    name: str | None = None,
    benchmark_indexes: pd.DataFrame | None = None,
    height=1200,
    colour="rgba(160, 160, 160, 0.5)",
    log_y=False,
) -> Figure:
    """Draw multiple equity curves in the same chart.

    - See how all grid searched strategies work

    - Benchmark against buy and hold of various assets

    - Benchmark against hold all cash

    Example for a single trading pair strategy:

    .. code-block:: python

        TODO

    :param results:
        Results from the grid search.

    :param benchmark_indexes:
        List of other asset price series displayed on the timeline besides equity curve.

        DataFrame containing multiple series.

        - Asset name is the series name.
        - Setting `colour` for `pd.Series.attrs` allows you to override the colour of the index

    :param height:
        Chart height in pixels

    :param start_at:
        When the backtest started

    :param end_at:
        When the backtest ended

    :param additional_indicators:
        Additional technical indicators drawn on this chart.

        List of indicator names.

        The indicators must be plotted earlier using `state.visualisation.plot_indicator()`.

        **Note**: Currently not very useful due to Y axis scale

    :param log_y:
        Use logarithmic Y-axis.

        Because we accumulate larger treasury over time,
        the swings in the value will be higher later.
        We need to use a logarithmic Y axis so that we can compare the performance
        early in the strateg and late in the strategy.

    """

    if name is None:
        name = "Grid search equity curve comparison"

    fig = Figure()

    for result in results:
        curve = result.equity_curve
        label = result.get_label()
        template =_get_hover_template(result)
        scatter = Scatter(
            x=curve.index,
            y=curve,
            mode="lines",
            name="",  # Hides hover legend, use hovertext only
            line=dict(color=colour),
            showlegend=False,
            hovertemplate=template,
            hovertext=None,
        )
        fig.add_trace(scatter)

    if benchmark_indexes is not None:
        for benchmark_name, curve in benchmark_indexes.items():
            benchmark_colour = curve.attrs.get("colour", "black")
            scatter = Scatter(
                x=curve.index,
                y=curve,
                mode="lines",
                name=benchmark_name,
                line=dict(color=benchmark_colour),
                showlegend=True,
            )
            fig.add_trace(scatter)

    fig.update_layout(title=f"{name}", height=height)
    if log_y:
        fig.update_yaxes(title="Value $ (logarithmic)", showgrid=False, type="log")
    else:
        fig.update_yaxes(title="Value $", showgrid=False)
    fig.update_xaxes(rangeslider={"visible": False})

    # Move legend to the bottom so we have more space for
    # time axis in narrow notebook views
    # https://plotly.com/python/legend/
    fig.update_layout(legend=dict(
        orientation="h",
        yanchor="bottom",
        y=1.02,
        xanchor="right",
        x=1
    ))

    return fig