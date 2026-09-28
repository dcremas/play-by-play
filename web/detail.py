"""The single-play detail view.

People lead: who kicked it, who caught it, who made the tackle, who got a hand on
it -- each with the name-match confidence the parser assigned and a link through to
their profile, and each kicker carrying the line he had built in that season *before*
this kick. Situation, environment and the verbatim ESPN text sit underneath, one
click away, so an odd-looking row can always be audited back to its source.
"""
from __future__ import annotations

import dash_mantine_components as dmc
import pandas as pd
from dash import dcc, html

from . import columns, data, league, lens, routes, theme, ui

_ROLE = {"field_goal": "Placekicker", "punt": "Punter", "kickoff": "Kickoff",
         "pat": "Placekicker", "two_point": "Passer / rusher",
         "defensive_conversion": "Defense"}

# The three kinds the conversion chip covers. They share a drawer panel: what a
# reader wants off a conversion row is whether it converted and how it was tried.
_CONVERSIONS = ("pat", "two_point", "defensive_conversion")


def _na(x) -> bool:
    """True for None, NaN, NaT and pd.NA alike."""
    return x is None or bool(pd.isna(x))


def _outcome_badge(outcome: str, mode: str, key: str = "st"):
    hexv = theme.outcome_colors(mode, key).get(outcome)
    kw = {"style": {"color": hexv}} if hexv else {}
    return dmc.Badge(outcome, variant="light", size="sm", radius="sm", **kw)


def _kv(key, value, mono=False):
    if value is None or value == "":
        value = "—"
    return html.Div(className="kv-row", children=[
        html.Span(key, className="kv-key"),
        html.Span(str(value), className="kv-val",
                  style={"fontFamily": "ui-monospace, Menlo, monospace"} if mono else None),
    ])


def _kv_block(pairs):
    return html.Div([_kv(k, v) for k, v in pairs])


def _conf_badge(conf):
    """Name-match confidence, always with a word beside it -- never colour alone."""
    if conf is None:
        return None
    label = "exact" if conf >= 0.999 else ("likely" if conf >= 0.9 else "uncertain")
    return dmc.Badge(f"name {label} · {conf:.2f}", variant="light", size="xs", radius="sm")


def _prior_line(row, lg: str) -> str | None:
    """The kicker's season-to-date line immediately before this kick."""
    pid, season, kind = row["player_id"], int(row["season"]), row["play_kind"]
    if _na(pid):
        # An unlinked kick has no athlete id to accumulate a season line against.
        # `pid is None` was not enough: the column is a nullable integer, so the
        # missing value arrives as pd.NA, which is not None.
        return None
    where = (f"player_id = {int(pid)} AND season = {season} AND play_kind = "
             f"{data.lit(kind)} AND (game_date, play_uid) < "
             f"(DATE {data.lit(str(row['game_date'])[:10])}, {data.lit(row['play_uid'])})")
    if kind == "field_goal":
        r = data.q(f"""SELECT count(*) n,
                              sum(CASE WHEN outcome='Made' THEN 1 ELSE 0 END) made
                       FROM {lens.view("st", lg)} WHERE {where} AND outcome <> 'Negated'""").iloc[0]
        if not r.n:
            return "first field goal attempt of the season"
        return f"{int(r.made)} of {int(r.n)} on the season before this kick"
    if kind == "punt":
        r = data.q(f"""SELECT count(*) n, avg(punt_gross_yds) g, avg(punt_net_yds) net
                       FROM {lens.view("st", lg)} WHERE {where}""").iloc[0]
        if not r.n:
            return "first punt of the season"
        net = "" if _na(r.net) else f", {r.net:.1f} net"
        return f"{int(r.n)} punts on the season before this one · {r.g:.1f} gross{net}"
    if kind in _CONVERSIONS:
        noun = "extra point" if kind == "pat" else "two-point try"
        r = data.q(f"""SELECT count(*) n,
                              sum(CASE WHEN outcome='Converted' THEN 1 ELSE 0 END) made
                       FROM {lens.view("st", lg)} WHERE {where}""").iloc[0]
        if not r.n:
            return f"first {noun} of the season"
        return f"{int(r.made)} of {int(r.n)} on the season before this one"
    r = data.q(f"""SELECT count(*) n,
                          sum(CASE WHEN outcome='Touchback' THEN 1 ELSE 0 END) tb
                   FROM {lens.view("st", lg)} WHERE {where}""").iloc[0]
    if not r.n:
        return "first kickoff of the season"
    return (f"{int(r.n)} kickoffs on the season before this one · "
            f"{int(r.tb)} touchbacks ({r.tb / r.n:.0%})")


def _person(role, name, *, athlete_id=None, conf=None, sub=None, mode="dark",
            lg=None):
    if not name:
        return None
    heading = html.Div(name, className="person-name")
    if not _na(athlete_id):
        # League-qualified, because the same athlete id names a college career and a pro
        # one and the drawer knows which row it came from.
        heading = dcc.Link(heading,
                           href=routes.url(f"/player/{league.resolve(lg)}/{int(athlete_id)}"),
                           className="st-link")
    badges = [b for b in [_conf_badge(conf)] if b is not None]
    return dmc.Paper(withBorder=True, radius="md", p="sm", className="person-card",
                     children=dmc.Stack(gap=4, children=[
                         html.Div(role, className="person-role"),
                         heading,
                         dmc.Group(gap=6, children=badges) if badges else None,
                         dmc.Text(sub, size="xs", c="dimmed") if sub else None,
                     ]))


def _assists(uid: str) -> list[str]:
    df = data.q(f"""
        SELECT a.known_name AS name
        FROM snap.play_athlete pa
        JOIN snap.dim_athlete a ON a.athlete_id = pa.athlete_id
        WHERE pa.play_uid = {data.lit(uid)} AND pa.role = 'assistedBy'
        ORDER BY pa.ordinal
    """)
    # `if n` is not a null test here: a missing name comes back as NaN, and NaN is
    # truthy, so it survived the filter and then broke str.join.
    return [n for n in df["name"].tolist() if isinstance(n, str) and n]


def _headline(row) -> str:
    kind = row["play_kind"]
    if kind in _CONVERSIONS:
        return f"{row['phase']} · {row['outcome']}"
    if kind == "field_goal":
        d = row["fg_distance_yds"]
        return f"{'?' if _na(d) else int(d)} yd field goal · {row['outcome']}"
    if kind == "punt":
        g = row["punt_gross_yds"]
        return f"{'?' if _na(g) else int(g)} yd punt · {row['outcome']}"
    k = row["kickoff_yds"]
    tag = "Onside kick" if row["onside"] else "Kickoff"
    return f"{'?' if _na(k) else int(k)} yd {tag.lower()} · {row['outcome']}"


def render(uid: str, lg: str, key: str = "st", mode: str = "dark"):
    """Dispatch to the drawer the lens's rows deserve."""
    key = lens.resolve(key)
    if lens.is_scrimmage(key):
        return _render_scrimmage(uid, key, lg, mode)
    return _render_kick(uid, lg, mode)


def _render_kick(uid: str, lg: str, mode: str = "dark"):
    df = data.q(f"""
        SELECT {', '.join(columns.detail_fields("st"))}
        FROM {lens.view("st", lg)} WHERE play_uid = {data.lit(uid)}
    """)
    if df.empty:
        return dmc.Text("That play is no longer in the snapshot.", size="sm"), ""
    row = df.iloc[0]

    def v(key):
        x = row[key]
        return None if _na(x) else x

    # ---------------------------------------------------------------- people
    people = [
        _person(_ROLE[row["play_kind"]], v("player"), athlete_id=v("player_id"),
                conf=v("player_conf"), sub=_prior_line(row, lg), mode=mode,
                lg=row.get("league")),
        _person("Returner", v("returner_name"), athlete_id=v("returner_athlete_id"),
                sub="No profile page — this app profiles the kicking side only",
                mode=mode, lg=row.get("league")),
        _person("Tackler", v("tackler_name"), athlete_id=v("tackler_athlete_id"),
                mode=mode, lg=row.get("league")),
        _person("Got a hand on it", v("blocker_name"), mode=mode, lg=row.get("league")),
        # NFL only. The gamebook names the long snapper on every snapped kick and the
        # holder on every place kick; the college feed names neither, so these two cards
        # simply do not appear on a college row -- _person returns None on an empty name.
        _person("Long snapper", v("snapper_name"), mode=mode, lg=row.get("league")),
        _person("Holder", v("holder_name"), mode=mode, lg=row.get("league")),
    ]
    people = [p for p in people if p is not None]
    assists = _assists(uid)
    if assists:
        people.append(_person("Assisted by", ", ".join(assists), mode=mode))

    # People lead this panel, so when a role is empty say why rather than leaving a
    # gap the reader has to interpret.
    missing_people = []
    if not v("player"):
        missing_people.append(
            f"no {_ROLE[row['play_kind']].lower()} named in the play text")
    elif row["player_name_unparsed"]:
        missing_people.append(
            "the name above still carries the gamebook clock and jersey prefix, so "
            "this kick is not linked to an athlete id — see Known limits §11")
    if row["play_kind"] in ("punt", "kickoff") and not v("returner_name") \
            and row["outcome"] in ("Returned", "Unknown"):
        missing_people.append("no returner named in the play text")
    if row["outcome"] == "Returned" and not v("tackler_name"):
        missing_people.append("no tackler named")

    # ---------------------------------------------------------------- panels
    kick_pairs = {
        "field_goal": [("Attempt distance", _yd(v("fg_distance_yds"))),
                       ("Outcome", row["outcome"]),
                       ("Miss reason", v("miss_reason")),
                       ("Blocked", "yes" if v("blocker_name") else
                        ("yes" if row["outcome"] == "Blocked" else "no")),
                       ("Negated by penalty", _yn(v("negated_by_penalty")))],
        "punt": [("Gross", _yd(v("punt_gross_yds"))), ("Net", _yd(v("punt_net_yds"))),
                 ("Return", _yd(v("return_yds"))), ("Outcome", row["outcome"]),
                 ("Returned for TD", _yn(v("returned_for_td")))],
        "kickoff": [("Kick distance", _yd(v("kickoff_yds"))),
                    ("Return", _yd(v("return_yds"))), ("Outcome", row["outcome"]),
                    ("Onside", _yn(v("onside"))),
                    ("Returned for TD", _yn(v("returned_for_td")))],
        "conversion": [("Outcome", row["outcome"]),
                       ("Converted", _yn(v("converted"))),
                       ("Attempt type", v("two_point_type") or
                        ("kick" if row["play_kind"] == "pat" else None)),
                       ("Kind", row["phase"]),
                       ("Blocked", "yes" if row["outcome"] == "Blocked" else "no")],
    }["conversion" if row["play_kind"] in _CONVERSIONS else row["play_kind"]]

    situation = [
        ("Quarter", v("qtr")), ("Clock", v("clock")),
        ("Down & distance", _down(v("down"), v("dist_to_go"))),
        ("Yards to goal", _yd(v("yards_to_goal"))),
        ("Score margin (kicking team)", _signed(v("score_diff"))),
        ("Score state", v("score_state")),
        ("Clutch", _yn(v("is_clutch"))),
    ]
    environment = [
        ("Date", str(row["game_date"])[:10]),
        ("Season", f"{int(row['season'])} · week {_int(v('week'))} · {row['season_type']}"),
        ("Venue", v("venue_name")),
        ("Location", ", ".join([x for x in (v("venue_city"), v("venue_state")) if x])),
        ("Surface", v("surface")), ("Attendance", _comma(v("attendance"))),
        ("Neutral site", _yn(v("neutral_site"))),
        ("Conference game", _yn(v("conference_game"))),
    ]
    provenance = [
        ("Parse confidence", v("parse_confidence")), ("Source", v("source")),
        ("Play uid", row["play_uid"]), ("ESPN game id", _int(v("game_id"))),
    ]

    unknown_note = None
    if row["outcome_unknown"]:
        unknown_note = ui.note("The play text states no outcome for this kick, so the "
                               "warehouse carries NULL — not false — on returned, "
                               "touchback, fair catch, downed and out of bounds. The result "
                               "is unreadable, not absent: this kick is counted as Unknown "
                               "rather than as one that had no return.", "warn")

    body = dmc.Stack(gap="sm", children=[
        dmc.Group(gap=6, children=[
            ui.pill(row["phase"], mode),
            _outcome_badge(row["outcome"], mode),
            dmc.Text(f"{row['team']} vs {row['opponent']} · {row['site'].lower()}",
                     size="xs", c="dimmed"),
        ]),
        dmc.Group(gap=8, children=[
            dcc.Link(dmc.Button(f"{row['team']} profile", variant="light", size="compact-xs"),
                     href=routes.url(f"/team/{league.resolve(row.get('league'))}/{int(row['team_id'])}"))
            if not _na(row["team_id"]) else None,
            dcc.Link(dmc.Button(f"{row['opponent']} profile", variant="subtle",
                                size="compact-xs"),
                     href=routes.url(f"/team/{league.resolve(row.get('league'))}/{int(row['opp_id'])}"))
            if not _na(row["opp_id"]) else None,
        ]),
        unknown_note,
        dmc.Divider(label="Who was involved", labelPosition="left", mt=4),
        dmc.SimpleGrid(cols={"base": 1, "md": 2}, spacing="xs", children=people),
        ui.note("Not recoverable from this row: " + "; ".join(missing_people),
                "neutral") if missing_people else None,
        dmc.Space(h=4),
        dmc.Accordion(variant="separated", radius="sm", chevronPosition="right",
                      value="kick", children=[
            _item("kick", "The attempt" if row["play_kind"] in _CONVERSIONS
                  else "The kick", _kv_block(kick_pairs)),
            _item("sit", "Situation & leverage", _kv_block(situation)),
            _item("env", "Environment", _kv_block(environment)),
            _item("prov", "Provenance & raw text", dmc.Stack(gap="xs", children=[
                html.Div(row["play_text"] or "— no play text on this row —",
                         className="playtext"),
                _kv_block(provenance),
                ui.note("Every field above except the ESPN-native ones (teams, clock, "
                        "down, score) was derived from that line by scripts/st_parser_cfb.py.",
                        "neutral"),
            ])),
        ]),
    ])
    return body, _headline(row)


# --------------------------------------------------------------------------- scrimmage
# The four id columns on the view are ESPN's structured fields and cover the offense
# well and the defense badly. The bridge is where the rest of the field is: every
# tackler, the assists, who sacked the quarterback, who broke the pass up. It is a
# (play, role, athlete) grain, so it is read here as a lookup rather than joined onto
# the play grid -- joining it would multiply a play by its defenders.
_BRIDGE_ROLES = [
    ("tackler", "Tacklers"), ("assistedBy", "Assisted by"),
    ("sackedBy", "Sacked by"), ("passDefender", "Pass defended by"),
    ("forcedBy", "Forced by"), ("recoverer", "Recovered by"),
    ("fumbler", "Fumbled by"), ("penalized", "Penalty on"),
    ("scorer", "Scored by"), ("returner", "Returned by"),
]


def _bridge_people(uid: str) -> dict[str, list[str]]:
    df = data.q(f"""
        SELECT sa.role, a.known_name AS name
        FROM snap.scrimmage_athlete sa
        JOIN snap.dim_athlete a ON a.athlete_id = sa.athlete_id
        WHERE sa.play_uid = {data.lit(uid)}
        ORDER BY sa.role, sa.ordinal
    """)
    out: dict[str, list[str]] = {}
    for r in df.itertuples():
        if isinstance(r.name, str) and r.name:
            out.setdefault(r.role, []).append(r.name)
    return out


_NO_PROFILE = "No profile page yet — profiles are kicking-side only"


def _render_scrimmage(uid: str, lg: str, key: str, mode: str = "dark"):
    df = data.q(f"""
        SELECT {', '.join(columns.detail_fields(key))}
        FROM {lens.view(key, lg)} WHERE play_uid = {data.lit(uid)}
    """)
    if df.empty:
        return dmc.Text("That play is no longer in the snapshot.", size="sm"), ""
    row = df.iloc[0]

    def v(name):
        x = row[name]
        return None if _na(x) else x

    def pos(name):
        p = v(name)
        return f"{p}" if p else None

    bridge = _bridge_people(uid)

    # ---------------------------------------------------------------- people
    people = [
        _person("Passer", v("passer_name"), sub=pos("passer_position"), mode=mode),
        _person("Rusher", v("rusher_name"), sub=pos("rusher_position"), mode=mode),
        _person("Receiver", v("receiver_name"), sub=pos("receiver_position"),
                mode=mode),
    ]
    for role, label in _BRIDGE_ROLES:
        names = bridge.get(role)
        if not names or role in ("scorer",):
            continue
        people.append(_person(label, ", ".join(names), mode=mode))
    people = [p for p in people if p is not None]

    # The first-tackler column is the one the grid can show and the bridge is the one
    # that is complete. Where they disagree in coverage, say which is which rather
    # than letting a reader assume the play had one tackler.
    missing = []
    if not bridge.get("tackler") and v("player") is None \
            and row["play_kind"] in ("rush", "pass", "sack"):
        missing.append("no tackler named anywhere on this play, in the structured "
                       "field or the participant list")
    if row["play_kind"] == "pass" and v("is_complete") is None:
        missing.append("the feed does not say whether the pass was completed")

    # ---------------------------------------------------------------- panels
    lab = "gained" if key == "off" else "allowed"
    yards = v("yards_gained")
    play_pairs = [
        (f"Yards {lab}", _yd(yards) if yards is not None else "—"),
        ("Outcome", row["outcome"]),
        ("First down", _yn(v("first_down_gained"))),
        ("Complete", _yn(v("is_complete"))),
        ("Touchdown", _yn(v("is_touchdown"))),
        ("Turnover" if key == "off" else "Takeaway", _yn(v("is_turnover"))),
        ("Penalty", _yn(v("is_penalty"))),
        (f"Points {'scored' if key == 'off' else 'to the defense'}",
         _signed(v("points_scored"))),
        ("ESPN play type", v("play_type_espn")),
    ]
    situation = [
        ("Quarter", v("qtr")), ("Clock", v("clock")),
        ("Down & distance", _down(v("down"), v("dist_to_go"))),
        ("Yards to goal", _yd(v("yards_to_goal"))),
        ("Field zone", v("field_zone")),
        ("To-go band", v("distance_bucket")),
        ("Drive", _int(v("drive_number"))),
        (f"Score margin ({lens.SUBJECT[key].lower()})", _signed(v("score_diff"))),
        ("Score state", v("score_state")),
        ("Clutch", _yn(v("is_clutch"))),
        ("Ball at, after the play", _yd(v("end_yards_to_goal"))),
        ("Next down", _down(v("end_down"), v("end_distance"))),
    ]
    environment = [
        ("Date", str(row["game_date"])[:10]),
        ("Season", f"{int(row['season'])} · week {_int(v('week'))} · {row['season_type']}"),
        ("Venue", v("venue_name")),
        ("Location", ", ".join([x for x in (v("venue_city"), v("venue_state")) if x])),
        ("Surface", v("surface")), ("Attendance", _comma(v("attendance"))),
        ("Neutral site", _yn(v("neutral_site"))),
        ("Conference game", _yn(v("conference_game"))),
    ]
    provenance = [
        ("Source", v("source")), ("Play uid", row["play_uid"]),
        ("ESPN game id", _int(v("game_id"))),
    ]

    notes = []
    if row["yards_impossible"]:
        notes.append(ui.note(
            "ESPN's statYardage on this row is outside anything a football field "
            "allows, so the yardage is NULL here rather than clamped to a plausible "
            "number. The raw text below is exactly what the feed said.", "warn"))
    if v("is_turnover") and yards is not None:
        notes.append(ui.note(
            f"On a turnover ESPN puts the *return* distance in statYardage, so the "
            f"{int(yards)} yards above belong to the team that took the ball away, not "
            "to the offense. Every mean-yards figure in this app excludes turnover "
            "rows for that reason.", "warn"))

    body = dmc.Stack(gap="sm", children=[
        dmc.Group(gap=6, children=[
            ui.pill(row["phase"], mode),
            _outcome_badge(row["outcome"], mode, key),
            dmc.Text(f"{row['team']} vs {row['opponent']}"
                     + (f" · {row['site'].lower()}" if v("site") else ""),
                     size="xs", c="dimmed"),
        ]),
        *notes,
        dmc.Divider(label="Who was involved", labelPosition="left", mt=4),
        dmc.SimpleGrid(cols={"base": 1, "md": 2}, spacing="xs", children=people)
        if people else ui.note("The feed names nobody on this play.", "neutral"),
        ui.note("Not recoverable from this row: " + "; ".join(missing), "neutral")
        if missing else None,
        ui.note(_NO_PROFILE + ". The names above come from ESPN athlete ids, so the "
                "pages are a query away rather than a rebuild.", "neutral"),
        dmc.Space(h=4),
        dmc.Accordion(variant="separated", radius="sm", chevronPosition="right",
                      value="play", children=[
            _item("play", "The play", _kv_block(play_pairs)),
            _item("sit", "Situation & leverage", _kv_block(situation)),
            _item("env", "Environment", _kv_block(environment)),
            _item("prov", "Provenance & raw text", dmc.Stack(gap="xs", children=[
                html.Div(row["play_text"] or "— no play text on this row —",
                         className="playtext"),
                _kv_block(provenance),
                ui.note("No parser was involved on this side of the warehouse. Every "
                        "field above is an ESPN structured field -- statYardage, down, "
                        "isTurnover, scoringPlay -- and the text is provenance rather "
                        "than input.", "neutral"),
            ])),
        ]),
    ])

    head = f"{row['phase']}"
    if yards is not None and row["play_kind"] != "penalty":
        head += f" · {int(yards)} yd"
    return body, f"{head} · {row['outcome']}"


def _item(value, label, children):
    return dmc.AccordionItem(value=value, children=[
        dmc.AccordionControl(dmc.Text(label, size="xs", fw=600)),
        dmc.AccordionPanel(children),
    ])


# --------------------------------------------------------------------------- formatters
def _yd(x):
    return None if _na(x) else f"{int(x)} yd"


def _yn(x):
    return None if _na(x) else ("yes" if x else "no")


def _int(x):
    return None if _na(x) else int(x)


def _comma(x):
    return None if _na(x) else f"{int(x):,}"


def _signed(x):
    return None if _na(x) else f"{int(x):+d}"


def _down(d, dist):
    if _na(d):
        return None
    ordinal = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}.get(int(d), f"{int(d)}th")
    return f"{ordinal} & {int(dist)}" if not _na(dist) else ordinal
