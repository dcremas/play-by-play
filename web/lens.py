"""The three lenses the explorer reads the corpus through: offense, defense, kicks.

A lens is a *perspective*, not a table. Offense and defense are the same scrimmage
rows with the subject team swapped -- `offense_team_id` or `defense_team_id`
becomes `team_id`, the other becomes `opp_id`, and everything signed (the score margin,
points) flips with it. Special teams is a genuinely different fact with different
measures, which is why it is a different table upstream and a different view here.

Exactly one lens is on screen at a time, and that is the design. A rush row and a punt
row share almost no measured fields; a grid holding both would be mostly empty cells in
both directions. Making the side an exclusive choice means every column set, metric and
chart downstream only ever has one vocabulary to serve.

What each lens owns is here and nowhere else: which view it reads, what a row's subject
is called, which phase chips it offers, and which grains it can be rolled up to. The
generic machinery -- filter translation, sort translation, the infinite row model --
never learns about lenses at all, because all three views expose the same column names.

A lens is also INDEPENDENT of the league. All three work over either corpus, and which
corpus is a separate axis that lives in web/league.py. That split is why the prose below
is generated rather than written: the sentences that quote a play count or a coverage
percentage have to say a different number for each league, and a hardcoded one is simply
wrong half the time.
"""
from __future__ import annotations

KEYS = ["off", "def", "st"]
DEFAULT = "off"

LABEL = {"off": "Offense", "def": "Defense", "st": "Special teams"}
# Base view names. They are PREFIXED BY LEAGUE at creation -- cfb_st_play,
# nfl_st_play -- because there is now one DuckDB file per corpus and the league
# selects the view, not a WHERE clause. See web/data.py's con().
VIEW = {"off": "off_play", "def": "def_play", "st": "st_play"}


def view(key: str, league_key: str | None = None) -> str:
    """The view for one lens in one corpus.

    The league is part of the NAME rather than a predicate, which is the whole
    point of the split: there is no combined table left to forget to filter.
    """
    from . import league as _league
    return f"{_league.resolve(league_key)}_{VIEW[key]}"

# What one row is, for counts and prose. "1,510,679 plays selected".
NOUN = {"off": "plays", "def": "plays", "st": "kicks"}

# The subject of a row, and the other side. These label the sidebar facets, so they
# have to read as the thing being filtered: on the defense lens, "Team" means the
# defense and "Opponent" means the offense it faced.
SUBJECT = {"off": "Offense", "def": "Defense", "st": "Kicking team"}
OPPONENT = {"off": "Defense faced", "def": "Offense faced", "st": "Receiving team"}

# The headline player on a row. There is no single "the player" on a scrimmage play,
# so this is the one the play is *about* from the subject's side: whoever initiated it
# on offense, and the first tackler credited on defense.
PLAYER = {"off": "Passer / rusher", "def": "First tackler", "st": "Kicker / punter"}
# Templates, not strings. Every scalar in them -- the leaderboard floor, the id coverage --
# is a per-league fact, so the sentence is built for whichever corpus is on screen. Written
# out as literals here, this text claimed 98.7% id coverage in both leagues; it is 98.7% in
# one and 99.9% in the other.
_PLAYER_HINT = {
    "off": "The passer on a pass or sack, the rusher on a run. Receivers are their own "
           "column and their own filter. The list holds anyone with {floor}+ plays; type "
           "to search it.",
    "def": "ESPN credits one tackler per play in the structured field, and only on 41% "
           "of rushes and 28% of passes. Real defensive counts need the participant "
           "bridge -- that is the next pass, not this one. The list holds anyone with "
           "{floor}+ plays; type to search it.",
    "st": "Placekickers, punters and kickoff specialists, with {kfloor}+ kicks. {cov} of "
          "kicks carry an id. With Conversions selected the list also holds the "
          "passers and rushers on two-point tries — ESPN tags them patPasser, so "
          "they are the athlete on that row and reach this lens no other way.",
}


def player_hint(key: str, league_key: str | None = None) -> str:
    """The player picker's help text, for one lens in one league."""
    from . import league as lg
    lk = lg.resolve(league_key)
    return _PLAYER_HINT[resolve(key)].format(
        floor=lg.player_floor(lk), kfloor=lg.kicker_floor(lk), cov=lg.kicker_coverage(lk))


# --------------------------------------------------------------------------- phases
# A chip maps to one or more play_kind values. Most are 1:1; the conversion chip
# collapses three kinds that are one thing to a reader.
PHASES = {
    "off": {"rush": "Rush", "pass": "Pass", "sack": "Sack",
            "penalty": "Penalty", "other": "Other"},
    "def": {"rush": "Rush", "pass": "Pass", "sack": "Sack",
            "penalty": "Penalty", "other": "Other"},
    "st": {"field_goal": "Field goal", "punt": "Punt", "kickoff": "Kickoff",
           "conversion": "Conversions"},
}

# The plural noun for a chip, for prose. Not derivable from the chip label: three of
# the four read as an adjective ("field goal" plays) and the fourth is already plural
# ("Conversions"), which "No conversions plays" is the proof of.
PHASE_NOUN = {
    "off": {"rush": "rushes", "pass": "passes", "sack": "sacks",
            "penalty": "penalties", "other": "other plays"},
    "def": {"rush": "rushes", "pass": "passes", "sack": "sacks",
            "penalty": "penalties", "other": "other plays"},
    "st": {"field_goal": "field goals", "punt": "punts", "kickoff": "kickoffs",
           "conversion": "conversions"},
}

PHASE_ORDER = {
    "off": ["rush", "pass", "sack", "penalty", "other"],
    "def": ["rush", "pass", "sack", "penalty", "other"],
    "st": ["field_goal", "punt", "kickoff", "conversion"],
}

# Conversions are off by default and that is deliberate. They are the largest single
# phase in both corpora and sit at essentially ONE distance -- 67,678 college extra
# points and 14,861 NFL ones, all from the same spot -- so leaving them on would put a
# spike at one distance in every distance-based view and drown the three phases the
# kicking data is actually interesting for. The judgement is the same in both leagues,
# which is why this default is not per-league. They are one click away, and the corpus is
# complete either way.
#
# Default-off is now the ONLY thing that separates them from the other three phases.
# Until 2026-09-09 the chip led to a half-built phase: no measures in the aggregates,
# no columns on either leaderboard, no tab on either profile page, and two of the
# three explorer charts empty because a conversion has no distance. All of that is
# built. Do not read this default as "conversions are a lesser row" -- it is a
# statement about one axis of one chart family, not about the phase.
PHASE_DEFAULT = {
    "off": ["rush", "pass", "sack", "penalty", "other"],
    "def": ["rush", "pass", "sack", "penalty", "other"],
    "st": ["field_goal", "punt", "kickoff"],
}

KINDS = {
    "st": {"field_goal": ["field_goal"], "punt": ["punt"], "kickoff": ["kickoff"],
           "conversion": ["pat", "two_point", "defensive_conversion"]},
}

# --------------------------------------------------------------------------- grains
# Player leaderboards exist only where a player-grain query is honest today. On the
# kicks that is the kicker column -- see league.KICKER_ID_COVERAGE. On offense a
# leaderboard has to choose a
# role per play, and on defense it has to go through the participant bridge; both are
# the next pass, and offering an empty tab now would read as a missing feature rather
# than a deferred decision.
GRAINS = {
    "off": [{"value": "plays", "label": "Plays"}, {"value": "teams", "label": "Teams"}],
    "def": [{"value": "plays", "label": "Plays"}, {"value": "teams", "label": "Teams"}],
    "st": [{"value": "plays", "label": "Plays"},
           {"value": "kickers", "label": "Kickers & punters"},
           {"value": "teams", "label": "Teams"}],
}


# --------------------------------------------------------------------------- helpers
def resolve(key: str | None) -> str:
    return key if key in LABEL else DEFAULT


def is_scrimmage(key: str) -> bool:
    return resolve(key) in ("off", "def")


def chips(key: str) -> list[str]:
    return list(PHASE_ORDER[resolve(key)])


def default_chips(key: str) -> list[str]:
    return list(PHASE_DEFAULT[resolve(key)])


def kind_sql(chip: str, key: str = "st") -> str:
    """`play_kind` predicate for one chip. Most are 1:1; the conversion chip is not.

    Every caller that scopes a query to a single chip -- a profile tab, a season
    table, a trend line -- goes through this rather than interpolating the chip name,
    which is what stopped working the moment a chip stood for three kinds.
    """
    ks = KINDS.get(resolve(key), {}).get(chip, [chip])
    if len(ks) == 1:
        return f"play_kind = '{ks[0]}'"
    return "play_kind IN (" + ", ".join(f"'{k}'" for k in ks) + ")"


def kinds_for(key: str, chosen) -> list[str]:
    """Chip selection -> the play_kind values it means.

    An empty selection means "all of them", never "none": an empty grid is not what
    clearing the last chip meant, and the same rule already governs the kicks.
    """
    key = resolve(key)
    chosen = [c for c in (chosen or []) if c in PHASES[key]] or default_chips(key)
    table = KINDS.get(key)
    if not table:
        return list(chosen)
    out: list[str] = []
    for c in chosen:
        out.extend(table.get(c, [c]))
    return out


def segmented() -> list[dict]:
    """Data for the header's side selector."""
    return [{"value": k, "label": LABEL[k]} for k in KEYS]
