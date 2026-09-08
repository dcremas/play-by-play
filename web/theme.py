"""Design tokens, validated palettes and shared Plotly / AG-Grid defaults.

The categorical hues come from the reference data-viz palette. Every sequence used
in this app was run through `validate_palette.js` against the surface it actually
renders on, in both modes:

  kick outcomes  blue orange aqua yellow magenta violet
                 light: CVD dE 9.1, normal dE 19.6, contrast WARN on aqua/yellow/magenta
                 dark:  CVD dE 8.4, normal dE 19.3, all >= 3:1
  fg outcomes    blue(made) red(missed) violet(blocked)   -- the documented diverging
                 pair, because made/missed is a polarity, not an identity
                 light: CVD dE 21.6, normal dE 32.3       dark: CVD dE 19.2

Two obligations follow from those runs and are honoured in the charts:
  * the light-mode contrast WARN triggers the relief rule -> stacked segments carry
    visible direct labels, and every chart has a grid beside it as its table view
  * `Unknown` is painted in muted ink, not a categorical hue. It fails the chroma
    floor on purpose: it is a data-quality state, not a series, and must read as
    absence rather than as another outcome.
"""
from __future__ import annotations

# --------------------------------------------------------------------- surfaces & ink
TOKENS = {
    "light": {
        "surface": "#fcfcfb",
        "plane": "#f9f9f7",
        "ink": "#0b0b0b",
        "ink2": "#52514e",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "border": "rgba(11,11,11,0.10)",
        "good": "#006300",
    },
    "dark": {
        "surface": "#1a1a19",
        "plane": "#0d0d0d",
        "ink": "#ffffff",
        "ink2": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "border": "rgba(255,255,255,0.10)",
        "good": "#0ca30c",
    },
}

# --------------------------------------------------------------------- categorical slots
SLOTS = {
    "light": {"blue": "#2a78d6", "orange": "#eb6834", "aqua": "#1baf7a", "yellow": "#eda100",
              "magenta": "#e87ba4", "green": "#008300", "violet": "#4a3aa7", "red": "#e34948"},
    "dark": {"blue": "#3987e5", "orange": "#d95926", "aqua": "#199e70", "yellow": "#c98500",
             "magenta": "#d55181", "green": "#008300", "violet": "#9085e9", "red": "#e66767"},
}

# Sequential blue ramp, for magnitude (distance heat, volume shading).
SEQ_BLUE = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7",
            "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"]

# --------------------------------------------------------------------- outcome identity
# Stack order == validated adjacency order. Do not reorder without re-running the
# validator: the ordering *is* the CVD-safety mechanism.
KICK_OUTCOMES = ["Touchback", "Fair catch", "Out of bounds", "Downed", "Returned",
                 "Onside", "Blocked", "Unknown"]
FG_OUTCOMES = ["Made", "Missed", "Blocked", "Negated"]

_KICK_HUE = {"Touchback": "blue", "Fair catch": "orange", "Out of bounds": "aqua",
             "Downed": "yellow", "Returned": "magenta", "Onside": "violet",
             "Blocked": "violet"}
_FG_HUE = {"Made": "blue", "Missed": "red", "Blocked": "violet"}

# `Onside` and `Blocked` share violet. They can never appear in one chart: kickoffs
# carry a NULL `kick_blocked` on every row and punts can never be onside.


def outcome_colors(mode: str) -> dict[str, str]:
    """Colour by outcome identity, stable across every filter state."""
    s, t = SLOTS[mode], TOKENS[mode]
    out = {k: s[v] for k, v in _KICK_HUE.items()}
    out.update({k: s[v] for k, v in _FG_HUE.items()})
    out["Unknown"] = t["muted"]
    out["Negated"] = t["muted"]
    return out


def ordered_outcomes(present: list[str], phase_set: set[str]) -> list[str]:
    """Outcomes in stack order, restricted to those actually present."""
    order = FG_OUTCOMES if phase_set == {"field_goal"} else KICK_OUTCOMES
    tail = [o for o in present if o not in order]
    return [o for o in order if o in present] + sorted(tail)


# --------------------------------------------------------------------- plotly
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

# Phase identity, for the charts that compare the three phases against each other.
# Slots 1/2/3 -- the validated all-pairs-safe leading three.
PHASE_HUE = {"Field goal": "blue", "Punt": "orange", "Kickoff": "aqua"}


def plotly_layout(mode: str, height: int = 300, legend: bool = True, **kw) -> dict:
    """Recessive chrome, thin marks, no chartjunk. One y-axis, always."""
    t = TOKENS[mode]
    lay = dict(
        height=height,
        paper_bgcolor=t["surface"],
        plot_bgcolor=t["surface"],
        font=dict(family=FONT, size=12, color=t["ink2"]),
        margin=dict(l=56, r=18, t=42, b=72),
        hoverlabel=dict(bgcolor=t["plane"], bordercolor=t["border"],
                        font=dict(family=FONT, size=12, color=t["ink"])),
        xaxis=dict(showgrid=False, zeroline=False, linecolor=t["axis"],
                   tickcolor=t["axis"], tickfont=dict(color=t["muted"], size=11)),
        yaxis=dict(gridcolor=t["grid"], zeroline=False, showline=False,
                   tickfont=dict(color=t["muted"], size=11)),
        showlegend=legend,
        # Below the plot, not above it: at 1.02 the legend sits in the same band as
        # the title and the two overlap at these chart heights.
        legend=dict(orientation="h", yanchor="top", y=-0.16, x=0,
                    font=dict(size=11, color=t["ink2"]), bgcolor="rgba(0,0,0,0)"),
        title=dict(font=dict(size=13, color=t["ink"]), x=0, xanchor="left", y=0.98),
        bargap=0.28,
        uirevision="keep",
    )
    lay.update(kw)
    return lay


# --------------------------------------------------------------------- ag grid
# dash-ag-grid bundles only the light quartz stylesheet -- `ag-theme-quartz-dark`
# is not loaded, so asking for it silently left cell text at near-black on our dark
# surface. Always use the light theme class and drive every colour from the CSS
# variables in app.css instead, which works in both modes by construction.
GRID_THEME = {"light": "ag-theme-quartz", "dark": "ag-theme-quartz"}

DEFAULT_COL = {
    "sortable": True,
    "resizable": True,
    "filter": True,
    "floatingFilter": False,
    "minWidth": 90,
    "flex": 1,
}

GRID_OPTS = {
    "rowHeight": 34,
    "headerHeight": 36,
    "animateRows": False,
    "suppressCellFocus": False,
    "tooltipShowDelay": 300,
}
