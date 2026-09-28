"""AG Grid column definitions, tailored per lens and per phase.

Field relevance is the whole point here. A punt row has a gross, a net and a return;
a field goal row has a distance and a miss reason; a rush row has neither and has a
down and a gain instead. Rather than one 80-column grid with mostly-empty cells,
each (lens, phase) pair gets its own default set plus an opt-in list the user can
switch on from the Columns control.

The offense and defense lenses read the same rows and share the same underlying
columns, and differ only in what those columns are *called*: `yards_gained` is
"Yards" to the offense and "Yards allowed" to the defense, `is_turnover` is a
"Turnover" one way and a "Takeaway" the other. Relabelling rather than recomputing is
the point -- one event, two readings, no second copy of the number.
"""
from __future__ import annotations

from . import lens, theme

_CHECK = {"function": "params.value == null ? '' : (params.value ? '✓' : '·')"}
_COMMA = {"function": "params.value == null ? '' : d3.format(',')(params.value)"}
_SIGNED = {"function": "params.value == null ? '' : d3.format('+d')(params.value)"}
_CONF = {"function": "params.value == null ? '' : d3.format('.2f')(params.value)"}

_NUM = "agNumberColumnFilter"
_TXT = "agTextColumnFilter"
_DATE = "agDateColumnFilter"


def _num(field, header, width=90, fmt=None, filt=_NUM):
    d = {"field": field, "headerName": header, "filter": filt, "minWidth": width,
         "type": "rightAligned", "cellClass": "num-cell"}
    if fmt:
        d["valueFormatter"] = fmt
    return d


def _txt(field, header, width=130):
    return {"field": field, "headerName": header, "filter": _TXT, "minWidth": width}


def _name(field, header, width=150):
    return {"field": field, "headerName": header, "filter": _TXT,
            "minWidth": width, "cellClass": "name-cell"}


def _bool(field, header, width=70):
    return {"field": field, "headerName": header, "valueFormatter": _CHECK,
            "filter": _TXT, "minWidth": width, "cellClass": "bool-cell"}


def outcome_col(mode: str, key: str = "st") -> dict:
    """Outcome carries a colour chip so it reads as the same identity as the charts.

    The data-quality states -- Unknown, Negated, Unclassified -- are muted and italic
    on purpose. They are not outcomes and must not look like one.
    """
    colors = theme.outcome_colors(mode, key)
    muted = theme.MUTED_OUTCOMES
    conds = [
        {"condition": f"params.value == '{name}'",
         "style": {"color": hexv, "fontWeight": 500}}
        for name, hexv in colors.items() if name not in muted
    ]
    test = " || ".join(f"params.value == '{m}'" for m in muted)
    conds.append({"condition": test,
                  "style": {"color": theme.TOKENS[mode]["muted"], "fontStyle": "italic"}})
    tip = {
        "st": "params.value == 'Unknown' ? 'The play text states no outcome and every "
              "outcome flag is false -- unreadable, not absent' : params.value",
        "off": "params.value == 'Turnover TD' ? 'Lost the ball and the defense scored "
               "on the return' : params.value",
        "def": "params.value == 'Takeaway TD' ? 'Took the ball away and scored on the "
               "return' : params.value",
    }[key]
    return {"field": "outcome", "headerName": "Outcome", "filter": _TXT, "minWidth": 130,
            "cellStyle": {"styleConditions": conds},
            "tooltipValueGetter": {"function": tip}}


# --------------------------------------------------------------------------- catalogue
def _shared(mode: str, key: str) -> dict[str, dict]:
    """Columns every lens has, under the same names in every view."""
    subject = lens.SUBJECT[key]
    return {
        "game_date": {"field": "game_date", "headerName": "Date", "filter": _DATE,
                      "minWidth": 106},
        "season": _num("season", "Season", 88),
        "week": _num("week", "Wk", 62),
        "season_type": _txt("season_type", "Season type", 110),
        "phase": _txt("phase", "Phase", 96),
        "player": _name("player", lens.PLAYER[key], 150),
        "team": _txt("team", subject, 170),
        "conference": _txt("conference", "Conference", 150),
        "ncaa_division": _txt("ncaa_division", "Div", 70),
        "opponent": _txt("opponent", "Opponent", 170),
        "opp_conference": _txt("opp_conference", "Opp conference", 150),
        "site": _txt("site", "Site", 82),
        "outcome": outcome_col(mode, key),
        "qtr": _num("qtr", "Qtr", 62),
        "clock": {"field": "clock", "headerName": "Clock", "filter": _TXT,
                  "minWidth": 78, "type": "rightAligned", "cellClass": "num-cell"},
        "down": _num("down", "Down", 70),
        "dist_to_go": _num("dist_to_go", "To go", 74),
        "yards_to_goal": _num("yards_to_goal", "YTG", 70),
        "score_diff": _num("score_diff", "Score Δ", 84, _SIGNED),
        "score_state": _txt("score_state", "Score state", 108),
        "is_clutch": _bool("is_clutch", "Clutch", 76),
        "game_secs_remaining": _num("game_secs_remaining", "Game secs", 100, _COMMA),
        "venue_name": _txt("venue_name", "Venue", 175),
        "venue_city": _txt("venue_city", "City", 120),
        "venue_state": _txt("venue_state", "St", 62),
        "surface": _txt("surface", "Surface", 88),
        "attendance": _num("attendance", "Attendance", 110, _COMMA),
        "neutral_site": _bool("neutral_site", "Neutral", 82),
        "conference_game": _bool("conference_game", "Conf game", 96),
        "play_text": {"field": "play_text", "headerName": "Play text", "filter": _TXT,
                      "minWidth": 420, "tooltipField": "play_text",
                      "cellClass": "mono-cell"},
    }


def _kick_only(mode: str) -> dict[str, dict]:
    return {
        "player_conf": _num("player_conf", "Name conf", 96, _CONF),
        "player_name_unparsed": _bool("player_name_unparsed", "Name raw", 88),
        "fg_distance_yds": _num("fg_distance_yds", "Dist (yd)", 96),
        "punt_gross_yds": _num("punt_gross_yds", "Gross", 82),
        "punt_net_yds": _num("punt_net_yds", "Net", 74),
        "kickoff_yds": _num("kickoff_yds", "Kick (yd)", 92),
        "kick_yds": _num("kick_yds", "Kick (yd)", 92),
        "return_yds": _num("return_yds", "Return", 82),
        "returner_name": _name("returner_name", "Returner", 145),
        "tackler_name": _name("tackler_name", "Tackler", 145),
        "blocker_name": _name("blocker_name", "Blocked by", 140),
        "miss_reason": _txt("miss_reason", "Miss reason", 118),
        "returned_for_td": _bool("returned_for_td", "Ret TD", 78),
        "onside": _bool("onside", "Onside", 78),
        "converted": _bool("converted", "Converted", 96),
        "two_point_type": _txt("two_point_type", "2pt type", 88),
        "negated_by_penalty": _bool("negated_by_penalty", "Negated", 84),
        "parse_confidence": _txt("parse_confidence", "Parse", 92),
    }


# What a scrimmage measure is called from each side. The column is the same column.
_SCRIM_LABEL = {
    "off": {"yards_gained": "Yards", "first_down_gained": "1st down",
            "is_touchdown": "TD", "is_turnover": "Turnover", "points_scored": "Points"},
    "def": {"yards_gained": "Yds allowed", "first_down_gained": "1st allowed",
            "is_touchdown": "TD allowed", "is_turnover": "Takeaway",
            "points_scored": "Points"},
}


def _scrim_only(mode: str, key: str) -> dict[str, dict]:
    lab = _SCRIM_LABEL[key]
    tackler = _name("tackler_name", "First tackler", 150)
    # ESPN's structured tackler field is credited on 41% of rushes and 28% of passes.
    # A column that is two-thirds empty has to say why in the header, or it reads as
    # a bug rather than as the limit of the feed.
    tackler["headerTooltip"] = (
        "ESPN credits one tackler per play in the structured field, and only on 41% "
        "of rushes and 28% of passes. An empty cell means the feed named nobody, not "
        "that nobody made the tackle.")
    return {
        "yards_gained": _num("yards_gained", lab["yards_gained"], 92),
        "yards_impossible": _bool("yards_impossible", "Yds bad", 84),
        "first_down_gained": _bool("first_down_gained", lab["first_down_gained"], 92),
        "is_complete": _bool("is_complete", "Complete", 92),
        "is_touchdown": _bool("is_touchdown", lab["is_touchdown"], 84),
        "is_turnover": _bool("is_turnover", lab["is_turnover"], 92),
        "is_penalty": _bool("is_penalty", "Penalty", 84),
        "points_scored": _num("points_scored", lab["points_scored"], 84, _SIGNED),
        "passer_name": _name("passer_name", "Passer", 150),
        "rusher_name": _name("rusher_name", "Rusher", 150),
        "receiver_name": _name("receiver_name", "Receiver", 150),
        "tackler_name": tackler,
        "passer_position": _txt("passer_position", "Passer pos", 96),
        "rusher_position": _txt("rusher_position", "Rusher pos", 96),
        "receiver_position": _txt("receiver_position", "Recv pos", 92),
        "tackler_position": _txt("tackler_position", "Tackler pos", 100),
        "play_type_espn": _txt("play_type_espn", "ESPN type", 165),
        "distance_bucket": _txt("distance_bucket", "To-go band", 100),
        "field_zone": _txt("field_zone", "Field zone", 112),
        "end_yards_to_goal": _num("end_yards_to_goal", "End YTG", 92),
        "end_down": _num("end_down", "End down", 96),
        "drive_number": _num("drive_number", "Drive", 78),
    }


def catalogue(mode: str, key: str = "st") -> dict[str, dict]:
    key = lens.resolve(key)
    cat = _shared(mode, key)
    cat.update(_scrim_only(mode, key) if lens.is_scrimmage(key) else _kick_only(mode))
    return cat


# --------------------------------------------------------------------------- column sets
_KICK_HEAD = ["game_date", "season", "player", "team", "opponent", "site"]
_KICK_TAIL = ["qtr", "clock", "score_diff"]

_OFF_HEAD = ["game_date", "season", "player", "team", "opponent"]
_DEF_HEAD = ["game_date", "season", "team", "opponent"]
_SIT = ["down", "dist_to_go", "yards_to_goal"]

DEFAULTS = {
    "st": {
        "field_goal": _KICK_HEAD + ["fg_distance_yds", "outcome",
                                    "miss_reason"] + _KICK_TAIL,
        "punt": _KICK_HEAD + ["punt_gross_yds", "punt_net_yds", "return_yds", "outcome",
                              "returner_name", "yards_to_goal"] + _KICK_TAIL[:1],
        "kickoff": _KICK_HEAD + ["kickoff_yds", "return_yds", "outcome",
                                 "returner_name", "onside"] + _KICK_TAIL,
        "conversion": _KICK_HEAD + ["phase", "outcome",
                                    "two_point_type"] + _KICK_TAIL,
        "mixed": ["game_date", "season", "phase", "player", "team", "opponent",
                  "kick_yds", "return_yds", "outcome"] + _KICK_TAIL,
    },
    "off": {
        "rush": _OFF_HEAD + _SIT + ["yards_gained", "outcome", "tackler_name"],
        "pass": _OFF_HEAD + ["receiver_name"] + _SIT + ["yards_gained", "is_complete",
                                                        "outcome"],
        "sack": _OFF_HEAD + _SIT + ["yards_gained", "outcome", "tackler_name"],
        "penalty": _DEF_HEAD + _SIT + ["yards_gained", "outcome", "play_text"],
        "other": _OFF_HEAD + ["phase"] + _SIT + ["yards_gained", "outcome",
                                                 "play_text"],
        "mixed": ["game_date", "season", "phase", "player", "team", "opponent"] + _SIT
                 + ["yards_gained", "outcome"],
    },
    "def": {
        "rush": _DEF_HEAD + _SIT + ["yards_gained", "outcome", "rusher_name",
                                    "player"],
        "pass": _DEF_HEAD + _SIT + ["yards_gained", "outcome", "passer_name",
                                    "receiver_name", "player"],
        "sack": _DEF_HEAD + _SIT + ["yards_gained", "outcome", "passer_name",
                                    "player"],
        "penalty": _DEF_HEAD + _SIT + ["yards_gained", "outcome", "play_text"],
        "other": _DEF_HEAD + ["phase"] + _SIT + ["yards_gained", "outcome",
                                                 "play_text"],
        "mixed": ["game_date", "season", "phase", "team", "opponent"] + _SIT
                 + ["yards_gained", "outcome", "player"],
    },
}

_OPT_ENV = [
    "week", "season_type", "conference", "opp_conference", "ncaa_division",
    "score_state", "is_clutch", "game_secs_remaining",
    "venue_name", "venue_city", "venue_state", "surface", "attendance",
    "neutral_site", "conference_game", "play_text",
]

_OPT_KICK = [
    "player_conf", "player_name_unparsed", "tackler_name", "returned_for_td",
    "down", "dist_to_go", "yards_to_goal", "converted", "two_point_type",
] + _OPT_ENV

# Conversions get their own optional list rather than the kick one. Four of its
# columns are NULL on all 71,460 conversion rows -- `emit_pat` nulls the return and
# block fields when it lifts the row out of the touchdown text, and no conversion in
# the corpus carries a returner, a return, a blocker or a return touchdown -- so
# offering them is offering an empty column. Down, distance and yards to goal stay,
# because they are now honest: the view nulls the touchdown's values on the 71,330
# derived rows and keeps the real ones on the 130 conversions ESPN emits as their own
# play.
_OPT_CONV = [c for c in _OPT_KICK if c != "returned_for_td"]

_OPT_SCRIM = [
    "phase", "site", "qtr", "clock", "score_diff", "points_scored",
    "first_down_gained", "is_touchdown", "is_turnover", "is_penalty", "is_complete",
    "passer_name", "rusher_name", "receiver_name", "tackler_name", "player",
    "passer_position", "rusher_position", "receiver_position", "tackler_position",
    "play_type_espn", "distance_bucket", "field_zone", "end_yards_to_goal",
    "end_down", "drive_number", "yards_impossible",
] + _OPT_ENV

OPTIONAL = {
    "st": {
        "field_goal": ["blocker_name", "negated_by_penalty"] + _OPT_KICK,
        "punt": ["blocker_name", "qtr", "clock", "score_diff"] + _OPT_KICK,
        "kickoff": _OPT_KICK,
        "conversion": _OPT_CONV,
        "mixed": ["fg_distance_yds", "punt_gross_yds", "punt_net_yds", "kickoff_yds",
                  "returner_name", "miss_reason", "onside"] + _OPT_KICK,
    },
    "off": {k: _OPT_SCRIM for k in ("rush", "pass", "sack", "penalty", "other",
                                    "mixed")},
    "def": {k: _OPT_SCRIM for k in ("rush", "pass", "sack", "penalty", "other",
                                    "mixed")},
}


def key_for(chips) -> str:
    """One phase selected -> that phase's tailored column set; otherwise the mixed set."""
    chips = list(chips or [])
    return chips[0] if len(chips) == 1 else "mixed"


def build(key: str, chips, extra: list[str] | None = None,
          mode: str = "dark") -> list[dict]:
    key = lens.resolve(key)
    cat = catalogue(mode, key)
    names = list(DEFAULTS[key].get(key_for(chips), DEFAULTS[key]["mixed"]))
    for name in extra or []:
        if name not in names and name in cat:
            names.append(name)
    return [cat[n] for n in names if n in cat]


def optional_options(key: str, chips, mode: str = "dark") -> list[dict]:
    key = lens.resolve(key)
    cat = catalogue(mode, key)
    ck = key_for(chips)
    seen = set(DEFAULTS[key].get(ck, DEFAULTS[key]["mixed"]))
    out = []
    for n in OPTIONAL[key].get(ck, []):
        if n in seen or n not in cat:
            continue
        seen.add(n)
        out.append({"value": n, "label": cat[n].get("headerName", n)})
    return sorted(out, key=lambda d: d["label"])


# Columns fetched for the play-detail drawer regardless of what the grid shows.
_DETAIL_SHARED = [
    # No `league` here. The drawer does still need the corpus, and for the reason this
    # comment used to give -- it builds league-qualified profile links, and a bare team
    # or athlete id is ambiguous across the two. It takes it from the `lg` argument it is
    # called with. There is no `league` COLUMN to take it from: the split put the corpus
    # in the view NAME (see lens.view), and asking for one here made every single detail
    # render raise a BinderException.
    "play_uid", "phase", "play_kind", "season", "week", "season_type", "game_id",
    "game_date", "outcome", "outcome_unknown", "player", "player_id", "player_conf",
    "player_name_unparsed", "team", "team_id", "opponent", "opp_id", "site",
    "conference", "opp_conference", "qtr", "clock", "down", "dist_to_go",
    "yards_to_goal", "score_diff", "score_state", "is_clutch",
    "venue_name", "venue_city", "venue_state", "surface", "attendance",
    "neutral_site", "conference_game", "play_text", "source",
]

DETAIL_FIELDS = {
    "st": _DETAIL_SHARED + [
        "returner_name", "returner_athlete_id", "tackler_name", "tackler_athlete_id",
        # NFL only; NULL on every college row, and the drawer drops an empty card.
        "blocker_name", "snapper_name", "holder_name",
        "fg_distance_yds", "punt_gross_yds", "punt_net_yds",
        "kickoff_yds", "return_yds", "kick_yds", "returned_for_td", "onside",
        "converted", "two_point_type", "miss_reason", "negated_by_penalty",
        "parse_confidence",
    ],
    "off": _DETAIL_SHARED + [
        "yards_gained", "yards_impossible", "first_down_gained", "is_complete",
        "is_touchdown", "is_turnover", "is_penalty", "points_scored",
        "end_down", "end_distance", "end_yards_to_goal", "play_type_espn",
        "distance_bucket", "field_zone", "drive_number",
        "passer_name", "passer_athlete_id", "passer_position",
        "rusher_name", "rusher_athlete_id", "rusher_position",
        "receiver_name", "receiver_athlete_id", "receiver_position",
        "tackler_name", "tackler_athlete_id", "tackler_position",
    ],
}
DETAIL_FIELDS["def"] = DETAIL_FIELDS["off"]


def detail_fields(key: str) -> list[str]:
    return DETAIL_FIELDS[lens.resolve(key)]
