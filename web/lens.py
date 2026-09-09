"""The three lenses the explorer reads the corpus through: offense, defense, kicks.

A lens is a *perspective*, not a table. Offense and defense are the same 1,510,679
scrimmage rows with the subject team swapped -- `offense_team_id` or `defense_team_id`
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
"""
from __future__ import annotations

KEYS = ["off", "def", "st"]
DEFAULT = "off"

LABEL = {"off": "Offense", "def": "Defense", "st": "Special teams"}
VIEW = {"off": "off_play", "def": "def_play", "st": "st_play"}

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
PLAYER_HINT = {
    "off": "The passer on a pass or sack, the rusher on a run. Receivers are their own "
           "column and their own filter.",
    "def": "ESPN credits one tackler per play in the structured field, and only on 41% "
           "of rushes and 28% of passes. Real defensive counts need the participant "
           "bridge -- that is the next pass, not this one.",
    "st": "Placekickers, punters and kickoff specialists. 98.7% of kicks carry an id.",
}

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

PHASE_ORDER = {
    "off": ["rush", "pass", "sack", "penalty", "other"],
    "def": ["rush", "pass", "sack", "penalty", "other"],
    "st": ["field_goal", "punt", "kickoff", "conversion"],
}

# Conversions are off by default and that is deliberate. They are 71,460 rows at
# essentially one distance -- 67,678 of them extra points from the same spot -- so
# leaving them on would put a spike at one distance in every distance-based view and
# drown the three phases the kicking data is actually interesting for. They are one
# click away, and the corpus is complete either way.
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
# kicks that is the kicker column, at 98.7%. On offense a leaderboard has to choose a
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
