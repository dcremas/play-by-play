"""Does a kicker get better with experience?

Everything here is a count or a rate over counts. There is no model, no fitted baseline,
no significance test and no derived index -- the page states what the kickers actually
did and leaves the reading to whoever is looking at it. That is a deliberate constraint
and not a shortcut: a number a reader cannot reconstruct from the table under it is a
number they have to take on trust, and this app has never asked for that anywhere else.

What it does do is refuse to answer the question with the wrong arithmetic. The obvious
query -- label every player-season with how many seasons the man had already had, average
the make rate inside each label -- gives a clean rising curve, and the rise is mostly not
improvement:

**The year-two group is not the year-one group.** A kicker who misses in year one does
not get a year two. So comparing all of year two to all of year one compares the whole
intake against the survivors of it. The difference is large and it is visible without any
statistics at all: college year-one placekickers who never appeared again made 69.6% of
their field goals, and the ones who came back made 75.4% -- measured in year one, before
either group gained a season. `paired_steps()` therefore compares each player to
*himself* a year later, and `survivorship()` puts the two groups side by side so the gap
is on the page rather than asserted in a footnote.

**The corpus starts in 2014.** A player whose first row is a 2014 row may have been a
fourth-year starter in 2013, and calling that season "year 1" puts a veteran in the
freshman cohort -- 29.7% of the college year-one cohort. `panel(drop_censored=True)`, the
default, keeps only the careers that begin inside the window.

**A field goal is not a fixed task.** Attempt distance rises with experience, from 34.9
yards in a college placekicker's first season to 35.9 in his second, because the coach who
trusts him sends him out from further. Rather than adjust for that -- which would mean a
model -- every table carries mean attempt distance in the column beside the make rate, so
a reader can see the rate hold while the kicks get longer and draw their own conclusion.

What this module deliberately does NOT do is apply the sidebar filters. A player's
experience year is a fact about his whole career, and a season range that cut 2018-2022
out of the middle of it would renumber every season after the gap; a team filter would
turn one transfer into two rookie years. The panel is always the whole corpus for one
league, and web/pages/experience.py says so on the page.
"""
from __future__ import annotations

import functools

import numpy as np
import pandas as pd

from . import data, lens

# How many experience years to carry. Twelve rather than the six college eligibility
# would justify, because the two corpora do not have the same career length in them: a
# college placekicker is done in five or six seasons and an NFL one is not. A cap set for
# college truncated the NFL curve at six while the tile above it advertised eleven.
MAX_EXP = 12

# Below this many players a point is computed but not drawn. Not a significance rule --
# there are none here -- just the size below which a "group" is a handful of individuals
# and its average is a name rather than a level. Thin years stay in the tables, where the
# player count sits in the column beside the number and says so itself.
MIN_SHOWN = 15

# The came-back-or-not split gets a smaller floor. It compares two groups measured in the
# same season, and in a 32-team league only about five kickers a year lose the job -- at
# fifteen the NFL panel came back completely empty, which teaches a reader that the page
# is broken rather than that the league is small.
MIN_SPLIT = 5


# --------------------------------------------------------------------------- roles
# A role is a job, not a phase chip. The explorer's chips cut the ROWS; these cut the
# PEOPLE, and the difference matters here: a placekicker who also kicks off is one career
# with two jobs in it, and his kickoff seasons and his field-goal seasons improve at
# different rates. Each role carries its own volume column and its own floor, because
# "enough of a season to measure" is 5 field goals and 20 punts.
#
# `counts` are the raw tallies that go in the table beside the rates, so every rate on
# the page has its own numerator and denominator visible next to it.
ROLES = {
    "placekick": {
        "label": "Placekicker",
        "noun": "field goals",
        "vol": "fga",
        "vol_label": "Field goal attempts",
        "floor": 5,
        "metrics": ["fg_pct", "dist", "long"],
        "counts": [("fga", "Attempts"), ("fgm", "Made")],
    },
    "punt": {
        "label": "Punter",
        "noun": "punts",
        "vol": "punts",
        "vol_label": "Punts",
        "floor": 20,
        "metrics": ["net", "gross", "ret_rate"],
        "counts": [("punts", "Punts"), ("returned", "Returned")],
    },
    "kickoff": {
        "label": "Kickoff specialist",
        "noun": "kickoffs",
        "vol": "kos",
        "vol_label": "Kickoffs",
        "floor": 20,
        "metrics": ["tb_rate", "ko_dist"],
        "counts": [("kos", "Kickoffs"), ("touchbacks", "Touchbacks")],
    },
}
DEFAULT_ROLE = "placekick"

# A metric is a column pair plus how to print it. `better` says which direction is good,
# and it is not decoration: `ret_rate` is the one measure here that improves by falling,
# and counting a fall as a man who "got worse" would be wrong in the one place a reader
# would not check.
METRICS = {
    "fg_pct":   {"label": "Make rate", "unit": "pp", "fmt": "pct", "better": +1,
                 "num": "fgm", "den": "fga",
                 "blurb": "Field goals made divided by field goals attempted. Kicks "
                          "wiped out by a penalty are left out of both."},
    # Denominated on the attempts that HAVE a distance, not on all of them: 62 college
    # field goals and 9 NFL ones carry none, and a numerator and a denominator over
    # different sets of kicks is not an average.
    "dist":     {"label": "Mean attempt distance", "unit": "yd", "fmt": "one",
                 "better": 0, "num": "fg_dist_sum", "den": "fg_dist_n",
                 "blurb": "How far out the attempts were, on average. Not a measure of "
                          "how well he kicked -- a measure of how hard the kicks were. "
                          "It sits beside the make rate in every table for that reason."},
    # The only per-season metric here that is one kick rather than a rate over many, so
    # it is read off a column instead of a numerator over a denominator.
    "long":     {"label": "Longest made", "unit": "yd", "fmt": "int", "col": "long",
                 "better": +1,
                 "blurb": "His longest make that season. One kick, so it moves on how "
                          "often he got a long chance as much as on his leg."},
    "net":      {"label": "Net yards", "unit": "yd", "fmt": "one", "better": +1,
                 "num": "net_sum", "den": "net_n",
                 "blurb": "How far the ball ended up from where it was punted, after "
                          "any return. The measure a punter is actually judged on."},
    "gross":    {"label": "Gross yards", "unit": "yd", "fmt": "one", "better": +1,
                 "num": "gross_sum", "den": "gross_n",
                 "blurb": "How far he hit it, before the return team touched it."},
    "ret_rate": {"label": "Return rate allowed", "unit": "pp", "fmt": "pct", "better": -1,
                 "num": "returned", "den": "punts",
                 "blurb": "Share of his punts that got returned at all. Lower is "
                          "better: a punt fair-caught, downed or out of bounds is a "
                          "punt that went right."},
    "tb_rate":  {"label": "Touchback rate", "unit": "pp", "fmt": "pct", "better": +1,
                 "num": "touchbacks", "den": "ko_denom",
                 "blurb": "Share of kickoffs that went for a touchback. Onside kicks "
                          "are left out of the denominator -- they are a different "
                          "play, not a kickoff that went badly."},
    "ko_dist":  {"label": "Mean kickoff distance", "unit": "yd", "fmt": "one",
                 "better": +1, "num": "ko_dist_sum", "den": "ko_dist_n",
                 "blurb": "How far the ball travelled, whatever happened after it "
                          "landed."},
}


# Short column headers, for the tables only. A chart title has the width for "Mean
# attempt distance" and a 150px grid column does not -- and AG Grid truncates from the
# right, so the long form loses "distance" and leaves a header reading "Mean attempt".
TABLE_HEADER = {
    "dist": "Mean distance (yd)",
    "ko_dist": "Mean distance (yd)",
    "net": "Net (yd)",
    "gross": "Gross (yd)",
    "long": "Longest made (yd)",
}


def role_of(key: str | None) -> str:
    return key if key in ROLES else DEFAULT_ROLE


def metrics_for(role: str) -> list[dict]:
    return [{"value": m, "label": METRICS[m]["label"]}
            for m in ROLES[role_of(role)]["metrics"]]


def metric_of(role: str, key: str | None) -> str:
    allowed = ROLES[role_of(role)]["metrics"]
    return key if key in allowed else allowed[0]


def fmt_value(metric: str, v) -> str:
    """One value, printed the way its metric is read."""
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "—"
    kind = METRICS[metric]["fmt"]
    if kind == "pct":
        return f"{v:.1%}"
    if kind == "one":
        return f"{v:.1f}"
    return f"{v:,.0f}"


def tickformat(metric: str) -> str:
    return {"pct": ".0%", "one": ".1f", "int": ".0f"}[METRICS[metric]["fmt"]]


def value_format(metric: str) -> str:
    """The d3 format for a single value label on a chart."""
    return {"pct": ".1%", "one": ".1f", "int": ".0f"}[METRICS[metric]["fmt"]]


def _in_progress_list(lg: str) -> str:
    """The seasons to leave out, as a SQL list that is never empty.

    A season still being played is four games next to twelve, and every measure here is a
    per-season rate. Leaving 2026 in would put every active player's partial year into
    the panel as a full one -- and would make it his last observed season, which is what
    the came-back-or-not split keys on. `-1` is the empty case: `season NOT IN (-1)` is
    true for every row, where `NOT IN ()` is a syntax error.
    """
    rows = data.in_progress_seasons(lg)
    seasons = [int(r["season"]) for r in rows] or [-1]
    return ", ".join(str(s) for s in seasons)


# --------------------------------------------------------------------------- the panel
@functools.lru_cache(maxsize=8)
def _raw_panel(lg: str) -> pd.DataFrame:
    """Every kicker-season in one corpus, with the counters all three roles need.

    One query and one cache for all three roles, because the roles overlap in people:
    1,533 of the college kickers with five or more kicks work more than one of the three
    jobs, and running this per role would read the same rows three times to build three
    different careers out of the same man.
    """
    return data.q(f"""
        SELECT player_id                                                   AS aid,
               any_value(player)                                           AS player,
               season,
               arg_max(team, season)                                       AS team,
               arg_max(team_id, season)                                    AS team_id,
               count(DISTINCT game_id)                                     AS games,

               count(*) FILTER (play_kind = 'field_goal'
                                AND outcome <> 'Negated')                  AS fga,
               count(*) FILTER (play_kind = 'field_goal'
                                AND outcome = 'Made')                      AS fgm,
               sum(fg_distance_yds) FILTER (play_kind = 'field_goal'
                                AND outcome <> 'Negated')                  AS fg_dist_sum,
               count(*) FILTER (play_kind = 'field_goal' AND outcome <> 'Negated'
                                AND fg_distance_yds IS NOT NULL)           AS fg_dist_n,
               max(fg_distance_yds) FILTER (outcome = 'Made')              AS long,

               count(*) FILTER (play_kind = 'punt')                        AS punts,
               sum(punt_net_yds) FILTER (play_kind = 'punt')               AS net_sum,
               count(punt_net_yds) FILTER (play_kind = 'punt')             AS net_n,
               sum(punt_gross_yds) FILTER (play_kind = 'punt')             AS gross_sum,
               count(punt_gross_yds) FILTER (play_kind = 'punt')           AS gross_n,
               count(*) FILTER (play_kind = 'punt'
                                AND outcome = 'Returned')                  AS returned,

               count(*) FILTER (play_kind = 'kickoff')                     AS kos,
               count(*) FILTER (play_kind = 'kickoff'
                                AND NOT COALESCE(onside, FALSE))           AS ko_denom,
               count(*) FILTER (play_kind = 'kickoff'
                                AND outcome = 'Touchback')                 AS touchbacks,
               sum(kickoff_yds) FILTER (play_kind = 'kickoff')             AS ko_dist_sum,
               count(kickoff_yds) FILTER (play_kind = 'kickoff')           AS ko_dist_n
        FROM {lens.view("st", lg)}
        WHERE player_id IS NOT NULL
          AND season NOT IN ({_in_progress_list(lg)})
        GROUP BY player_id, season
    """)


def panel(lg: str, role: str, min_vol: int | None = None,
          drop_censored: bool = True) -> pd.DataFrame:
    """The player-seasons that qualify for one role, numbered by experience year.

    The numbering runs over the seasons that SURVIVE the volume floor, and that ordering
    is deliberate. A punter who took four field goals in 2019 and then kicked forty in
    2020 did not have a placekicking year one in 2019; counting it would push his real
    first season to year two and put a rookie in the sophomore cohort. The floor is part
    of the definition of the job, so it is applied before the numbering.

    Gap years are numbered by ORDER, not by calendar distance -- a man who kicked in 2016
    and 2019 is in his second season the second time, not his fourth. 7.5% of college
    kicker-seasons follow a gap, almost all of them a redshirt or an injury, and calling
    a redshirt year a year of experience is the opposite of what the page asks.
    """
    role = role_of(role)
    spec = ROLES[role]
    floor = spec["floor"] if min_vol is None else max(1, int(min_vol))

    df = _raw_panel(lg)
    df = df[df[spec["vol"]].fillna(0) >= floor].copy()
    if df.empty:
        return df.assign(exp=pd.Series(dtype=int), censored=pd.Series(dtype=bool),
                         came_back=pd.Series(dtype="boolean"))

    df = df.sort_values(["aid", "season"])
    df["exp"] = df.groupby("aid").cumcount() + 1
    # Censoring is judged on the corpus window, not on this role's filtered frame: a
    # placekicker whose 2014 was a punting season is still someone whose career began
    # before the corpus, and his first placekicking season is not necessarily his first.
    s0_all = _raw_panel(lg).groupby("aid")["season"].min()
    first_season = int(_raw_panel(lg)["season"].min())
    df["censored"] = df["aid"].map(s0_all).eq(first_season)
    if drop_censored:
        df = df[~df["censored"]]

    # Did he get another season in this job? Asked as "is there ANY later qualifying
    # season", not "is there one next year": 7.5% of these careers have a gap in them,
    # and a redshirt year would otherwise be recorded as a man who lost his job and then
    # as a second man who arrived from nowhere.
    #
    # NULL, not False, in the final complete season of the corpus. A player standing
    # there has no observable next year, so "did not come back" and "has not come back
    # yet" are the same row.
    #
    # `came_back`, NOT `returned`. `returned` is already a column on this frame -- the
    # count of a punter's punts that the other team ran back -- and writing the flag
    # here silently replaced it with a 0/1, so `ret_rate` divided a boolean by a season
    # of punts and reported a 1.9% return rate against a corpus that returns 25.5%. The
    # two words mean different things in football and only one of them can have the
    # column.
    last = int(_raw_panel(lg)["season"].max())
    later = df.groupby("aid")["season"].transform("max") > df["season"]
    df["came_back"] = later.where(df["season"] < last).astype("boolean")
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------- measures
def _value(df: pd.DataFrame, metric: str) -> pd.Series:
    """One metric, per player-season."""
    spec = METRICS[metric]
    if spec.get("col"):
        return pd.to_numeric(df[spec["col"]], errors="coerce")
    num = pd.to_numeric(df[spec["num"]], errors="coerce")
    den = pd.to_numeric(df[spec["den"]], errors="coerce")
    return num / den.where(den > 0)


def value_series(df: pd.DataFrame, metric: str) -> pd.Series:
    """One metric per player-season, for callers outside this module."""
    return _value(df, metric)


def _pooled(df: pd.DataFrame, metric: str) -> float:
    """The metric over a GROUP of player-seasons: total numerator over total denominator.

    Pooled, never a mean of per-season rates. A kicker with three attempts and a kicker
    with thirty are one vote each in a mean of rates, and the three-attempt season is the
    one whose rate is 0% or 100%. Putting the kicks on both sides of the fraction is the
    only form in which "this group made 75.6%" is a true sentence about the group -- and
    it is the form a reader can check against the Attempts and Made columns beside it.
    """
    spec = METRICS[metric]
    if spec.get("col"):
        v = pd.to_numeric(df[spec["col"]], errors="coerce")
        return float(v.mean()) if v.notna().any() else float("nan")
    num = pd.to_numeric(df[spec["num"]], errors="coerce").sum()
    den = pd.to_numeric(df[spec["den"]], errors="coerce").sum()
    return float(num / den) if den else float("nan")


def cohort_curve(p: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Everyone standing at each experience year, pooled.

    The shape the question is usually answered with. It is here so the page can put it
    next to the same-players comparison rather than instead of it: on its own it is not
    wrong so much as unanswerable, because the group changes from point to point and the
    `players` column is the only thing that says so.
    """
    out = []
    for e in range(1, MAX_EXP + 1):
        sub = p[p["exp"] == e]
        # STOP, not skip. Cohorts shrink monotonically -- every year-4 player was a
        # year-3 player -- so the first year under the floor is the last one worth
        # drawing, and breaking here cannot leave a hole in the middle of the line.
        if len(sub) < MIN_SHOWN:
            break
        out.append({"exp": e, "players": int(sub["aid"].nunique()),
                    "value": _pooled(sub, metric)})
    return pd.DataFrame(out)


def cohort_table(p: pd.DataFrame, role: str) -> pd.DataFrame:
    """Every year, every measure the role has, with the raw tallies beside them.

    The table the page is really for. One row per experience year carrying the counts and
    all of the role's measures at once, so a reader can see a make rate hold while the
    mean attempt distance climbs -- and reach the conclusion about degree of difficulty
    themselves, off numbers they can add up, rather than being handed an adjusted figure.

    Thin years are INCLUDED here, unlike on the charts. A row carries its own player and
    attempt counts, so a reader can see that year six is eleven people and weigh it; a
    point on a line cannot say that about itself.
    """
    role = role_of(role)
    spec = ROLES[role]
    out = []
    for e in range(1, MAX_EXP + 1):
        sub = p[p["exp"] == e]
        if sub.empty:
            continue
        row = {"exp": e, "players": int(sub["aid"].nunique())}
        for col, _ in spec["counts"]:
            row[col] = int(pd.to_numeric(sub[col], errors="coerce").sum() or 0)
        for m in spec["metrics"]:
            row[m] = _pooled(sub, m)
        out.append(row)
    return pd.DataFrame(out)


def paired_steps(p: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Year N against year N+1, for the players who appear in BOTH.

    The heart of it, in the plainest form it has: take the men who kicked in both
    seasons, state what that same group did the first year and what it did the second,
    and count how many of them got better, got worse and stayed level. Every number in
    the row is something a reader could work out by hand from the panel at the bottom of
    the page.

    Because it is the same people on both sides, the kicker who never got a year two is
    missing from BOTH columns rather than only from the right-hand one -- which is the
    whole difference between this table and the cohort one above it.
    """
    rows = []
    for e in range(1, MAX_EXP):
        a = p[p["exp"] == e].set_index("aid")
        b = p[p["exp"] == e + 1].set_index("aid")
        both = a.index.intersection(b.index)
        if len(both) == 0:
            continue
        ra, rb = a.loc[both], b.loc[both]
        va, vb = _value(ra, metric), _value(rb, metric)
        ok = va.notna() & vb.notna()
        va, vb = va[ok], vb[ok]
        n = int(ok.sum())
        if not n:
            continue
        # Direction of good, not sign of the subtraction. A punter whose return rate
        # allowed fell got BETTER, and counting that as a man who declined would be
        # wrong on the one measure nobody would check.
        d = (vb - va) * (METRICS[metric]["better"] or 1)
        better, worse = int((d > 0).sum()), int((d < 0).sum())
        rows.append({
            "step": f"{e}→{e + 1}", "from_exp": e, "n": n,
            "before": _pooled(ra[ok.values], metric),
            "after": _pooled(rb[ok.values], metric),
            "better": better, "worse": worse, "level": n - better - worse,
            "thin": n < MIN_SHOWN,
        })
    out = pd.DataFrame(rows)
    if not out.empty:
        out["change"] = out["after"] - out["before"]
    return out


def survivorship(p: pd.DataFrame, metric: str) -> pd.DataFrame:
    """At each experience year, the men who got another season against the ones who did not.

    Both groups measured in the SAME season, so nothing here is a development effect --
    neither group has gained a year yet. Whatever separates the two lines is who keeps
    the job, and it is the same separation the cohort curve mistakes for improvement.

    Player-seasons with no observable next season -- the final complete season of the
    corpus -- are excluded, because there "did not come back" cannot be told apart from
    "has not come back yet".
    """
    out = []
    for e in range(1, MAX_EXP + 1):
        sub = p[(p["exp"] == e) & p["came_back"].notna()]
        if sub.empty:
            break
        stay, gone = sub[sub["came_back"] == True], sub[sub["came_back"] == False]  # noqa: E712
        # Both arms above the floor, and STOP at the first year that fails rather than
        # skipping it. Skipping left the NFL curve with years 1,2,3,5,6,7,10 on a
        # categorical axis, which draws a straight segment from year 7 to year 10 and
        # invites the reader to read it as one season's worth of change. A line that ends
        # early says the evidence stops here; a line with a hole says something false.
        if len(stay) < MIN_SPLIT or len(gone) < MIN_SPLIT:
            break
        out.append({
            "exp": e,
            "stayed_n": int(stay["aid"].nunique()), "gone_n": int(gone["aid"].nunique()),
            "stayed": _pooled(stay, metric), "gone": _pooled(gone, metric),
        })
    df = pd.DataFrame(out)
    if not df.empty:
        df["gap"] = df["stayed"] - df["gone"]
    return df


def headline(steps: pd.DataFrame, metric: str) -> tuple[str, str]:
    """The first step, stated and not judged, for the tile.

    The first step only: year one to year two is where a development effect would be
    largest and has the most players behind it. What the tile says is what the group did
    -- "75.6% → 76.2%" -- with the counts underneath. It draws no conclusion, because
    the whole point of the page is that the reader draws it.
    """
    if steps.empty:
        return "—", "no player appears in two consecutive years"
    r = steps.iloc[0]
    return (f"{fmt_value(metric, r['before'])} → {fmt_value(metric, r['after'])}",
            f"{int(r['n']):,} players · {int(r['better'])} better, "
            f"{int(r['worse'])} worse, {int(r['level'])} level")


# How the per-player table groups a career into columns, per corpus.
#
# College is one column per season and needs no bands: eligibility caps a career at five
# or six, which fits across a screen, and the survival curve has a hard wall in it --
# 66% of college placekickers get a second season, 71% a third, 56% a fourth and 27% a
# fifth, then nothing. Every year is its own event.
#
# The NFL has no wall, so a career runs to eleven seasons in this panel and eleven
# columns scroll sideways. The bands below are read off its survival curve rather than
# chosen for tidiness, and the curve says one thing clearly: YEAR ONE IS THE ONLY CULL.
# 73% of NFL placekickers get a second season; after that it is 87%, 90%, 100%, 77%,
# 94%, 90% -- flat. Punters are the same shape (78%, then 84/82/100/95). The measure
# agrees: make rate is .826 and .833 in years one and two, then settles at .86-.87.
#
# So the rookie year is its own column. Grouping it with years two and three -- the
# obvious "every three seasons" scheme -- would hide the single structural break in the
# data inside the first band, on a page whose entire subject is that the men who leave
# are not a random sample. That scheme also puts 3 players and 5 seasons in a `10+`
# band, which is a column of blanks.
#
# `7+` is open-ended for the same reason: years 7, 8, 9, 10 and 11 hold 15, 9, 7, 3 and 2
# players, and no honest split of that tail has two sides.
YEAR_BANDS = {
    "cfb": None,                                  # one column per season
    "nfl": [(1, 1), (2, 3), (4, 6), (7, 99)],
}


def year_bands(lg: str, years: list[int]) -> list[dict]:
    """The column groups for one corpus, given the years actually present.

    A band with nobody in it is dropped rather than rendered empty -- raise the volume
    floor far enough and the NFL panel has no year-7 careers left, and a column headed
    `Years 7+` over 600 blank cells is a worse answer than no column.
    """
    if not years:
        return []
    spec = YEAR_BANDS.get(lg)
    if not spec:
        return [{"key": f"y{e}", "label": f"Year {e}", "lo": e, "hi": e} for e in years]
    top, out = max(years), []
    for lo, hi in spec:
        if not any(lo <= e <= hi for e in years):
            continue
        label = (f"Year {lo}" if lo == hi
                 else (f"Years {lo}+" if hi >= top else f"Years {lo}-{hi}"))
        out.append({"key": f"b{lo}_{hi}", "label": label, "lo": lo, "hi": hi})
    return out


def _band_value(sub: pd.DataFrame, metric: str) -> pd.Series:
    """One value per player over a band of seasons, keyed by athlete id.

    Pooled across the band the same way every other number on this page is pooled --
    total numerator over total denominator -- so a man's `Years 4-6` cell is his made
    over his attempted across those three seasons, not the average of three season
    rates. A 2-for-2 season would otherwise count as much as a 30-attempt one.

    `Longest made` is the exception and takes the MAX, because that is what the words
    mean: the longest he made in those years is a kick that happened, and the mean of
    three season-bests is not.
    """
    spec = METRICS[metric]
    g = sub.groupby("aid")
    if spec.get("col"):
        return pd.to_numeric(sub[spec["col"]], errors="coerce").groupby(sub["aid"]).max()
    num = pd.to_numeric(sub[spec["num"]], errors="coerce").groupby(sub["aid"]).sum()
    den = pd.to_numeric(sub[spec["den"]], errors="coerce").groupby(sub["aid"]).sum()
    return num / den.where(den > 0)


def by_player(p: pd.DataFrame, role: str, metric: str,
              lg: str = "cfb") -> tuple[pd.DataFrame, list[dict]]:
    """One row per player, with the measure spread across the career as columns.

    The long panel -- a row per player-season -- is the honest shape and is what every
    figure above is added up from, but it is the wrong shape for the question a reader
    actually brings to the bottom of this page: *did THIS man get better?* Answering it
    off the long form means finding a name, reading down four non-adjacent rows and
    holding four numbers in your head. Pivoted, his career is one line read left to right.

    The columns are seasons in college and bands of seasons in the NFL -- see YEAR_BANDS
    for why, and why the bands are not evenly sized. Nothing is aggregated away that a
    reader cannot get back: the per-column volume, the seasons it covers and how many
    there are ride along in hidden fields so the grid can put them in the tooltip. A
    100% column off five kicks has to be able to say so.

    Returns the frame and the column spec, because the caller has to build one column per
    band and only this function knows how many there are.
    """
    role = role_of(role)
    spec = ROLES[role]
    if p.empty:
        return pd.DataFrame(), []

    w = p.sort_values(["aid", "exp"])
    years = sorted(int(e) for e in w["exp"].unique())
    bands = year_bands(lg, years)

    # `dict.fromkeys` and not `set`: a career that went Duke -> Arkansas should read in
    # that order, and a set would print it in whichever order hashing happens to give.
    out = w.groupby("aid").agg(
        player=("player", "first"),
        teams=("team", lambda s: " · ".join(dict.fromkeys(s))),
        seasons=("exp", "size"),
        first_season=("season", "min"),
        last_season=("season", "max"),
        total_vol=(spec["vol"], "sum"),
    )
    for b in bands:
        sub = w[(w["exp"] >= b["lo"]) & (w["exp"] <= b["hi"])]
        if sub.empty:
            continue
        k, aid = b["key"], sub["aid"]
        out[k] = _band_value(sub, metric)
        out[f"{k}_vol"] = pd.to_numeric(sub[spec["vol"]], errors="coerce").groupby(aid).sum()
        out[f"{k}_n"] = sub.groupby("aid").size()
        out[f"{k}_from"] = sub.groupby("aid")["season"].min()
        out[f"{k}_to"] = sub.groupby("aid")["season"].max()
    # Longest careers first, then the busiest. The page is about change across years, so
    # a man with one season is the least informative row on it and alphabetical order
    # would put four hundred of them above the first career worth reading.
    out = out.sort_values(["seasons", "total_vol"], ascending=[False, False])
    return out.reset_index(), bands
