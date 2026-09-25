"""Plotly figures for the explorer and the profile pages.

Every figure obeys the same rules: one y-axis (never two scales), categorical hue
by outcome *identity* so a filter that drops a series never repaints the survivors,
thin marks with a 2px surface gap between stacked fills, recessive grid, and direct
labels on any segment big enough to hold one.
"""
from __future__ import annotations

import plotly.graph_objects as go

from . import data, lens, theme

_MIN_LABEL_SHARE = 0.07  # below this a segment is too thin to hold a legible label


def _abbr(n: int) -> str:
    """Short form for a denominator printed under an axis tick.

    The under-tick denominator is a convention worth keeping (a 100% stack that hides
    its n reads as confidently on two plays as on two thousand), but it only works if
    it fits. At scrimmage volumes the full form is seven characters -- "128,994" --
    which plotly rotates to 45 degrees and then overlaps into the legend. Four
    characters stays horizontal and legible.
    """
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n // 1000:,}k"
    if n >= 1_000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _seasons(xs) -> bool:
    """True when the axis is a run of seasons wide enough to need short ticks."""
    if len(xs) <= 8:
        return False
    try:
        return all(1900 <= int(x) <= 2100 for x in xs)
    except (TypeError, ValueError):
        return False


def _tick(x, short: bool) -> str:
    """Two-digit years once the axis gets crowded.

    Thirteen four-digit years do not fit a 290px panel, and plotly's answer is to
    rotate them 45 degrees -- which on a two-line tick puts the year and its
    denominator on top of each other and then on top of the legend. Two digits fit
    horizontally, and the sidebar's season slider already reads that way.
    """
    return str(x)[2:] if short else str(x)


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

# Draw order for the by-phase charts, which is theme.PHASE_HUE's order and therefore
# the validated one. A chart that claims to show every selected phase has to iterate
# the whole list, not the three the app used to have.
_KICK_PHASES = ("Field goal", "Punt", "Kickoff", "Extra point", "Two-point",
                "Defensive conv")
_SCRIM_PHASES = ("Rush", "Pass", "Sack", "Penalty", "Other")


def _stacked_share(df, xcol, mode, phases, title, height=300, xtype=None,
                   key="st", noun="plays"):
    """100% stacked bar of outcome share. Direct-labels every segment >= 7%."""
    if df.empty:
        return empty_fig(mode, height=height)
    t = theme.TOKENS[mode]
    colors = theme.outcome_colors(mode, key)
    present = df["outcome"].unique().tolist()
    order = theme.ordered_outcomes(present, key, set(phases))

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
                           f"%{{customdata:,}} {noun} · %{{y:.1%}}<extra></extra>"),
        )
    fig.update_layout(**theme.plotly_layout(mode, height=height, barmode="stack",
                                            bargap=0.22, title=dict(text=title)))
    fig.update_yaxes(tickformat=".0%", range=[0, 1], title=None,
                     tickvals=[0, 0.25, 0.5, 0.75, 1.0])
    fig.update_xaxes(title=None, type=xtype or "-")

    # A 100% stack hides its denominator: a band with two kicks reads exactly as
    # confidently as one with two thousand, so n is printed for every bar.
    #
    # Above the bar, not under the tick. Under the tick it has to share a two-line
    # label with the category, and plotly rotates a crowded axis as a block -- at
    # thirteen seasons in a 290px panel the year and its n ended up printed on top of
    # each other and then on top of the legend.
    #
    # Positioned in *domain* fractions rather than category coordinates: plotly
    # coerces a numeric-looking annotation x on a category axis into a numeric
    # coordinate, which once stretched the x range to [-0.5, 2116] and squashed the
    # bars flat. Category i sits at (i + 0.5)/n of the domain by construction, so
    # this cannot be misread as a year.
    if label:
        fig.update_xaxes(tickvals=xp,
                         ticktext=[_tick(x, _seasons(xs)) for x in xs])
        n_cat = len(xs)
        for i, x in enumerate(xs):
            fig.add_annotation(
                x=(i + 0.5) / n_cat, xref="x domain", xanchor="center",
                y=1.0, yref="y", yanchor="bottom", yshift=3,
                text=_abbr(totals[x]), showarrow=False,
                font=dict(size=9, color=t["muted"]),
            )
    return fig


# --------------------------------------------------------------------------- explorer
def outcome_mix_by_season(where: str, phases, mode: str, height: int = 300):
    df = data.q(f"""
        SELECT season, outcome, count(*) AS n
        FROM st_play WHERE {where} GROUP BY 1, 2
    """)
    return _stacked_share(df, "season", mode, phases,
                          "Outcome mix by season", height, xtype="category",
                          key="st", noun="kicks")


def outcome_by_distance(where: str, phases, mode: str, height: int = 300):
    """Where each outcome lives along the distance axis, in 5-yard bands."""
    df = data.q(f"""
        SELECT kick_bucket, outcome, count(*) AS n
        FROM st_play WHERE {where} AND kick_bucket IS NOT NULL GROUP BY 1, 2
    """)
    unit = {"field_goal": "attempt distance", "punt": "gross punt",
            "kickoff": "kick distance"}.get(theme_key(phases), "kick distance")
    return _stacked_share(df, "kick_bucket", mode, phases,
                          f"Outcome mix by {unit} (5-yard bands)", height,
                          xtype="linear", key="st", noun="kicks")


def conversion_by_type(where: str, mode: str, height: int = 300):
    """Outcome share by how the conversion was attempted.

    The distance chart's replacement on the conversion chip. A conversion has no
    distance -- `kick_yds` is NULL on all 71,460 rows -- so `outcome_by_distance`
    drew an empty panel here until 2026-09-09. Attempt type is the axis conversions
    actually vary along, and it separates the three things the chip pools: a kick
    that converts 97.4% of the time, a two-point try that converts 42.6%, and a
    defensive return that is not an attempt at all.
    """
    df = data.q(f"""
        SELECT CASE WHEN play_kind = 'pat'                  THEN 'Kick (XP)'
                    WHEN play_kind = 'defensive_conversion' THEN 'Return (def 2pt)'
                    WHEN two_point_type = 'pass'            THEN 'Pass (2pt)'
                    WHEN two_point_type = 'rush'            THEN 'Rush (2pt)'
                    ELSE 'Unstated (2pt)' END AS attempt_type,
               outcome, count(*) AS n
        FROM st_play WHERE {where} GROUP BY 1, 2
    """)
    return _stacked_share(df, "attempt_type", mode, ["conversion"],
                          "Outcome by attempt type", height, xtype="category",
                          key="st", noun="attempts")


def theme_key(phases) -> str:
    p = list(phases or [])
    return p[0] if len(p) == 1 else "mixed"


def volume_by_distance(where: str, phases, mode: str, height: int = 300):
    """Single-series distribution -- how many kicks at each distance. No legend needed."""
    df = data.q(f"""
        SELECT kick_bucket AS b, count(*) AS n
        FROM st_play WHERE {where} AND kick_bucket IS NOT NULL GROUP BY 1 ORDER BY 1
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
            FROM st_play WHERE {where} AND kick_bucket IS NOT NULL AND outcome <> 'Negated'
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
            FROM st_play WHERE {where} AND yards_to_goal IS NOT NULL
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

    if key == "conversion":
        # Both series are shares, so one axis is legitimate -- but of DIFFERENT
        # denominators, which the hover states. They are drawn together because the
        # interesting thing about conversions over this window is the gap between
        # them and that the two-point line is the one that moves.
        df = data.q(f"""
            SELECT season,
                   count(*) FILTER (play_kind = 'pat')                    xp,
                   count(*) FILTER (play_kind = 'pat'
                                    AND outcome = 'Converted')            xp_good,
                   count(*) FILTER (play_kind = 'two_point')              two,
                   count(*) FILTER (play_kind = 'two_point'
                                    AND outcome = 'Converted')            two_good
            FROM st_play WHERE {where} GROUP BY 1 ORDER BY 1
        """)
        if df.empty:
            return empty_fig(mode, height=height)
        fig = go.Figure()
        series = (("Extra point", df["xp_good"] / df["xp"].replace(0, float("nan")),
                   df["xp"], s["yellow"]),
                  ("Two-point", df["two_good"] / df["two"].replace(0, float("nan")),
                   df["two"], s["magenta"]))
        for name, y, denom, hexv in series:
            fig.add_scatter(x=df["season"], y=y, name=name, mode="lines+markers",
                            line=dict(color=hexv, width=2),
                            marker=dict(size=8, color=hexv,
                                        line=dict(color=t["surface"], width=2)),
                            customdata=denom,
                            hovertemplate=f"<b>{name}</b><br>%{{x}}<br>"
                                          "%{y:.1%} of %{customdata:,}<extra></extra>")
        fig.update_layout(**theme.plotly_layout(mode, height=height,
                          title=dict(text="Conversion rate by season "
                                          "(each on its own attempts)")))
        fig.update_yaxes(tickformat=".0%", range=[0, 1.05], title=None)
        fig.update_xaxes(title=None, dtick=1)
        return fig

    if key == "kickoff":
        # Both series are shares of the same denominator -- one axis.
        df = data.q(f"""
            SELECT season,
                   count(*) n,
                   sum(CASE WHEN outcome = 'Touchback' THEN 1 ELSE 0 END) tb,
                   sum(CASE WHEN outcome = 'Returned'  THEN 1 ELSE 0 END) ret,
                   sum(CASE WHEN outcome = 'Unknown'   THEN 1 ELSE 0 END) unk
            FROM st_play WHERE {where} AND NOT COALESCE(onside, FALSE)
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
        FROM st_play WHERE {where} AND kick_yds IS NOT NULL GROUP BY 1, 2 ORDER BY 1
    """)
    if df.empty:
        return empty_fig(mode, height=height)
    hue = {ph: s[theme.PHASE_HUE[ph]] for ph in _KICK_PHASES}
    fig = go.Figure()
    for ph in _KICK_PHASES:
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
    # Each spec carries its own play_kind predicate rather than taking `kind` as one:
    # `conversion` is a chip over three kinds, and the measure it draws is narrower
    # still. The extra point is the conversion with a stable denominator -- a kicker
    # takes one after every touchdown -- while a two-point try is a coach's decision
    # on a twentieth of the volume, so a pooled line would move with how often the
    # team went for two rather than with how well it kicked. The two-point rate is in
    # the grid directly below, and both are drawn together by phase_detail.
    spec = {
        "field_goal": ("Make rate by season",
                       "sum(CASE WHEN outcome='Made' THEN 1 ELSE 0 END)::DOUBLE / "
                       "nullif(sum(CASE WHEN outcome<>'Negated' THEN 1 ELSE 0 END),0)",
                       ".0%", s["blue"], "play_kind = 'field_goal'"),
        "punt": ("Average net by season", "avg(punt_net_yds)", ".1f", s["orange"],
                 "play_kind = 'punt'"),
        "kickoff": ("Touchback rate by season",
                    "sum(CASE WHEN outcome='Touchback' THEN 1 ELSE 0 END)::DOUBLE / "
                    "nullif(count(*),0)", ".0%", s["aqua"], "play_kind = 'kickoff'"),
        "conversion": ("Extra point rate by season",
                       "sum(CASE WHEN outcome='Converted' THEN 1 ELSE 0 END)::DOUBLE / "
                       "nullif(count(*),0)", ".0%", s["yellow"], "play_kind = 'pat'"),
    }[kind]
    title, expr, fmt, hexv, kind_where = spec
    df = data.q(f"""
        SELECT season, {expr} AS v, count(*) n
        FROM st_play WHERE {where} AND {kind_where} GROUP BY 1 ORDER BY 1
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
def _phase_stack(df, xcol, mode, title, height, xtype=None, phases=None,
                 noun="kicks"):
    if df.empty:
        return empty_fig(mode, height=height)
    t = theme.TOKENS[mode]
    xs = sorted(df[xcol].unique().tolist())
    width_kw = {"width": 4.4} if xtype == "linear" else {}
    xp = [str(x) for x in xs] if xtype == "category" else xs
    fig = go.Figure()
    for ph in (phases or _KICK_PHASES):
        sub = df[df["phase"] == ph].set_index(xcol)["n"]
        if sub.empty:
            continue
        hexv = theme.SLOTS[mode][theme.PHASE_HUE[ph]]
        fig.add_bar(x=xp, y=[int(sub.get(x, 0)) for x in xs], name=ph, **width_kw,
                    marker=dict(color=hexv, line=dict(color=t["surface"], width=1)),
                    hovertemplate=(f"<b>{ph}</b><br>%{{x}}<br>"
                                   f"%{{y:,}} {noun}<extra></extra>"))
    fig.update_layout(**theme.plotly_layout(mode, height=height, barmode="stack",
                                            bargap=0.22, title=dict(text=title)))
    fig.update_yaxes(tickformat=",", title=None)
    fig.update_xaxes(title=None, type=xtype or "-")
    if _seasons(xs):
        fig.update_xaxes(tickvals=xp, ticktext=[_tick(x, True) for x in xs])
    return fig


def kicks_by_phase_season(where: str, mode: str, height: int = 300):
    df = data.q(f"""
        SELECT season, phase, count(*) AS n FROM st_play WHERE {where} GROUP BY 1, 2
    """)
    return _phase_stack(df, "season", mode, "Kicks by phase and season", height,
                        xtype="category")


def kicks_by_phase_distance(where: str, mode: str, height: int = 300):
    df = data.q(f"""
        SELECT kick_bucket AS b, phase, count(*) AS n FROM st_play
        WHERE {where} AND kick_bucket IS NOT NULL GROUP BY 1, 2
    """)
    return _phase_stack(df, "b", mode,
                        "Kicks by phase and distance (5-yard bands)", height,
                        xtype="linear")


# --------------------------------------------------------------------------- scrimmage
# The views expose ten outcome values on offense and nine on defense -- more than a
# stacked bar can carry legibly, and more than the palette has validated slots for.
# Charts read the seven-way grouping in theme.SCRIM_GROUP; the grid keeps the full
# vocabulary. Grouping happens here rather than in SQL so it lives in one place
# beside the colours it has to agree with.
def _grouped(view: str, where: str, key: str, xexpr: str, xname: str,
             extra: str = "TRUE"):
    df = data.q(f"""
        SELECT {xexpr} AS {xname}, outcome, count(*) AS n
        FROM {view} WHERE {where} AND {extra} GROUP BY 1, 2
    """)
    if df.empty:
        return df
    df["outcome"] = [theme.group_of(key, o) for o in df["outcome"]]
    return df.groupby([xname, "outcome"], as_index=False, observed=True)["n"].sum()


def scrim_outcome_by_season(where: str, key: str, chips, mode: str, height: int = 300):
    df = _grouped(lens.VIEW[key], where, key, "season", "season")
    return _stacked_share(df, "season", mode, chips, "Outcome mix by season", height,
                          xtype="category", key=key)


def scrim_outcome_by_down(where: str, key: str, chips, mode: str, height: int = 300):
    df = _grouped(lens.VIEW[key], where, key, "down", "down",
                  extra="down BETWEEN 1 AND 4")
    return _stacked_share(df, "down", mode, chips, "Outcome mix by down", height,
                          xtype="category", key=key)


def scrim_phase_by_season(where: str, key: str, mode: str, height: int = 300):
    df = data.q(f"""
        SELECT season, phase, count(*) AS n
        FROM {lens.VIEW[key]} WHERE {where} GROUP BY 1, 2
    """)
    return _phase_stack(df, "season", mode, "Plays by kind and season", height,
                        xtype="category", phases=_SCRIM_PHASES, noun="plays")


def scrim_yards_by_down(where: str, key: str, mode: str, height: int = 300):
    """Mean yards by down, one series per play kind. One y-axis, grouped bars.

    Turnover rows are excluded and the title says so. ESPN puts the *defense's
    return* in statYardage on an interception -- 35 yards on the offense's row of a
    35-yard pick-six -- so leaving them in lifts the passing mean from 7.5 to 13.2
    and tells the reader nothing true from either side.
    """
    df = data.q(f"""
        SELECT down, phase, avg(yards_gained) AS y, count(*) AS n
        FROM {lens.VIEW[key]}
        WHERE {where} AND down BETWEEN 1 AND 4 AND NOT COALESCE(is_turnover, FALSE)
              AND yards_gained IS NOT NULL
        GROUP BY 1, 2
    """)
    if df.empty:
        return empty_fig(mode, height=height)
    t = theme.TOKENS[mode]
    verb = "gained" if key == "off" else "allowed"
    xs = [1, 2, 3, 4]
    fig = go.Figure()
    for ph in _SCRIM_PHASES:
        sub = df[df["phase"] == ph].set_index("down")
        if sub.empty:
            continue
        hexv = theme.SLOTS[mode][theme.PHASE_HUE[ph]]
        fig.add_bar(
            x=[str(x) for x in xs],
            y=[float(sub["y"].get(x, float("nan"))) for x in xs], name=ph,
            marker=dict(color=hexv, cornerradius=4,
                        line=dict(color=t["surface"], width=1)),
            customdata=[int(sub["n"].get(x, 0)) for x in xs],
            hovertemplate=(f"<b>{ph}</b><br>down %{{x}}<br>%{{y:.2f}} yd {verb}"
                           "<br>%{customdata:,} plays<extra></extra>"),
        )
    # The exclusion is load-bearing, so it is in the title rather than only in the
    # hover -- but on its own line, because the one-line form clipped at panel width.
    fig.update_layout(**theme.plotly_layout(
        mode, height=height, barmode="group", bargap=0.28,
        title=dict(text=f"Mean yards {verb} by down<br>"
                        "<span style='font-size:10px'>turnover rows "
                        "excluded</span>")))
    fig.update_yaxes(title=None, zeroline=True, zerolinecolor=t["axis"])
    fig.update_xaxes(title=None, type="category")
    return fig


# --------------------------------------------------------------------------- dispatch
def explorer_figs(key: str, where: str, chips, mode: str, height: int = 300):
    """The charts for a selection, whichever lens and phases it spans.

    Always a 3-tuple, but the third may be None -- the multi-phase kicks view has two.
    Callers must hide the empty slot AND narrow their grid; see web/pages/explorer.py.

    One phase -> the outcome vocabulary is coherent, so show the outcome mix and the
    measure that matters for that phase. More than one -> compare the phases instead,
    which on the kicks is the only axis all of them share.
    """
    key = lens.resolve(key)
    ps = sorted(chips or [])
    if lens.is_scrimmage(key):
        if len(ps) == 1:
            return (scrim_outcome_by_season(where, key, ps, mode, height),
                    scrim_outcome_by_down(where, key, ps, mode, height),
                    scrim_yards_by_down(where, key, mode, height))
        return (scrim_phase_by_season(where, key, mode, height),
                scrim_outcome_by_season(where, key, ps, mode, height),
                scrim_yards_by_down(where, key, mode, height))
    if len(ps) == 1:
        # The middle slot is the distance chart on every chip but one. Conversions
        # have no distance, so they get attempt type instead of an empty panel.
        middle = (conversion_by_type(where, mode, height) if ps[0] == "conversion"
                  else outcome_by_distance(where, ps, mode, height))
        return (outcome_mix_by_season(where, ps, mode, height), middle,
                phase_detail(where, ps, mode, height))
    # Two charts, not three. The third used to be the unreadable-outcome share by
    # season -- removed 2026-09-09 at the user's request. `None` rather than an empty
    # figure so the caller can collapse the slot and let these two have the width;
    # an empty figure would hold a third of the row to say nothing. The 8.6% of kicks
    # that state no outcome are still an `Unknown` value in the Outcome column and in
    # the mix chart, and the note under the tiles still explains them.
    return (kicks_by_phase_season(where, mode, height),
            kicks_by_phase_distance(where, mode, height),
            None)
