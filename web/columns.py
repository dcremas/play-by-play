"""AG Grid column definitions, tailored per phase.

Field relevance is the whole point here: a punt row has a gross, a net and a return;
a field goal row has a distance and a miss reason. Rather than one 60-column grid
with mostly-empty cells, each phase gets its own default set plus an opt-in list the
user can switch on from the Columns control.
"""
from __future__ import annotations

from . import theme

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


def _bool(field, header, width=70):
    return {"field": field, "headerName": header, "valueFormatter": _CHECK,
            "filter": _TXT, "minWidth": width, "cellClass": "bool-cell"}


def outcome_col(mode: str) -> dict:
    """Outcome carries a colour chip so it reads as the same identity as the charts.

    Unknown is muted and italic on purpose -- it is a data-quality state, not an
    outcome, and must not look like one.
    """
    colors = theme.outcome_colors(mode)
    conds = [
        {"condition": f"params.value == '{name}'",
         "style": {"color": hexv, "fontWeight": 500}}
        for name, hexv in colors.items() if name not in ("Unknown", "Negated")
    ]
    conds.append({"condition": "params.value == 'Unknown' || params.value == 'Negated'",
                  "style": {"color": theme.TOKENS[mode]["muted"], "fontStyle": "italic"}})
    return {"field": "outcome", "headerName": "Outcome", "filter": _TXT, "minWidth": 120,
            "cellStyle": {"styleConditions": conds},
            "tooltipValueGetter": {"function":
                "params.value == 'Unknown' ? 'The play text states no outcome and every "
                "outcome flag is false -- unreadable, not absent' : params.value"}}


# --------------------------------------------------------------------------- catalogue
def catalogue(mode: str) -> dict[str, dict]:
    return {
        "game_date": {"field": "game_date", "headerName": "Date", "filter": _DATE,
                      "minWidth": 106},
        "season": _num("season", "Season", 88),
        "week": _num("week", "Wk", 62),
        "season_type": _txt("season_type", "Season type", 110),
        "phase": _txt("phase", "Phase", 96),
        "player": {"field": "player", "headerName": "Kicker", "filter": _TXT,
                   "minWidth": 150, "cellClass": "name-cell"},
        "player_conf": _num("player_conf", "Name conf", 96, _CONF),
        "player_name_unparsed": _bool("player_name_unparsed", "Name raw", 88),
        "team": _txt("team", "Team", 170),
        "conference": _txt("conference", "Conference", 150),
        "division": _txt("division", "Div", 70),
        "opponent": _txt("opponent", "Opponent", 170),
        "opp_conference": _txt("opp_conference", "Opp conference", 150),
        "site": _txt("site", "Site", 82),
        "outcome": outcome_col(mode),
        "fg_distance_yds": _num("fg_distance_yds", "Dist (yd)", 96),
        "punt_gross_yds": _num("punt_gross_yds", "Gross", 82),
        "punt_net_yds": _num("punt_net_yds", "Net", 74),
        "kickoff_yds": _num("kickoff_yds", "Kick (yd)", 92),
        "kick_yds": _num("kick_yds", "Kick (yd)", 92),
        "return_yds": _num("return_yds", "Return", 82),
        "returner_name": _txt("returner_name", "Returner", 145),
        "tackler_name": _txt("tackler_name", "Tackler", 145),
        "blocker_name": _txt("blocker_name", "Blocked by", 140),
        "miss_reason": _txt("miss_reason", "Miss reason", 118),
        "returned_for_td": _bool("returned_for_td", "Ret TD", 78),
        "onside": _bool("onside", "Onside", 78),
        "negated_by_penalty": _bool("negated_by_penalty", "Negated", 84),
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
        "parse_confidence": _txt("parse_confidence", "Parse", 92),
        "play_text": {"field": "play_text", "headerName": "Play text", "filter": _TXT,
                      "minWidth": 420, "tooltipField": "play_text",
                      "cellClass": "mono-cell"},
    }


# --------------------------------------------------------------------------- per-phase sets
_SHARED_HEAD = ["game_date", "season", "player", "team", "opponent", "site"]
_SHARED_TAIL = ["qtr", "clock", "score_diff"]

DEFAULTS = {
    "field_goal": _SHARED_HEAD + ["fg_distance_yds", "outcome", "miss_reason"] + _SHARED_TAIL,
    "punt": _SHARED_HEAD + ["punt_gross_yds", "punt_net_yds", "return_yds", "outcome",
                            "returner_name", "yards_to_goal"] + _SHARED_TAIL[:1],
    "kickoff": _SHARED_HEAD + ["kickoff_yds", "return_yds", "outcome", "returner_name",
                               "onside"] + _SHARED_TAIL,
    "mixed": ["game_date", "season", "phase", "player", "team", "opponent", "kick_yds",
              "return_yds", "outcome"] + _SHARED_TAIL,
}

_OPTIONAL_COMMON = [
    "week", "season_type", "conference", "opp_conference", "division", "player_conf",
    "player_name_unparsed",
    "tackler_name", "returned_for_td", "down", "dist_to_go", "yards_to_goal",
    "score_state", "is_clutch", "game_secs_remaining",
    "venue_name", "venue_city", "venue_state", "surface", "attendance",
    "neutral_site", "conference_game", "parse_confidence", "play_text",
]

OPTIONAL = {
    "field_goal": ["blocker_name", "negated_by_penalty"] + _OPTIONAL_COMMON,
    "punt": ["blocker_name", "qtr", "clock", "score_diff"] + _OPTIONAL_COMMON,
    "kickoff": _OPTIONAL_COMMON,
    "mixed": ["fg_distance_yds", "punt_gross_yds", "punt_net_yds", "kickoff_yds",
              "returner_name", "miss_reason", "onside"] + _OPTIONAL_COMMON,
}


def key_for(phases: list[str] | set[str] | None) -> str:
    """One phase selected -> that phase's tailored column set; otherwise the mixed set."""
    phases = list(phases or [])
    return phases[0] if len(phases) == 1 else "mixed"


def build(phases, extra: list[str] | None = None, mode: str = "dark") -> list[dict]:
    cat = catalogue(mode)
    key = key_for(phases)
    names = list(DEFAULTS[key])
    for name in extra or []:
        if name not in names and name in cat:
            names.append(name)
    return [cat[n] for n in names if n in cat]


def optional_options(phases, mode: str = "dark") -> list[dict]:
    cat = catalogue(mode)
    key = key_for(phases)
    seen = set(DEFAULTS[key])
    out = []
    for n in OPTIONAL[key]:
        if n in seen or n not in cat:
            continue
        seen.add(n)
        out.append({"value": n, "label": cat[n].get("headerName", n)})
    return sorted(out, key=lambda d: d["label"])


# Columns fetched for the play-detail drawer regardless of what the grid shows.
DETAIL_FIELDS = [
    "play_uid", "phase", "play_kind", "season", "week", "season_type", "game_id",
    "game_date", "outcome", "outcome_unknown", "player", "player_id", "player_conf",
    "player_name_unparsed",
    "returner_name", "returner_athlete_id", "tackler_name", "tackler_athlete_id",
    "blocker_name", "team", "team_id", "opponent", "opp_id", "site", "conference",
    "opp_conference", "fg_distance_yds", "punt_gross_yds", "punt_net_yds",
    "kickoff_yds", "return_yds", "kick_yds", "returned_for_td", "onside",
    "miss_reason", "negated_by_penalty", "qtr", "clock", "down", "dist_to_go",
    "yards_to_goal", "score_diff", "score_state", "is_clutch",
    "venue_name", "venue_city", "venue_state", "surface", "attendance",
    "neutral_site", "conference_game", "play_text", "parse_confidence", "source",
]
