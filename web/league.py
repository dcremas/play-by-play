"""Which league the explorer is reading, and everything that is true of one and not the other.

The sibling of lens.py, and deliberately shaped like it. A LENS is a perspective on one
corpus (offense, defense, kicks); a LEAGUE is which corpus. They are independent: every
lens works in both leagues, so the header carries two selectors and not one combined list
of six.

The toggle itself is cheap because `league` is an ordinary column on both facts and
web/data.py's filter layer is column-driven -- it interpolates a view name and a WHERE
clause and never learns what is in either. What is NOT cheap, and is why this file exists,
is that the corpus-specific SCALARS were previously hardcoded in lens.py's prose: the play
counts, the coverage percentages, the leaderboard floors. Those are per-league facts, and a
sentence promising "98.7% of kicks carry an id" is wrong by half a point in one league and
by more than a point in the other.

Counts here are measured off the loaded warehouse on 2026-09-11, not estimated.
"""
from __future__ import annotations

KEYS = ["cfb", "nfl"]
DEFAULT = "cfb"

LABEL = {"cfb": "College", "nfl": "NFL"}
LONG = {"cfb": "D-I FBS college football", "nfl": "the NFL"}

# What the corpus holds, per league. Used in prose, empty states and the header subtitle.
# `kicks` is pbp.special_teams_play, `plays` is pbp.scrimmage_play.
#
# HAND-MAINTAINED, AND IT GOES STALE EVERY WEEK A LIVE SEASON IS PULLED FORWARD. These
# were measured on 2026-09-11 and were wrong by 2026-09-29 in both corpora -- the header
# was still reporting 3,297 NFL games against 3,343 actually loaded, and 10,470 college
# against 10,702. Re-measure them after any run of scripts/update_season.py:
#
#   for lg in cfb nfl; do .venv/bin/python -c "
#   import duckdb; c=duckdb.connect(f'data/out/pbp_$lg.duckdb', read_only=True)
#   q=lambda s: c.execute(s).fetchone()[0]
#   print('$lg', q('SELECT count(*) FROM play'), q('SELECT count(*) FROM scrimmage'),
#         q('SELECT count(*) FROM fact_game'), q('SELECT count(*) FROM dim_team'),
#         q('SELECT count(*) FROM drive'))"; done
#
# Reading them off the snapshot at import would end the drift, and is the obvious fix.
# It is not done here because these numbers feed lens.py's generated PROSE as well as the
# header, and that prose is written to read as a stable description of the corpus rather
# than as a live counter -- a sentence about coverage that moves every Sunday is a
# different design decision, not a bug fix. Measured 2026-09-29 against the snapshot.
CORPUS = {
    "cfb": {"kicks": 323_130, "plays": 1_543_171, "games": 10_702,
            "teams": 250, "seasons": "2014-2026", "drives": 264_220},
    "nfl": {"kicks": 92_068, "plays": 453_797, "games": 3_343,
            "teams": 32, "seasons": "2014-2026", "drives": 74_568},
}

# The word for the top-level grouping a team belongs to. Both leagues have a column called
# `conference`, but "Conference" means a 16-team league in one and half the sport in the
# other, and the NFL additionally has a division under it that college does not track.
GROUPING = {"cfb": "Conference", "nfl": "Conference"}
SUBGROUPING = {"cfb": None, "nfl": "Division"}

# `division` means FBS | FCS and exists only for college; `nfl_division` means AFC East
# and exists only for the NFL. The sidebar offers whichever the league actually has, which
# is the entire reason they are two columns upstream rather than one overloaded one.
DIVISION_COLUMN = {"cfb": "ncaa_division", "nfl": "nfl_division"}
DIVISION_LABEL = {"cfb": "Division (FBS/FCS)", "nfl": "Division"}

# Every college game in the corpus is FBS-vs-something; 5 code sites offer a filter to cut
# the FCS opponents out. The NFL has no second division, so the filter would be a checkbox
# that never changes the answer -- worse than absent, because it implies it might.
HAS_FBS_FILTER = {"cfb": True, "nfl": False}

# Kicker/punter id coverage, measured per league. The college number is the one lens.py has
# always quoted; the NFL's is higher because its participants feed is more complete.
KICKER_ID_COVERAGE = {"cfb": "98.7%", "nfl": "99.9%"}

# Minimum plays for a player to appear in a picker. The NFL corpus is roughly a third the
# size, so the college floor of 25 would hide genuine rotation players; scaled to keep the
# list the same order of length.
PLAYER_FLOOR = {"cfb": 25, "nfl": 10}
KICKER_FLOOR = {"cfb": 3, "nfl": 3}

# Which lenses are live. Both leagues carry all three -- the NFL kick parser reached 98.2%
# `exact`, against 98.51% on college -- so this is a full toggle and not a staged one.
LENSES = {"cfb": ["off", "def", "st"], "nfl": ["off", "def", "st"]}


def resolve(key: str | None) -> str:
    return key if key in LABEL else DEFAULT


def segmented() -> list[dict]:
    """Data for the header's league selector, shaped like lens.segmented()."""
    return [{"value": k, "label": LABEL[k]} for k in KEYS]


def corpus(key: str, field: str) -> int | str:
    return CORPUS[resolve(key)][field]


def division_column(key: str) -> str:
    return DIVISION_COLUMN[resolve(key)]


def division_label(key: str) -> str:
    return DIVISION_LABEL[resolve(key)]


def has_fbs_filter(key: str) -> bool:
    return HAS_FBS_FILTER[resolve(key)]


def player_floor(key: str) -> int:
    return PLAYER_FLOOR[resolve(key)]


def kicker_floor(key: str) -> int:
    return KICKER_FLOOR[resolve(key)]


def kicker_coverage(key: str) -> str:
    return KICKER_ID_COVERAGE[resolve(key)]
