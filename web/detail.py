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

from . import columns, data, theme, ui

_ROLE = {"field_goal": "Placekicker", "punt": "Punter", "kickoff": "Kickoff"}


def _na(x) -> bool:
    """True for None, NaN, NaT and pd.NA alike."""
    return x is None or bool(pd.isna(x))


def _outcome_badge(outcome: str, mode: str):
    hexv = theme.outcome_colors(mode).get(outcome)
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


def _prior_line(row) -> str | None:
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
                       FROM st WHERE {where} AND outcome <> 'Negated'""").iloc[0]
        if not r.n:
            return "first field goal attempt of the season"
        return f"{int(r.made)} of {int(r.n)} on the season before this kick"
    if kind == "punt":
        r = data.q(f"""SELECT count(*) n, avg(punt_gross_yds) g, avg(punt_net_yds) net
                       FROM st WHERE {where}""").iloc[0]
        if not r.n:
            return "first punt of the season"
        net = "" if _na(r.net) else f", {r.net:.1f} net"
        return f"{int(r.n)} punts on the season before this one · {r.g:.1f} gross{net}"
    r = data.q(f"""SELECT count(*) n,
                          sum(CASE WHEN outcome='Touchback' THEN 1 ELSE 0 END) tb
                   FROM st WHERE {where}""").iloc[0]
    if not r.n:
        return "first kickoff of the season"
    return (f"{int(r.n)} kickoffs on the season before this one · "
            f"{int(r.tb)} touchbacks ({r.tb / r.n:.0%})")


def _person(role, name, *, athlete_id=None, conf=None, sub=None, mode="dark"):
    if not name:
        return None
    heading = html.Div(name, className="person-name")
    if not _na(athlete_id):
        heading = dcc.Link(heading, href=f"/player/{int(athlete_id)}",
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
    if kind == "field_goal":
        d = row["fg_distance_yds"]
        return f"{'?' if _na(d) else int(d)} yd field goal · {row['outcome']}"
    if kind == "punt":
        g = row["punt_gross_yds"]
        return f"{'?' if _na(g) else int(g)} yd punt · {row['outcome']}"
    k = row["kickoff_yds"]
    tag = "Onside kick" if row["onside"] else "Kickoff"
    return f"{'?' if _na(k) else int(k)} yd {tag.lower()} · {row['outcome']}"


def render(uid: str, mode: str = "dark"):
    df = data.q(f"""
        SELECT {', '.join(columns.DETAIL_FIELDS)}
        FROM st WHERE play_uid = {data.lit(uid)}
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
                conf=v("player_conf"), sub=_prior_line(row), mode=mode),
        _person("Returner", v("returner_name"), athlete_id=v("returner_athlete_id"),
                sub="No profile page — this app profiles the kicking side only",
                mode=mode),
        _person("Tackler", v("tackler_name"), athlete_id=v("tackler_athlete_id"),
                mode=mode),
        _person("Got a hand on it", v("blocker_name"), mode=mode),
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
            "the name above still carries the gamebook clock and jersey prefix — "
            "st_parser.py handles that dialect for punts and kickoffs but not for "
            "field goals, so this kick is not linked to an athlete id")
    if row["play_kind"] != "field_goal" and not v("returner_name") \
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
    }[row["play_kind"]]

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
                     href=f"/team/{int(row['team_id'])}") if not _na(row["team_id"]) else None,
            dcc.Link(dmc.Button(f"{row['opponent']} profile", variant="subtle",
                                size="compact-xs"),
                     href=f"/team/{int(row['opp_id'])}") if not _na(row["opp_id"]) else None,
        ]),
        unknown_note,
        dmc.Divider(label="Who was involved", labelPosition="left", mt=4),
        dmc.SimpleGrid(cols={"base": 1, "md": 2}, spacing="xs", children=people),
        ui.note("Not recoverable from this row: " + "; ".join(missing_people),
                "neutral") if missing_people else None,
        dmc.Space(h=4),
        dmc.Accordion(variant="separated", radius="sm", chevronPosition="right",
                      value="kick", children=[
            _item("kick", "The kick", _kv_block(kick_pairs)),
            _item("sit", "Situation & leverage", _kv_block(situation)),
            _item("env", "Environment", _kv_block(environment)),
            _item("prov", "Provenance & raw text", dmc.Stack(gap="xs", children=[
                html.Div(row["play_text"] or "— no play text on this row —",
                         className="playtext"),
                _kv_block(provenance),
                ui.note("Every field above except the ESPN-native ones (teams, clock, "
                        "down, score) was derived from that line by scripts/st_parser.py.",
                        "neutral"),
            ])),
        ]),
    ])
    return body, _headline(row)


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
