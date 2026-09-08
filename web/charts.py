"""Plotly figures for the explorer and the profile pages.

Every figure obeys the same rules: one y-axis (never two scales), categorical hue
by outcome *identity* so a filter that drops a series never repaints the survivors,
thin marks with a 2px surface gap between stacked fills, recessive grid, and direct
labels on any segment big enough to hold one.
"""
from __future__ import annotations

import plotly.graph_objects as go

from . import data, theme

_MIN_LABEL_SHARE = 0.07  # below this a segment is too thin to hold a legible label


def empty_fig(mode: str, msg: str = "No plays match these filters", height: int = 300):
    t = theme.TOKENS[mode]
    fig = go.Figure()
    fig.update_layout(**theme.plotly_layout(mode, height=height, legend=False))
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    fig.add_annotation(text=msg, showarrow=False, xref="paper", yref="paper",
                       x=0.5, y=0.5, font=dict(color=t["muted"], size=12))
    return fig


_MAX_LABELLED_CATEGORIES = 14  # past this the bars are too narrow to hold a label


def _stacked_share(df, xcol, mode, phases, title, height=300, xtype=None):
    """100% stacked bar of outcome share. Direct-labels every segment >= 7%."""
    if df.empty:
        return empty_fig(mode, height=height)
    t = theme.TOKENS[mode]
    colors = theme.outcome_colors(mode)
    present = df["outcome"].unique().tolist()
    order = theme.ordered_outcomes(present, set(phases))

    totals = df.groupby(xcol, observed=True)["n"].sum()
    xs = sorted(totals.index.tolist())
    label = len(xs) <= _MAX_LABELLED_CATEGORIES
    width_kw = {"width": 4.4} if xtype == "linear" else {}
    # On a category axis plotly reads a *numeric* coordinate as a category index, so
    # an annotation at x=2016 stretched the axis to 0..2016 and squashed ten bars
    # into an invisible sliver. Categories must be strings everywhere.
    cat = xtype == "category"
    xp = [str(x) for x in xs] if cat else xs
    fig = go.Figure()
    for oc in order:
        sub = df[df["outcome"] == oc].set_index(xcol)["n"]
        shares = [(sub.get(x, 0) / totals[x]) if totals[x] else 0 for x in xs]
        counts = [int(sub.get(x, 0)) for x in xs]
        fig.add_bar(
            x=xp, y=shares, name=oc, **width_kw,
            marker=dict(color=colors.get(oc, t["muted"]),
                        line=dict(color=t["surface"], width=1)),
            text=([f"{s:.0%}" if s >= _MIN_LABEL_SHARE else "" for s in shares]
                  if label else None),
            textposition="inside", insidetextanchor="middle",
            textfont=dict(size=10), cliponaxis=False,
            customdata=counts,
            hovertemplate=(f"<b>{oc}</b><br>%{{x}}<br>"
                           "%{customdata:,} plays · %{y:.1%}<extra></extra>"),
        )
    fig.update_layout(**theme.plotly_layout(mode, height=height, barmode="stack",
                                            bargap=0.22, title=dict(text=title)))
    fig.update_yaxes(tickformat=".0%", range=[0, 1], title=None,
                     tickvals=[0, 0.25, 0.5, 0.75, 1.0])
    fig.update_xaxes(title=None, type=xtype or "-")

    # A 100% stack hides its denominator: a band with two kicks reads exactly as
    # confidently as one with two thousand, so n goes under each tick.
    #
    # On the tick and not in an annotation: plotly coerces a numeric-looking
    # annotation x on a category axis into a numeric *coordinate*, so an annotation
    # at "2016" stretched the x range to [-0.5, 2116] and squashed the bars flat.
    if label:
        fig.update_xaxes(
            tickvals=xp,
            ticktext=[f"{x}<br><span style='font-size:9px'>{int(totals[x]):,}</span>"
                      for x in xs],
        )
    return fig


# --------------------------------------------------------------------------- explorer
def outcome_mix_by_season(where: str, phases, mode: str, height: int = 300):
    df = data.q(f"""
        SELECT season, outcome, count(*) AS n
        FROM st WHERE {where} GROUP BY 1, 2
    """)
    return _stacked_share(df, "season", mode, phases,
                          "Outcome mix by season", height, xtype="category")


def outcome_by_distance(where: str, phases, mode: str, height: int = 300):
    """Where each outcome lives along the distance axis, in 5-yard bands."""
    df = data.q(f"""
        SELECT kick_bucket, outcome, count(*) AS n
        FROM st WHERE {where} AND kick_bucket IS NOT NULL GROUP BY 1, 2
    """)
    unit = {"field_goal": "attempt distance", "punt": "gross punt",
            "kickoff": "kick distance"}.get(theme_key(phases), "kick distance")
    return _stacked_share(df, "kick_bucket", mode, phases,
                          f"Outcome mix by {unit} (5-yard bands)", height,
                          xtype="linear")


def theme_key(phases) -> str:
    p = list(phases or [])
    return p[0] if len(p) == 1 else "mixed"


def volume_by_distance(where: str, phases, mode: str, height: int = 300):
    """Single-series distribution -- how many kicks at each distance. No legend needed."""
    df = data.q(f"""
        SELECT kick_bucket AS b, count(*) AS n
        FROM st WHERE {where} AND kick_bucket IS NOT NULL GROUP BY 1 ORDER BY 1
    """)
    if df.empty:
        return empty_fig(mode, height=height)
    t = theme.TOKENS[mode]
    fig = go.Figure(go.Bar(
        x=df["b"], y=df["n"], width=4.4,
        marker=dict(color=theme.SLOTS[mode]["blue"], cornerradius=4),
        hovertemplate="%{x}-%{x} +4 yd<br>%{y:,} kicks<extra></extra>",
    ))
    fig.update_layout(**theme.plotly_layout(mode, height=height, legend=False,
                                            title=dict(text="Attempts by distance (5-yard bands)")))
    fig.update_yaxes(tickformat=",", title=None)
    fig.update_xaxes(title=None, ticksuffix=" yd")
    return fig


def phase_detail(where: str, phases, mode: str, height: int = 300):
    """The measure that actually matters for the selected phase."""
    key = theme_key(phases)
    t = theme.TOKENS[mode]
    s = theme.SLOTS[mode]

    if key == "field_goal":
        df = data.q(f"""
            SELECT kick_bucket AS b, count(*) n,
                   sum(CASE WHEN outcome = 'Made' THEN 1 ELSE 0 END) made
            FROM st WHERE {where} AND kick_bucket IS NOT NULL AND outcome <> 'Negated'
            GROUP BY 1 HAVING count(*) >= 10 ORDER BY 1
        """)
        if df.empty:
            return empty_fig(mode, "Not enough attempts to plot a make rate", height)
        rate = df["made"] / df["n"]
        fig = go.Figure(go.Bar(
            x=df["b"], y=rate, width=4.4,
            marker=dict(color=s["blue"], cornerradius=4),
            text=[f"{r:.0%}<br>{int(n):,}" for r, n in zip(rate, df["n"])],
            textposition="outside",
            textfont=dict(size=10, color=t["ink2"]), cliponaxis=False,
            customdata=df[["made", "n"]].to_numpy(),
            hovertemplate=("%{x}-%{x} +4 yd<br>%{customdata[0]:,} of "
                           "%{customdata[1]:,} · %{y:.1%}<extra></extra>"),
        ))
        fig.update_layout(**theme.plotly_layout(mode, height=height, legend=False,
                          title=dict(text="Make rate by attempt distance (5-yard bands)")))
        fig.update_yaxes(tickformat=".0%", range=[0, 1.18], title=None)
        fig.update_xaxes(title=None, ticksuffix=" yd")
        return fig

    if key == "punt":
        # Gross and net are both yards, so they share one axis legitimately.
        df = data.q(f"""
            SELECT (yards_to_goal / 10)::INT * 10 AS b,
                   avg(punt_gross_yds) gross, avg(punt_net_yds) net, count(*) n
            FROM st WHERE {where} AND yards_to_goal IS NOT NULL
            GROUP BY 1 HAVING count(*) >= 200 ORDER BY 1
        """)
        if df.empty:
            return empty_fig(mode, "Not enough punts to plot gross vs net", height)
        fig = go.Figure()
        for name, col, hexv in (("Gross", "gross", s["blue"]), ("Net", "net", s["orange"])):
            fig.add_scatter(x=df["b"], y=df[col], name=name, mode="lines+markers",
                            line=dict(color=hexv, width=2),
                            marker=dict(size=8, color=hexv,
                                        line=dict(color=t["surface"], width=2)),
                            hovertemplate=f"<b>{name}</b><br>%{{x}} yd to goal<br>"
                                          "%{y:.1f} yd<extra></extra>")
        fig.update_layout(**theme.plotly_layout(mode, height=height,
                          margin=dict(l=56, r=18, t=54, b=72),
                          title=dict(text="Gross and net by yards to goal at the snap"
                                          "<br><sup>10-yard bands with fewer than 200 "
                                          "punts omitted</sup>")))
        fig.update_yaxes(title=None, ticksuffix=" yd")
        fig.update_xaxes(title=None)
        return fig

    if key == "kickoff":
        # Both series are shares of the same denominator -- one axis.
        df = data.q(f"""
            SELECT season,
                   count(*) n,
                   sum(CASE WHEN outcome = 'Touchback' THEN 1 ELSE 0 END) tb,
                   sum(CASE WHEN outcome = 'Returned'  THEN 1 ELSE 0 END) ret,
                   sum(CASE WHEN outcome = 'Unknown'   THEN 1 ELSE 0 END) unk
            FROM st WHERE {where} AND NOT COALESCE(onside, FALSE)
            GROUP BY 1 ORDER BY 1
        """)
        if df.empty:
            return empty_fig(mode, height=height)
        fig = go.Figure()
        series = (("Touchback", df["tb"] / df["n"], s["blue"]),
                  ("Returned", df["ret"] / df["n"], s["magenta"]),
                  ("Unknown", df["unk"] / df["n"], t["muted"]))
        for name, y, hexv in series:
            fig.add_scatter(x=df["season"], y=y, name=name, mode="lines+markers",
                            line=dict(color=hexv, width=2,
                                      dash="dot" if name == "Unknown" else "solid"),
                            marker=dict(size=8, color=hexv,
                                        line=dict(color=t["surface"], width=2)),
                            hovertemplate=f"<b>{name}</b><br>%{{x}}<br>"
                                          "%{y:.1%}<extra></extra>")
        fig.update_layout(**theme.plotly_layout(mode, height=height,
                          title=dict(text="Touchback and return rate by season "
                                          "(onside kicks excluded)")))
        fig.update_yaxes(tickformat=".0%", title=None)
        fig.update_xaxes(title=None, dtick=1)
        return fig

    # mixed: mean kick distance per phase. All three series are yards.
    df = data.q(f"""
        SELECT season, phase, avg(kick_yds) d, count(*) n
        FROM st WHERE {where} AND kick_yds IS NOT NULL GROUP BY 1, 2 ORDER BY 1
    """)
    if df.empty:
        return empty_fig(mode, height=height)
    hue = {"Field goal": s["blue"], "Punt": s["orange"], "Kickoff": s["aqua"]}
    fig = go.Figure()
    for ph in ("Field goal", "Punt", "Kickoff"):
        sub = df[df["phase"] == ph]
        if sub.empty:
            continue
        fig.add_scatter(x=sub["season"], y=sub["d"], name=ph, mode="lines+markers",
                        line=dict(color=hue[ph], width=2),
                        marker=dict(size=8, color=hue[ph],
                                    line=dict(color=t["surface"], width=2)),
                        hovertemplate=f"<b>{ph}</b><br>%{{x}}<br>"
                                      "%{y:.1f} yd<extra></extra>")
    fig.update_layout(**theme.plotly_layout(mode, height=height,
                      title=dict(text="Mean kick distance by season")))
    fig.update_yaxes(title=None, ticksuffix=" yd")
    fig.update_xaxes(title=None, dtick=1)
    return fig


# --------------------------------------------------------------------------- profiles
def season_trend(where: str, mode: str, kind: str, height: int = 260):
    """One measure across seasons for a single player or team, per phase."""
    t, s = theme.TOKENS[mode], theme.SLOTS[mode]
    spec = {
        "field_goal": ("Make rate by season",
                       "sum(CASE WHEN outcome='Made' THEN 1 ELSE 0 END)::DOUBLE / "
                       "nullif(sum(CASE WHEN outcome<>'Negated' THEN 1 ELSE 0 END),0)",
                       ".0%", s["blue"]),
        "punt": ("Average net by season", "avg(punt_net_yds)", ".1f", s["orange"]),
        "kickoff": ("Touchback rate by season",
                    "sum(CASE WHEN outcome='Touchback' THEN 1 ELSE 0 END)::DOUBLE / "
                    "nullif(count(*),0)", ".0%", s["aqua"]),
    }[kind]
    title, expr, fmt, hexv = spec
    df = data.q(f"""
        SELECT season, {expr} AS v, count(*) n
        FROM st WHERE {where} AND play_kind = '{kind}' GROUP BY 1 ORDER BY 1
    """)
    df = df[df["v"].notna()]
    if df.empty:
        return empty_fig(mode, "No qualifying seasons", height)
    fig = go.Figure(go.Scatter(
        x=df["season"], y=df["v"], mode="lines+markers+text",
        line=dict(color=hexv, width=2),
        marker=dict(size=9, color=hexv, line=dict(color=t["surface"], width=2)),
        text=[format(v, fmt) for v in df["v"]], textposition="top center",
        textfont=dict(size=10, color=t["ink2"]),
        customdata=df["n"],
        hovertemplate="%{x}<br>%{y:" + fmt + "} on %{customdata:,}<extra></extra>",
    ))
    fig.update_layout(**theme.plotly_layout(mode, height=height, legend=False,
                                            title=dict(text=title)))
    fig.update_yaxes(tickformat=fmt if fmt.endswith("%") else None, title=None)
    fig.update_xaxes(title=None, dtick=1, type="category")
    return fig


# --------------------------------------------------------------------------- multi-phase
# Field goals and kicks share no outcome vocabulary: `Made` and `Touchback` are not
# alternatives to one another, and pooling them put ten categories on one stacked bar
# -- two slots past the palette -- with Made and Touchback both painted blue. When
# more than one phase is selected the comparison is therefore *by phase*, which is
# the only axis the three actually share.
def _phase_stack(df, xcol, mode, title, height, xtype=None):
    if df.empty:
        return empty_fig(mode, height=height)
    t = theme.TOKENS[mode]
    xs = sorted(df[xcol].unique().tolist())
    width_kw = {"width": 4.4} if xtype == "linear" else {}
    xp = [str(x) for x in xs] if xtype == "category" else xs
    fig = go.Figure()
    for ph in ("Field goal", "Punt", "Kickoff"):
        sub = df[df["phase"] == ph].set_index(xcol)["n"]
        if sub.empty:
            continue
        hexv = theme.SLOTS[mode][theme.PHASE_HUE[ph]]
        fig.add_bar(x=xp, y=[int(sub.get(x, 0)) for x in xs], name=ph, **width_kw,
                    marker=dict(color=hexv, line=dict(color=t["surface"], width=1)),
                    hovertemplate=f"<b>{ph}</b><br>%{{x}}<br>%{{y:,}} kicks<extra></extra>")
    fig.update_layout(**theme.plotly_layout(mode, height=height, barmode="stack",
                                            bargap=0.22, title=dict(text=title)))
    fig.update_yaxes(tickformat=",", title=None)
    fig.update_xaxes(title=None, type=xtype or "-")
    return fig


def kicks_by_phase_season(where: str, mode: str, height: int = 300):
    df = data.q(f"""
        SELECT season, phase, count(*) AS n FROM st WHERE {where} GROUP BY 1, 2
    """)
    return _phase_stack(df, "season", mode, "Kicks by phase and season", height,
                        xtype="category")


def kicks_by_phase_distance(where: str, mode: str, height: int = 300):
    df = data.q(f"""
        SELECT kick_bucket AS b, phase, count(*) AS n FROM st
        WHERE {where} AND kick_bucket IS NOT NULL GROUP BY 1, 2
    """)
    return _phase_stack(df, "b", mode,
                        "Kicks by phase and distance (5-yard bands)", height,
                        xtype="linear")


def unknown_share_by_phase(where: str, mode: str, height: int = 300):
    """The data-quality story, which is the one thing worth three lines on one axis."""
    df = data.q(f"""
        SELECT season, phase, count(*) AS n,
               sum(CASE WHEN outcome = 'Unknown' THEN 1 ELSE 0 END) AS unk
        FROM st WHERE {where} GROUP BY 1, 2 ORDER BY 1
    """)
    if df.empty:
        return empty_fig(mode, height=height)
    t = theme.TOKENS[mode]
    fig = go.Figure()
    for ph in ("Field goal", "Punt", "Kickoff"):
        sub = df[df["phase"] == ph]
        if sub.empty:
            continue
        hexv = theme.SLOTS[mode][theme.PHASE_HUE[ph]]
        fig.add_scatter(x=sub["season"], y=sub["unk"] / sub["n"], name=ph,
                        mode="lines+markers", line=dict(color=hexv, width=2),
                        marker=dict(size=8, color=hexv,
                                    line=dict(color=t["surface"], width=2)),
                        customdata=sub["unk"],
                        hovertemplate=f"<b>{ph}</b><br>%{{x}}<br>%{{y:.1%}} "
                                      "(%{customdata:,} kicks)<extra></extra>")
    fig.update_layout(**theme.plotly_layout(
        mode, height=height,
        title=dict(text="Unreadable-outcome share by season")))
    fig.update_yaxes(tickformat=".0%", rangemode="tozero", title=None)
    fig.update_xaxes(title=None, dtick=1, type="category")
    return fig


def explorer_figs(where: str, phases, mode: str, height: int = 300):
    """The three charts for a selection, whichever phases it spans.

    One phase -> the outcome vocabulary is coherent, so show the outcome mix and the
    measure that matters for that phase. More than one -> compare the phases instead.
    """
    ps = sorted(phases or [])
    if len(ps) == 1:
        return (outcome_mix_by_season(where, ps, mode, height),
                outcome_by_distance(where, ps, mode, height),
                phase_detail(where, ps, mode, height))
    return (kicks_by_phase_season(where, mode, height),
            kicks_by_phase_distance(where, mode, height),
            unknown_share_by_phase(where, mode, height))
