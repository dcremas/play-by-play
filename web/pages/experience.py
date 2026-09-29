"""The experience curve: does a kicker get better the longer he does the job?

A separate route rather than a fourth grain on the explorer, because it does not read the
explorer's selection and could not honestly be made to. Every other view in this app
answers a question about the rows the sidebar has selected; this one answers a question
about CAREERS, and a career is not a set of rows you can filter without changing what
year one means. The season slider is the clearest case -- narrow it to 2018-2022 and a
man who started in 2016 becomes a rookie -- but a team filter does the same thing to
every transfer, and a conference filter to every player who moved up. So the panel is
always the whole corpus for the league on screen, the page says so at the top, and the
controls that DO belong to the question live here instead.

**Counts and rates over counts, and nothing else.** No fitted baseline, no significance
test, no index, no smoothing. Every figure on this page is a total over a total, every
chart point carries the number of players behind it, and every rate has its own numerator
and denominator in a column of the table below it. The page's job is to put the true
numbers in front of a reader in the order that lets them see what is going on; the
conclusion is theirs.

That order is deliberate, because the obvious reading of the question is wrong:

  1. everyone at each year -- the usual answer, with its changing group size on the axis
  2. the same players, one year to the next -- two measured levels and a count of who
     went which way
  3. the same season split by who came back -- which is where the difference in (1) and
     (2) comes from
  4. two tables and the panel, so every number above can be checked or added up by hand

See web/career.py for the three ways the naive arithmetic goes wrong.
"""
from __future__ import annotations

import dash_mantine_components as dmc
import numpy as np
from dash import Input, Output, callback, dcc, html

from .. import career, charts, league, ui

# Grid column widths are in the same idiom as pages/common.py, which is where the
# shared `num`/`txt` builders live -- these grids are just two more AG Grids.
from .common import num, txt, COM, ONE, PCT


def controls():
    """Role, measure, and the two judgement calls the reader is entitled to overrule.

    The volume floor and the censoring switch are on the page rather than buried in
    web/career.py's defaults because both of them MOVE THE ANSWER, and a reader who
    cannot see a choice that moves the answer is reading a claim, not a measurement.
    Drop the floor to 1 and the placekicker curve fills with two-attempt seasons whose
    make rate is 0% or 100%; turn censoring off and a third of the year-one cohort is
    veterans whose real first season is outside the window.
    """
    return dmc.Paper(withBorder=True, radius="sm", p="sm", children=dmc.Group(
        align="flex-end", gap="lg", wrap="wrap", children=[
            dmc.Stack(gap=3, children=[
                dmc.Text("Role", className="st-cap"),
                dmc.SegmentedControl(
                    id="xp-role", value=career.DEFAULT_ROLE, size="xs",
                    data=[{"value": k, "label": v["label"]}
                          for k, v in career.ROLES.items()]),
            ]),
            dmc.Select(id="xp-metric", label="Measure", size="xs", w=230,
                       allowDeselect=False,
                       data=career.metrics_for(career.DEFAULT_ROLE),
                       value=career.ROLES[career.DEFAULT_ROLE]["metrics"][0]),
            dmc.NumberInput(id="xp-min", label="Minimum per season", size="xs", w=160,
                            value=career.ROLES[career.DEFAULT_ROLE]["floor"],
                            min=1, max=200, step=1),
            dmc.Stack(gap=3, children=[
                dmc.Text("Censoring", className="st-cap"),
                dmc.Switch(id="xp-censor", size="sm", checked=True,
                           label="Drop careers already running in the first season"),
            ]),
        ]))


def layout(mode: str = "dark"):
    return dmc.Stack(gap="md", children=[
        ui.section("Experience curve",
                   "whether a kicker improves with years in the job, and how much of "
                   "the apparent improvement is the ones who washed out"),
        ui.note("This view ignores the sidebar and the season range. Experience years "
                "are counted over a player's whole career in the corpus, so a filter "
                "that cut seasons out of the middle of it would renumber everything "
                "after the gap and a team filter would turn a transfer into a second "
                "rookie year. The controls that belong to this question are below.",
                "info"),
        controls(),
        html.Div(id="xp-kpis"),
        html.Div(id="xp-readout"),
        dmc.SimpleGrid(cols={"base": 1, "lg": 2}, spacing="sm", children=[
            ui.graph("xp-cohort", 360),
            ui.graph("xp-paired", 360),
        ]),
        dmc.SimpleGrid(cols={"base": 1, "lg": 2}, spacing="sm", children=[
            ui.graph("xp-surv", 360),
            html.Div(id="xp-defs"),
        ]),
        ui.section("Every year, everybody",
                   "one row per season in the role, with the tallies the rates are "
                   "worked out from"),
        html.Div(id="xp-cohort-grid"),
        ui.section("The same players, one year to the next",
                   "the men who appear in both seasons, and which way each of them went"),
        html.Div(id="xp-steps-grid"),
        ui.section("Every player, year by year",
                   "one row per career, so a single man can be read left to right — "
                   "hover any year for the kicks behind it"),
        html.Div(id="xp-panel-grid"),
    ])


def crumb():
    return dmc.Group(gap=6, children=[
        dmc.Text("/", size="xs", c="dimmed"),
        dmc.Text("Experience curve", size="xs", fw=600),
    ])


# --------------------------------------------------------------------------- controls
@callback(
    Output("xp-metric", "data"), Output("xp-metric", "value"),
    Output("xp-min", "value"),
    Input("xp-role", "value"),
    prevent_initial_call=True,
)
def _role_changed(role):
    """Switching role re-points the measure list and the floor together.

    Both, not just the list. `net` is not a measure a kickoff specialist has, and a
    Select left holding a dead value renders blank and the callbacks below fall through
    to a metric nobody chose. The floor moves for the same reason in reverse: five is a
    season of field goals and a rounding error of punts, so carrying 5 across to the
    punters would admit 1,100 player-seasons of a man who punted twice.
    """
    role = career.role_of(role)
    spec = career.ROLES[role]
    return career.metrics_for(role), spec["metrics"][0], spec["floor"]


def _state(role, metric, min_vol, censor, lg):
    """The panel and every derived frame, for one set of control values."""
    role = career.role_of(role)
    metric = career.metric_of(role, metric)
    lg = league.resolve(lg)
    p = career.panel(lg, role, min_vol, bool(censor))
    steps = career.paired_steps(p, metric)
    return {
        "role": role, "metric": metric, "lg": lg, "panel": p, "steps": steps,
        "cohort": career.cohort_curve(p, metric),
        "table": career.cohort_table(p, role),
        "surv": career.survivorship(p, metric),
    }


_INPUTS = (Input("xp-role", "value"), Input("xp-metric", "value"),
           Input("xp-min", "value"), Input("xp-censor", "checked"),
           Input("league", "data"), Input("mode", "data"))


# --------------------------------------------------------------------------- tiles
@callback(Output("xp-kpis", "children"), *_INPUTS)
def _kpis(role, metric, min_vol, censor, lg, mode):
    st = _state(role, metric, min_vol, censor, lg)
    p, steps, m = st["panel"], st["steps"], st["metric"]
    if p.empty:
        # An explicit empty state, NOT PreventUpdate. Preventing the update leaves the
        # previous role's tiles standing -- "626 players" sitting above a grid that says
        # nothing clears this floor -- and a stale number next to a correct one is read
        # as the correct one.
        return ui.note("No player-season clears this floor. Lower the minimum, or the "
                       "role has nobody in this corpus.", "warn")
    spec = career.ROLES[st["role"]]
    floor = spec["floor"] if min_vol is None else int(min_vol)
    value, gloss = career.headline(steps, m)
    surv = st["surv"]
    tiles = [
        ui.tile("Players", f"{p['aid'].nunique():,}",
                f"{len(p):,} seasons of {floor}+ {spec['noun']}"),
        ui.tile("Longest career", f"{int(p['exp'].max())} seasons", "in this role"),
        ui.tile("Year 1 → year 2, same players", value, gloss),
    ]
    if not surv.empty:
        r = surv.iloc[0]
        tiles.append(ui.tile(
            "Year 1, by what happened next",
            f"{career.fmt_value(m, r['stayed'])} vs {career.fmt_value(m, r['gone'])}",
            f"{int(r['stayed_n']):,} came back, {int(r['gone_n']):,} did not — "
            "both measured in year 1"))
    return dmc.SimpleGrid(cols={"base": 2, "sm": 2, "lg": len(tiles)},
                          spacing="xs", children=tiles)


# --------------------------------------------------------------------------- readout
@callback(Output("xp-readout", "children"), *_INPUTS)
def _readout(role, metric, min_vol, censor, lg, mode):
    """The three numbers, in a sentence, with no conclusion attached.

    Written from the data rather than stored, because the numbers change sign between
    roles on the same page -- college placekickers and college kickoff specialists do
    opposite things -- and a fixed caption serving both would be wrong half the time.
    What it must NOT do is tell the reader what the numbers mean. It says what was
    measured and stops; the tables below carry the tallies to check it against.
    """
    st = _state(role, metric, min_vol, censor, lg)
    steps, cohort, surv = st["steps"], st["cohort"], st["surv"]
    if steps.empty or cohort.empty:
        # Same reason as the tiles: last role's sentence left standing is worse than no
        # sentence.
        return ui.note("Not enough of a career here to compare a player with himself.",
                       "warn")
    m, r = st["metric"], steps.iloc[0]
    label = career.METRICS[m]["label"].lower()
    c0 = cohort.iloc[0]
    c1 = cohort.iloc[1] if len(cohort) > 1 else c0
    parts = [
        f"All {int(c0['players']):,} players in their first season: "
        f"{career.fmt_value(m, c0['value'])} {label}. "
        f"All {int(c1['players']):,} in their second: "
        f"{career.fmt_value(m, c1['value'])}.",
        f"They are not the same men. The {int(r['n']):,} who appear in BOTH years went "
        f"{career.fmt_value(m, r['before'])} → {career.fmt_value(m, r['after'])} — "
        f"{int(r['better'])} of them better, {int(r['worse'])} worse, "
        f"{int(r['level'])} level.",
    ]
    if not surv.empty:
        s0 = surv.iloc[0]
        parts.append(
            f"In that first season the {int(s0['stayed_n']):,} who went on to get "
            f"another one were already at {career.fmt_value(m, s0['stayed'])}, against "
            f"{career.fmt_value(m, s0['gone'])} for the {int(s0['gone_n']):,} who did "
            "not — before either group had gained a year.")
    return ui.note(" ".join(parts), "neutral")


# --------------------------------------------------------------------------- figures
@callback(
    Output("xp-cohort", "figure"), Output("xp-paired", "figure"),
    Output("xp-surv", "figure"),
    *_INPUTS,
)
def _figs(role, metric, min_vol, censor, lg, mode):
    st = _state(role, metric, min_vol, censor, lg)
    m, mode = st["metric"], mode or "dark"
    return (charts.experience_cohort(st["cohort"], m, mode, 360),
            charts.experience_paired(st["steps"], m, mode, 360),
            charts.experience_survivorship(st["surv"], m, mode, 360))


@callback(Output("xp-defs", "children"), *_INPUTS)
def _defs(role, metric, min_vol, censor, lg, mode):
    """What the measure is and how to read the three charts.

    This slot used to hold a fitted make-probability curve, which was the one modelled
    thing on the page. It is prose now, and the job the curve was doing -- showing that
    field goals get longer as a kicker gets older -- is done by putting mean attempt
    distance in a column of the table below, where a reader can see it rise beside a
    make rate that does not and decide for themselves what that is worth.
    """
    role = career.role_of(role)
    metric = career.metric_of(role, metric)
    spec = career.METRICS[metric]
    line = lambda h, b: [dmc.Text(h, fw=600, size="xs"),
                         dmc.Text(b, size="xs", c="dimmed"), dmc.Divider(my=4)]
    return dmc.Paper(withBorder=True, radius="sm", p="md", h=360, children=dmc.Stack(
        gap="xs", children=[
            dmc.Text(spec["label"], fw=600, size="sm"),
            dmc.Text(spec["blurb"], size="xs", c="dimmed"),
            dmc.Divider(my=4),
            *line("Everyone at each year",
                  "Every player who reached that season in the role, added together. "
                  "The count under each year is how many people that is — it falls "
                  "fast, and the men who drop out are not a random sample."),
            *line("The same players, one year to the next",
                  "Only the men who appear in both seasons of a pair. Nobody is added "
                  "or removed between the two dots, so the two are directly comparable."),
            *line("Split by whether he came back",
                  "Both lines are the SAME season. Neither group has gained experience "
                  "yet, so anything separating them is who keeps the job."),
            dmc.Text("Every rate here is a total divided by a total — the tallies are "
                     "in the tables below, and nothing is adjusted, fitted or weighted.",
                     size="xs", c="dimmed"),
        ]))


# --------------------------------------------------------------------------- grids
@callback(Output("xp-cohort-grid", "children"), *_INPUTS)
def _cohort_grid(role, metric, min_vol, censor, lg, mode):
    st = _state(role, metric, min_vol, censor, lg)
    tbl, role = st["table"], st["role"]
    if tbl.empty:
        return ui.note("No player-season clears this floor.", "warn")
    spec = career.ROLES[role]
    cols = [num("exp", "Season in role", 128, COM),
            num("players", "Players", 100, COM)]
    cols += [num(c, h, 110, COM) for c, h in spec["counts"]]
    # EVERY measure the role has, not just the selected one. The whole reason this table
    # exists is that a make rate and a mean attempt distance have to be read together:
    # the rate holding flat while the kicks get a yard longer each year is the finding,
    # and it is only a finding if both columns are on screen at once.
    cols += [num(m, career.TABLE_HEADER.get(m, career.METRICS[m]["label"]), 150,
                 PCT if career.METRICS[m]["fmt"] == "pct" else ONE)
             for m in spec["metrics"]]
    return ui.grid("xp-cohort-tbl", mode or "dark", rows=tbl.to_dict("records"),
                   columns=cols, height="260px", row_id="exp")


def _step_cols(metric: str):
    unit = "points" if career.METRICS[metric]["unit"] == "pp" else "yd"
    fmt = PCT if career.METRICS[metric]["fmt"] == "pct" else ONE
    return [
        txt("step", "Step", 90),
        num("n", "Players in both", 130, COM),
        num("before", "Earlier year", 120, fmt),
        num("after", "Later year", 115, fmt),
        num("change_shown", f"Change ({unit})", 130, ONE),
        num("better", "Got better", 110, COM),
        num("worse", "Got worse", 110, COM),
        num("level", "Level", 88, COM),
    ]


@callback(Output("xp-steps-grid", "children"), *_INPUTS)
def _steps_grid(role, metric, min_vol, censor, lg, mode):
    st = _state(role, metric, min_vol, censor, lg)
    steps, m = st["steps"], st["metric"]
    if steps.empty:
        return ui.note("No player appears in two consecutive qualifying seasons under "
                       "these settings.", "warn")
    out = steps.copy()
    # Shown in the metric's OWN unit rather than as a fraction: a make rate that moves
    # from .755 to .762 moved by 0.7 percentage points, and a column headed "Change"
    # holding 0.0065 invites every reader to call it two-thirds of a percent.
    out["change_shown"] = out["change"] * (100.0 if career.METRICS[m]["unit"] == "pp"
                                           else 1.0)
    thin = int(out["thin"].sum())
    note = (ui.note(f"{thin} pair(s) hold fewer than {career.MIN_SHOWN} players and are "
                    "left off the chart. They are in this table, with their player "
                    "count beside them, because a small group is worth seeing as long "
                    "as you can see that it is small.", "warn") if thin else None)
    return dmc.Stack(gap="xs", children=[
        note,
        ui.grid("xp-steps-tbl", mode or "dark", rows=out.to_dict("records"),
                columns=_step_cols(m), height="240px", row_id="step"),
    ])


def _player_cols(role: str, metric: str, bands: list[dict]):
    """One row per player, the measure spread left to right across his career.

    The career columns carry the value and nothing else, because a dozen columns of
    value plus a dozen of volume is not a table anybody reads. What a reader needs the
    moment a cell looks extreme -- how many kicks it rests on, and which seasons -- is in
    the cell's tooltip instead, off hidden fields the pivot carries. A 100% column and a
    96.6% column have to be distinguishable on hover.
    """
    spec = career.ROLES[role]
    fmt = PCT if career.METRICS[metric]["fmt"] == "pct" else ONE
    # "Career field goals", not "Field goal attempts, career": the long form does not fit
    # the width this column gets and AG Grid truncates from the right, so the header lost
    # the word "career" and read as if it were a single season's count.
    teams = txt("teams", "Team(s)", 190)
    teams["tooltipField"] = "teams"          # transfers run past the column; hover has it
    cols = [
        txt("player", "Player", 170, "left"),
        num("seasons", "Seasons", 92, COM),
        num("first_season", "From", 82), num("last_season", "To", 76),
        num("total_vol", f"Career {spec['noun']}", 146, COM),
        teams,
    ]
    for b in bands:
        k = b["key"]
        # A one-season band says "2019", a multi-season one says "2019-2021 · 3 seasons".
        # Printing "3 seasons" over a single year reads as a bug, and printing a bare
        # year over three of them hides that the cell pools them.
        tip = (f"params.data.{k}_vol == null ? '' : ("
               f"(params.data.{k}_n > 1"
               f"  ? params.data.{k}_from + '-' + params.data.{k}_to + ' · ' + "
               f"    params.data.{k}_n + ' seasons'"
               f"  : String(params.data.{k}_from))"
               f" + ' · ' + d3.format(',')(params.data.{k}_vol) + ' {spec['noun']}')")
        c = num(k, b["label"], 116, fmt)
        c["tooltipValueGetter"] = {"function": tip}
        cols.append(c)
    return cols


@callback(Output("xp-panel-grid", "children"), *_INPUTS)
def _panel_grid(role, metric, min_vol, censor, lg, mode):
    st = _state(role, metric, min_vol, censor, lg)
    p, m, mode = st["panel"], st["metric"], mode or "dark"
    if p.empty:
        return ui.note("No player-seasons clear this floor.", "warn")
    wide, bands = career.by_player(p, st["role"], m, st["lg"])
    if wide.empty:
        return ui.note("No player-seasons clear this floor.", "warn")
    note = None
    if career.YEAR_BANDS.get(st["lg"]):
        note = ui.note(
            "NFL careers run past eleven seasons here, and eleven columns scroll "
            "sideways — you cannot compare year 1 with year 9 if they are never on "
            "screen together. The groups are read off the corpus's own survival curve "
            "rather than cut evenly: 73% of NFL placekickers get a second season, and "
            "after that it is 87%, 90%, 100%, 77% — the rookie year is the only real "
            "cull, so it keeps its own column. Hover a cell for the seasons and the "
            "kicks behind it.", "info")
    return dmc.Stack(gap="xs", children=[
        note,
        ui.grid("xp-player-tbl", mode, rows=wide.to_dict("records"),
                columns=_player_cols(st["role"], m, bands), height="460px",
                row_id="aid"),
    ])
