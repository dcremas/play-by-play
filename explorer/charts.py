"""The one chart an answer may get, or none.

Deliberately conservative. A wrong chart is worse than no chart here: the table is already
on screen and correct, so an invented axis only adds a way to misread it. Two shapes
qualify and nothing else does:

  * a TREND -- an ordered column (season, week, down...) and measures, nothing textual;
  * a RANKING -- one text label, at most 30 rows, drawn in the query's own order.

Kept apart from app.py so the choice can be tested against real result frames without a
browser. Every rule below was a wrong chart on a real answer first:

  * ID COLUMNS ARE NOT MEASURES. The system prompt tells the model to group by athlete_id
    and label with the name, so a kicker ranking arrives as (kicker_athlete_id, kicker,
    attempts, made, fg_pct) -- and the first version charted the ids.
  * A RANKING'S MEASURE IS WHAT IT WAS SORTED BY. "Most yards per rush" arrives as (team,
    rushes, yards_per_rush) ordered by the last column; the first version charted rushes.
  * A RANKING KEEPS ITS ORDER. st.bar_chart re-sorts its categories alphabetically, which
    turns a top ten into a list of names. Altair with sort=None does not.
  * A LABEL COLUMN RULES OUT A TREND. `yards` is an ordered word, but (team, yards, ypc)
    is a ranking, and a line through teams by total yards means nothing.
"""
from __future__ import annotations

import altair as alt
import pandas as pd

# Column names that mean "this is the x axis, and it is ordered". The system prompt tells
# the model to alias its bucket clearly -- `AS season`, `AS week` -- and this is the other
# half of that bargain: a vague alias costs the reader a chart, so the instruction is only
# honest if something here actually looks for the name.
_ORDERED_X = ("season", "year", "week", "game_month", "month", "day", "date", "bucket",
              "fg_dist_bucket", "down", "distance", "quarter", "period", "yards")

# A year is a LABEL, not a quantity: on a numeric axis it reads "2,014". These are drawn
# as ordered categories instead.
_CATEGORICAL_X = ("season", "year", "week", "game_month", "month", "down", "quarter",
                  "period", "fg_dist_bucket")

_RATE_HINTS = ("pct", "rate", "percent", "share", "avg", "per_", "_per", "mean")


def _is_id(col) -> bool:
    name = str(col).lower()
    return name == "id" or name.endswith(("_id", "_uid"))


def _is_rate(col) -> bool:
    return any(h in str(col).lower() for h in _RATE_HINTS)


def _monotonic(series: pd.Series) -> bool:
    s = series.dropna()
    return len(s) > 1 and (s.is_monotonic_increasing or s.is_monotonic_decreasing)


def _ranking_measure(df: pd.DataFrame, numeric: list) -> str:
    """The column the ranking is about: the sort key, preferring a rate among ties."""
    ordered = [c for c in numeric if _monotonic(df[c])]
    for pool in ([c for c in ordered if _is_rate(c)], ordered,
                 [c for c in numeric if _is_rate(c)]):
        if pool:
            return pool[0]
    return numeric[-1]


def pick(df: pd.DataFrame) -> alt.Chart | None:
    """An Altair chart for this result, or None when the shape does not obviously want one."""
    if df.empty or len(df.columns) < 2 or len(df) < 2 or len(df) > 500:
        return None

    cols = [c for c in df.columns if not _is_id(c)]
    numeric = [c for c in cols
               if pd.api.types.is_numeric_dtype(df[c]) and df[c].notna().any()]
    labels = [c for c in cols if c not in numeric]
    if not numeric:
        return None

    lowered = {str(c).lower(): c for c in cols}
    x = next((lowered[name] for name in _ORDERED_X if name in lowered), None)

    if x is not None and not [c for c in labels if c != x]:
        # The axis is usually numeric itself -- `season` is an integer -- so it is removed
        # from the measures rather than used to disqualify the chart.
        measures = [c for c in numeric if c != x]
        if not measures:
            return None
        # RATES AND COUNTS DO NOT SHARE AN AXIS. A frame with total_kickoffs (9,000) and
        # touchback_rate (51.5) plots the rate as a flat line along the bottom. When the
        # result has both, chart the rates: the question was almost always about the rate,
        # which is why the model computed one.
        measures = ([c for c in measures if _is_rate(c)] or measures)[:3]
        kind = "O" if str(x).lower() in _CATEGORICAL_X else "Q"
        long = df[[x] + measures].melt(x, var_name="measure", value_name="value")
        long[x] = long[x].astype(str) if kind == "O" else long[x]
        return (
            alt.Chart(long)
            .mark_line(point=True, strokeWidth=2.5)
            .encode(
                x=alt.X(f"{x}:{kind}", title=str(x).replace("_", " "),
                        sort=None if kind == "Q" else "ascending",
                        axis=alt.Axis(labelAngle=0, labelOverlap=True)),
                y=alt.Y("value:Q", title=None, scale=alt.Scale(zero=False)),
                color=alt.Color("measure:N", title=None,
                                legend=alt.Legend(orient="top") if len(measures) > 1 else None),
                tooltip=[alt.Tooltip(f"{x}:{kind}"), "measure:N",
                         alt.Tooltip("value:Q", format=",.4~f")],
            )
            .properties(height=260)
        )

    if len(labels) == 1 and len(df) <= 30:
        label = labels[0]
        # Two people with one name would be stacked into a single bar. The table shows
        # them apart; a chart that silently merges them is the wrong chart.
        if df[label].duplicated().any() or df[label].isna().any():
            return None
        measure = _ranking_measure(df, numeric)
        plot = df[[label, measure]].copy()
        plot[label] = plot[label].astype(str)
        return (
            alt.Chart(plot)
            .mark_bar(cornerRadiusEnd=3)
            .encode(
                # Every label, in full: Vega's default culls overlapping labels (every
                # other team vanished) and cuts long ones at 100px ("Jacksonville Ja…").
                y=alt.Y(f"{label}:N", sort=None, title=None,
                        axis=alt.Axis(labelOverlap=False, labelLimit=220)),
                x=alt.X(f"{measure}:Q", title=str(measure).replace("_", " ")),
                tooltip=[f"{label}:N", alt.Tooltip(f"{measure}:Q", format=",.4~f")],
            )
            .properties(height=max(120, 28 * len(plot)))
        )

    return None
