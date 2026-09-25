"""Render the cfb / pbp schema as a two-sheet Crow's Foot ERD (tabloid landscape PDF).

    .venv/bin/python scripts/build_erd.py   -> reports/pbp_erd.pdf, 2 pages
                                               + a .png proof of each sheet

ONE SHEET PER FACT FAMILY. Eleven tables and two forty-column facts do not fit on one page
legibly, so sheet 1 is special teams and sheet 2 is scrimmage. The four dimensions and
dim_athlete appear on BOTH, in the same position each time -- that repetition is the point,
because the two facts hang off one shared set of dimensions and a reader flipping between
the pages should see them land in the same place.

Each sheet stands alone: both carry the full legend, the whole-schema table inventory (with
a ·1 / ·2 / ·1,2 marker saying which sheets a table is drawn on) and their own relationship
inventory with orphan counts measured live. Sheet 2's relationship panel drops the WHAT IT
MEANS column, because `drive` needs the width sheet 1 gives the legend and a clipped note is
worse than none; sheet 1 carries those explanations.

Adding a table means giving it coordinates in ST_ENTITIES or SCRIM_ENTITIES. Anything in the
schema with no box is named on stdout and listed greyed in the inventory, so the diagram
cannot quietly fall behind the database. A wide table also needs a GROUPS entry, and that is
checked: a column missing from its grouping aborts the render rather than vanishing.

The schema, row counts and referential-integrity numbers are all read live from Postgres,
so the diagram cannot drift from the database the way a hand-drawn one does. Only one
foreign key is actually declared (fact_game.venue_id); every other join in this warehouse is
enforced by the build scripts rather than by a constraint, and the diagram says so with a
dashed line -- that distinction is the single most useful thing on the page.

Geometry is in POINTS so the whole canvas maps 1:1 onto 792x612pt = 11x8.5in landscape, and
a font size in the code is the font size on paper. Chrome headless does the PDF conversion.
"""
import os, re, subprocess, datetime, html, tempfile

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HOME, "reports")
DB = "pbp"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Tabloid landscape, 17x11in. The design space keeps the same 612 vertical units the layout
# was built around and widens to 946 so the page aspect (1.545) is matched exactly; the whole
# canvas is then scaled by S on output. So a font size below is still a real size on paper --
# just multiplied by S. Vertical positions are unchanged from the Letter build.
PAGE_W, PAGE_H = 1224.0, 792.0           # 17in x 11in, in points
W, H = 946.0, 612.0                      # design space
S = PAGE_W / W                           # 1.2939 -- equals PAGE_H / H
ROW_H, HDR_H, PAD = 9.2, 15.0, 5.0
F_COL, F_HDR, F_BADGE = 6.8, 8.5, 4.9

INK        = "#0f172a"
MUTED      = "#64748b"
RULE       = "#cbd5e1"
PANEL_BG   = "#f8fafc"
HARD_LINE  = "#1d4ed8"                   # declared foreign key
SOFT_LINE  = "#94a3b8"                   # build-enforced relationship
WARN       = "#b45309"

ROLE_FILL = {"fact": "#1e3a5f", "bridge": "#0f766e", "dim": "#475569"}
ROLE_TINT = {"fact": "#eff4fa", "bridge": "#eefbf8", "dim": "#f5f7fa"}

TYPE_ABBR = {"bigint": "int8", "integer": "int4", "smallint": "int2", "text": "text",
             "boolean": "bool", "numeric": "num",
             "timestamp with time zone": "tstz"}


def psql(sql):
    r = subprocess.run(["psql", "-d", DB, "-At", "-F", "\x1f", "-c", sql],
                       capture_output=True, text=True, check=True)
    return [ln.split("\x1f") for ln in r.stdout.strip().split("\n") if ln]


def warn_unplaced(cols):
    """Say loudly which tables exist in the schema but have no box on this page.

    The diagram is hand-laid out -- fixed coordinates, hand-routed edges -- so new tables do
    not appear by themselves. After the 2026-09-08 expansion the schema holds three the
    layout has never had room for. They are listed in the inventory panel and named here;
    placing them properly means re-laying out the page, which is a design job.
    """
    # Rollback copies are deliberate, transient and not part of the model. They are named
    # for what they roll back to, so a suffix test is enough.
    missing = [t for t in cols if t not in ALL_PLACED and "_pre" not in t]
    if missing:
        print(f"\n  NOTE: {len(missing)} table(s) in the schema are NOT drawn on the diagram:",
              flush=True)
        for t in sorted(missing):
            print(f"        {t}", flush=True)
        print("        They appear in the inventory panel, greyed, marked '(not drawn)'.\n",
              flush=True)
    return missing


def load_schema():
    cols = {}
    for t, _, name, typ, nullable in psql("""
        select table_name, ordinal_position, column_name, data_type, is_nullable
        from information_schema.columns where table_schema='pbp'
        order by table_name, ordinal_position"""):
        cols.setdefault(t, []).append({"name": name, "type": TYPE_ABBR.get(typ, typ),
                                       "nn": nullable == "NO"})
    pk, fk = {}, {}
    for tbl, kind, definition in psql("""
        select conrelid::regclass::text, contype::text, pg_get_constraintdef(oid)
        from pg_constraint where connamespace='pbp'::regnamespace and contype in ('p','f')"""):
        t = tbl.split(".")[-1]
        inner = re.search(r"\(([^)]*)\)", definition)
        names = [c.strip() for c in inner.group(1).split(",")] if inner else []
        (pk if kind == "p" else fk).setdefault(t, set()).update(names)
    counts = {t: int(n) for t, n in psql("""
        select relname, n_live_tup from pg_stat_user_tables where schemaname='pbp'""")}
    exact = {}
    for t in cols:
        exact[t] = int(psql(f"select count(*) from pbp.{t}")[0][0])
    sizes = {t: s for t, s in psql("""
        select c.relname, pg_size_pretty(pg_total_relation_size(c.oid))
        from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where n.nspname='pbp' and c.relkind='r'""")}
    return cols, pk, fk, exact, sizes


# ---------------------------------------------------------------- entity content
# special_teams_play carries 47 columns; grouping them keeps the hub readable and lets it
# flow into three internal columns instead of one 47-row tower.
PLAY_GROUPS = [
    ("IDENTITY & GRAIN", ["play_uid", "league", "source", "game_id", "season", "week",
                          "season_type", "play_kind"]),
    ("GAME SITUATION", ["period", "clock_secs_period", "wallclock_utc", "down", "distance",
                        "yards_to_goal", "kicking_team_id", "receiving_team_id",
                        "is_home_kicking", "score_diff_kicking"]),
    ("KICK MEASURES", ["fg_distance_yds", "fg_made", "punt_gross_yds", "punt_net_yds",
                       "kickoff_yds", "return_yds"]),
    ("OUTCOME FLAGS", ["returned", "touchback", "onside", "fair_catch", "downed",
                       "out_of_bounds", "kick_blocked", "returned_for_td", "converted",
                       "two_point_type", "miss_reason"]),
    ("PARSE & TEXT", ["negated_by_penalty", "kicker_name", "returner_name", "blocker_name",
                      "snapper_name", "holder_name", "play_text", "parse_confidence"]),
    ("LINKS & AUDIT", ["kicker_athlete_id", "returner_athlete_id", "tackler_athlete_id",
                       "venue_id", "conference_game", "neutral_site", "loaded_at"]),
]
PLAY_FLOW = [[0, 1, 2], [3, 4, 5]]               # which groups sit in which internal column

SCRIM_GROUPS = [
    ("IDENTITY & GRAIN", ["play_uid", "league", "source", "game_id", "season", "week",
                          "season_type", "play_kind", "play_type_espn"]),
    ("DRIVE", ["drive_id", "drive_number"]),
    ("GAME SITUATION", ["period", "clock_secs_period", "wallclock_utc", "down", "distance",
                        "yards_to_goal", "offense_team_id", "defense_team_id",
                        "is_home_offense", "score_diff_offense"]),
    ("OUTCOME", ["yards_gained", "end_down", "end_distance", "end_yards_to_goal",
                 "end_team_id", "first_down_gained", "is_complete", "is_touchdown",
                 "is_turnover", "is_penalty", "is_scoring_play", "points_scored"]),
    ("PEOPLE", ["passer_athlete_id", "rusher_athlete_id", "receiver_athlete_id",
                "tackler_athlete_id"]),
    ("TEXT & AUDIT", ["play_text", "loaded_at", "venue_id", "neutral_site",
                      "conference_game"]),
]
SCRIM_FLOW = [[0, 1, 2], [3, 4, 5]]

DRIVE_GROUPS = [
    ("IDENTITY", ["drive_uid", "league", "drive_id", "game_id", "season", "week",
                  "season_type", "drive_number"]),
    ("TEAMS", ["offense_team_id", "defense_team_id"]),
    ("RESULT", ["result", "display_result", "description", "is_score"]),
    ("COUNTS", ["offensive_plays", "plays_total", "plays_scrimmage", "yards",
                "time_elapsed_secs"]),
    ("START", ["start_period", "start_clock_secs", "start_yards_to_goal", "start_text"]),
    ("END", ["end_period", "end_clock_secs", "end_yards_to_goal", "end_text"]),
]
DRIVE_FLOW = [[0, 1, 2], [3, 4, 5]]

# Only the wide tables are grouped; everything else is one plain column of fields.
GROUPS = {
    "special_teams_play": (PLAY_GROUPS, PLAY_FLOW),
    "scrimmage_play":     (SCRIM_GROUPS, SCRIM_FLOW),
    "drive":              (DRIVE_GROUPS, DRIVE_FLOW),
}

# The four shared dimensions sit in the same place on both sheets, deliberately: the point of
# splitting is that the two facts hang off ONE set of dimensions, and a reader flipping
# between the pages should see them land in the same spot.
SHARED_DIMS = {
    "dim_venue":       dict(x=18, y=52,  w=132, role="dim", label="dim_venue"),
    "dim_conference":  dict(x=18, y=165, w=132, role="dim", label="dim_conference"),
    "dim_team_season": dict(x=18, y=240, w=132, role="dim", label="dim_team_season"),
    "dim_team":        dict(x=18, y=335, w=132, role="dim", label="dim_team"),
}

ST_ENTITIES = {
    **SHARED_DIMS,
    "fact_game":          dict(x=192, y=52,  w=148, role="fact",   label="fact_game"),
    "special_teams_play": dict(x=374, y=52,  w=282, role="fact",   label="special_teams_play"),
    "play_athlete":       dict(x=192, y=200, w=148, role="bridge", label="play_athlete"),
    "dim_athlete":        dict(x=704, y=52,  w=196, role="dim",    label="dim_athlete"),
}

# drive is 26 columns and needs two internal columns to stay legible, which needs ~250pt of
# width. The only place on the page with that much room is the band the legend occupies on
# sheet 1, so sheet 1's full legend is replaced here by a compact one in the bottom right.
SCRIM_ENTITIES = {
    **SHARED_DIMS,
    "fact_game":         dict(x=192, y=52,  w=164, role="fact",   label="fact_game"),
    "scrimmage_athlete": dict(x=192, y=200, w=164, role="bridge", label="scrimmage_athlete"),
    "scrimmage_play":    dict(x=374, y=52,  w=282, role="fact",   label="scrimmage_play"),
    "dim_athlete":       dict(x=704, y=52,  w=196, role="dim",    label="dim_athlete"),
    "drive":             dict(x=18,  y=414, w=250, role="fact",   label="drive"),
}

SUBTITLE = {
    "scrimmage_play":     "grain: one scrimmage play",
    "scrimmage_athlete":  "bridge: play x role x athlete",
    "drive":              "grain: one drive — spans both facts",
    "special_teams_play": "grain: one special-teams play",
    "fact_game":          "grain: one game",
    "play_athlete":       "bridge: play x role x athlete",
    "dim_team_season":    "slowly changing (league, team, season)",
    "dim_team":           "teams appearing on a play, keyed (league, team_id)",
    "dim_athlete":        "ESPN athlete identity",
    "dim_venue":          "stadium & surface",
    "dim_conference":     "conference lookup, keyed (league, conference_id)",
}

# parent, child, key text, waypoints, child-optional, declared-FK, note
ST_RELS = [
    # straight runs wherever the two edges can share a y; elbows only where a box is in the way
    ("dim_venue", "fact_game", "venue_id", [("R", 0.516), ("L", 0.370)], True, True, ""),
    ("dim_venue", "special_teams_play", "venue_id",
     [("R", 0.839), ("V", 165), ("Y", 192), ("L", None)], True, False, ""),
    ("dim_conference", "dim_team_season", "(league, conference_id)",
     [("B", 0.30), ("T", 0.30)], False, False, ""),
    ("dim_team", "dim_team_season", "(league, team_id)",
     [("T", 0.72), ("B", 0.72)], False, False, "201 orphan rows"),
    ("dim_team_season", "special_teams_play", "(league, kicking_team_id, season)",
     [("R", 0.805), ("V", 356), ("Y", 290), ("L", None)], False, False, ""),
    ("dim_team", "special_teams_play", "(league, kicking_team_id)",
     [("R", 0.639), ("L", 0.917)], False, False, ""),
    ("dim_team", "special_teams_play", "(league, receiving_team_id)",
     [("R", 0.895), ("L", 0.952)], False, False, ""),
    ("dim_team", "fact_game", "(league, home_team_id)",
     [("R", 0.128), ("L", 0.80)], False, False, ""),
    ("dim_team", "fact_game", "(league, away_team_id)",
     [("R", 0.384), ("L", 0.92)], False, False, ""),
    ("fact_game", "special_teams_play", "game_id",
     [("R", 0.524), ("L", 0.199)], False, False, ""),
    ("dim_athlete", "special_teams_play", "kicker_athlete_id",
     [("L", 0.274), ("R", 0.082)], True, False, ""),
    ("dim_athlete", "special_teams_play", "returner_athlete_id",
     [("L", 0.519), ("R", 0.155)], True, False, ""),
    ("dim_athlete", "special_teams_play", "tackler_athlete_id",
     [("L", 0.764), ("R", 0.229)], True, False, ""),
    ("dim_athlete", "play_athlete", "athlete_id",
     [("L", 0.930), ("H", 680), ("Y", 352), ("H", 300), ("B", None)], False, False, ""),
    ("special_teams_play", "play_athlete", "play_uid",
     [("L", 0.670), ("R", 0.45)], False, False, ""),
    ("dim_team", "dim_athlete", "primary_team_id",
     [("B", 0.30), ("Y", 412), ("V", 936), ("R", 0.5)], True, False, ""),
]

SCRIM_RELS = [
    ("dim_venue", "fact_game", "venue_id", [("R", 0.516), ("L", 0.370)], True, True, ""),
    ("dim_venue", "scrimmage_play", "venue_id",
     [("R", 0.839), ("V", 165), ("Y", 192), ("L", None)], True, False, ""),
    ("dim_conference", "dim_team_season", "(league, conference_id)",
     [("B", 0.30), ("T", 0.30)], False, False, ""),
    ("dim_team", "dim_team_season", "(league, team_id)",
     [("T", 0.72), ("B", 0.72)], False, False, ""),
    ("dim_team_season", "scrimmage_play", "(league, offense_team_id, season)",
     [("R", 0.805), ("V", 362), ("Y", 296), ("L", None)], False, False, ""),
    ("dim_team", "scrimmage_play", "(league, offense_team_id)",
     [("R", 0.639), ("V", 368), ("Y", 268), ("L", None)], False, False, ""),
    ("dim_team", "scrimmage_play", "(league, defense_team_id)",
     [("R", 0.895), ("V", 370), ("Y", 282), ("L", None)], False, False, ""),
    ("dim_team", "fact_game", "(league, home_team_id)",
     [("R", 0.128), ("L", 0.80)], False, False, ""),
    ("dim_team", "fact_game", "(league, away_team_id)",
     [("R", 0.384), ("L", 0.92)], False, False, ""),
    ("fact_game", "scrimmage_play", "game_id",
     [("R", 0.524), ("L", 0.199)], False, False, ""),
    ("fact_game", "drive", "game_id",
     [("B", 0.25), ("Y", 390), ("H", 143), ("T", None)], False, False, ""),
    ("drive", "scrimmage_play", "drive_id",
     [("R", 0.180), ("H", 340), ("Y", 292), ("L", None)], True, False, ""),
    ("dim_athlete", "scrimmage_play", "passer_athlete_id",
     [("L", 0.216), ("R", 0.062)], True, False, ""),
    ("dim_athlete", "scrimmage_play", "rusher_athlete_id",
     [("L", 0.412), ("R", 0.118)], True, False, ""),
    ("dim_athlete", "scrimmage_play", "receiver_athlete_id",
     [("L", 0.608), ("R", 0.174)], True, False, ""),
    ("dim_athlete", "scrimmage_play", "tackler_athlete_id",
     [("L", 0.804), ("R", 0.230)], True, False, ""),
    ("dim_athlete", "scrimmage_athlete", "athlete_id",
     [("L", 0.930), ("H", 680), ("Y", 330), ("H", 300), ("B", None)], False, False, ""),
    ("scrimmage_play", "scrimmage_athlete", "play_uid",
     [("L", 0.670), ("R", 0.45)], False, False, ""),
    ("dim_team", "dim_athlete", "primary_team_id",
     [("B", 0.30), ("Y", 412), ("V", 936), ("R", 0.5)], True, False, ""),
]



# One line per relationship explaining what it means in practice. The null shares quoted here
# are the measured ones from load_integrity()'s sibling audit, not estimates.
REL_NOTES = {
    ("fact_game", "venue_id"): "the only constraint Postgres enforces",
    ("special_teams_play", "venue_id"): "denormalised off fact_game so venue filters need no join",
    ("dim_team_season", "(league, conference_id)"): "conference membership as of that season",
    ("dim_team_season", "(league, team_id)"): "dim_team holds only teams seen on a play; this also carries FCS opponents",
    ("special_teams_play", "(league, kicking_team_id, season)"): "composite: joining on team alone backdates realignment across all 13 seasons AND mixes the two leagues, whose team ids collide",
    ("special_teams_play", "(league, kicking_team_id)"): "team executing the kick",
    ("special_teams_play", "(league, receiving_team_id)"): "team receiving it",
    ("fact_game", "(league, home_team_id)"): "home side",
    ("fact_game", "(league, away_team_id)"): "away side",
    ("special_teams_play", "game_id"): "every play belongs to exactly one game",
    ("special_teams_play", "kicker_athlete_id"): "null on 4,097 plays (1.3%) where ESPN names no kicker",
    ("special_teams_play", "returner_athlete_id"): "null on 212,796 plays: most kicks are not returned",
    ("special_teams_play", "tackler_athlete_id"): "null on 279,338 plays: no tackle recorded",
    ("play_athlete", "athlete_id"): "full-fidelity bridge: every role, every participant",
    ("play_athlete", "play_uid"): "one play carries many role/athlete pairs",
    ("dim_athlete", "primary_team_id"): "modal team; null for 8,684 athletes",
}


# One sheet per fact family. Both hang off the same four dimensions and the same
# dim_athlete, which is the whole reason the split works: the shared half is drawn twice, in
# the same position, and each sheet is readable on its own.
SHEETS = [
    dict(key="st", title="Special Teams",
         entities=None, rels=None,          # filled below; the dicts are defined further up
         blurb="kickoffs, punts, field goals and the conversion family"),
    dict(key="scrimmage", title="Scrimmage",
         entities=None, rels=None,
         blurb="rushes, passes, sacks, penalties — and the drives that contain both facts"),
]

INTEGRITY = {}          # filled by load_integrity()

# child table, child column(s), parent table, parent column(s) -- used to measure orphans
JOINS = {
    ("fact_game", "venue_id"):                       ("dim_venue", ["venue_id"], ["venue_id"]),
    ("special_teams_play", "venue_id"):              ("dim_venue", ["venue_id"], ["venue_id"]),
    ("dim_team_season", "(league, conference_id)"):            ("dim_conference", ["conference_id"], ["conference_id"]),
    ("dim_team_season", "(league, team_id)"):                  ("dim_team", ["team_id"], ["team_id"]),
    ("special_teams_play", "(league, kicking_team_id, season)"): ("dim_team_season", ["kicking_team_id", "season"], ["team_id", "season"]),
    ("special_teams_play", "(league, kicking_team_id)"):       ("dim_team", ["kicking_team_id"], ["team_id"]),
    ("special_teams_play", "(league, receiving_team_id)"):     ("dim_team", ["receiving_team_id"], ["team_id"]),
    ("fact_game", "(league, home_team_id)"):                   ("dim_team", ["home_team_id"], ["team_id"]),
    ("fact_game", "(league, away_team_id)"):                   ("dim_team", ["away_team_id"], ["team_id"]),
    ("special_teams_play", "game_id"):               ("fact_game", ["game_id"], ["game_id"]),
    ("special_teams_play", "kicker_athlete_id"):     ("dim_athlete", ["kicker_athlete_id"], ["athlete_id"]),
    ("special_teams_play", "returner_athlete_id"):   ("dim_athlete", ["returner_athlete_id"], ["athlete_id"]),
    ("special_teams_play", "tackler_athlete_id"):    ("dim_athlete", ["tackler_athlete_id"], ["athlete_id"]),
    ("play_athlete", "athlete_id"):                  ("dim_athlete", ["athlete_id"], ["athlete_id"]),
    ("play_athlete", "play_uid"):                    ("special_teams_play", ["play_uid"], ["play_uid"]),
    ("dim_athlete", "primary_team_id"):              ("dim_team", ["primary_team_id"], ["team_id"]),
    ("scrimmage_play", "venue_id"):                  ("dim_venue", ["venue_id"], ["venue_id"]),
    ("scrimmage_play", "(league, offense_team_id, season)"): ("dim_team_season", ["offense_team_id", "season"], ["team_id", "season"]),
    ("scrimmage_play", "(league, offense_team_id)"):           ("dim_team", ["offense_team_id"], ["team_id"]),
    ("scrimmage_play", "(league, defense_team_id)"):           ("dim_team", ["defense_team_id"], ["team_id"]),
    ("scrimmage_play", "game_id"):                   ("fact_game", ["game_id"], ["game_id"]),
    ("scrimmage_play", "drive_id"):                  ("drive", ["drive_id"], ["drive_id"]),
    ("scrimmage_play", "passer_athlete_id"):         ("dim_athlete", ["passer_athlete_id"], ["athlete_id"]),
    ("scrimmage_play", "rusher_athlete_id"):         ("dim_athlete", ["rusher_athlete_id"], ["athlete_id"]),
    ("scrimmage_play", "receiver_athlete_id"):       ("dim_athlete", ["receiver_athlete_id"], ["athlete_id"]),
    ("scrimmage_play", "tackler_athlete_id"):        ("dim_athlete", ["tackler_athlete_id"], ["athlete_id"]),
    ("scrimmage_athlete", "athlete_id"):             ("dim_athlete", ["athlete_id"], ["athlete_id"]),
    ("scrimmage_athlete", "play_uid"):               ("scrimmage_play", ["play_uid"], ["play_uid"]),
    ("drive", "game_id"):                            ("fact_game", ["game_id"], ["game_id"]),
}


SHEETS[0]["entities"], SHEETS[0]["rels"] = ST_ENTITIES, ST_RELS
SHEETS[1]["entities"], SHEETS[1]["rels"] = SCRIM_ENTITIES, SCRIM_RELS
ALL_PLACED = set(ST_ENTITIES) | set(SCRIM_ENTITIES)


def load_integrity(rels):
    """Count, for every relationship, child rows whose key finds no parent."""
    rows = []
    for parent, child, key, _spec, optional, declared, _note in rels:
        _p, ccols, pcols = JOINS[(child, key)]
        on = " AND ".join(f"p.{a}=c.{b}" for b, a in zip(ccols, pcols))
        notnull = " AND ".join(f"c.{c} IS NOT NULL" for c in ccols)
        n = int(psql(f"SELECT count(*) FROM pbp.{child} c LEFT JOIN pbp.{parent} p ON {on} "
                     f"WHERE {notnull} AND p.{pcols[0]} IS NULL")[0][0])
        rows.append((parent, child, key, optional, declared, f"{n:,}"))
    return rows


def esc(s):
    return html.escape(str(s), quote=True)


class SVG:
    def __init__(self):
        self.p = []

    def add(self, s):
        self.p.append(s)

    def rect(self, x, y, w, h, fill="none", stroke="none", rx=0, sw=0.5, extra=""):
        self.add(f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" rx="{rx}" '
                 f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}" {extra}/>')

    def text(self, x, y, s, size=6.2, fill=INK, anchor="start", weight="400",
             family="Helvetica Neue, Arial, sans-serif", spacing=0, style=""):
        self.add(f'<text x="{x:.2f}" y="{y:.2f}" font-size="{size}" fill="{fill}" '
                 f'text-anchor="{anchor}" font-weight="{weight}" font-family="{family}" '
                 f'letter-spacing="{spacing}" {style}>{esc(s)}</text>')

    def line(self, x1, y1, x2, y2, stroke=RULE, sw=0.5, dash=""):
        d = f'stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
                 f'stroke="{stroke}" stroke-width="{sw}" {d}/>')

    def path(self, d, stroke, sw=0.6, dash="", fill="none"):
        da = f'stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}" '
                 f'stroke-linejoin="round" stroke-linecap="round" {da}/>')

    def circle(self, cx, cy, r, fill="#ffffff", stroke=INK, sw=0.6):
        self.add(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r}" fill="{fill}" '
                 f'stroke="{stroke}" stroke-width="{sw}"/>')

    def out(self):
        return "\n".join(self.p)


def entity_rows(name, cols, pk, fk):
    """Return the per-internal-column row lists: [(kind, text, col)] where kind is hdr/col."""
    if name not in GROUPS:
        return [[("col", c) for c in cols[name]]]
    groups, flow = GROUPS[name]
    by_name = {c["name"]: c for c in cols[name]}
    # A grouping that has drifted from the table would silently drop columns off the diagram,
    # which is the one failure an ERD must not have.
    listed = {n for _t, names in groups for n in names}
    missing = [c["name"] for c in cols[name] if c["name"] not in listed]
    if missing:
        raise SystemExit(f"build_erd: {name} has columns absent from its GROUPS layout: "
                         f"{', '.join(missing)}. Add them before rendering.")
    out = []
    for group_ids in flow:
        block = []
        for gi in group_ids:
            title, names = groups[gi]
            block.append(("hdr", title))
            block += [("col", by_name[n]) for n in names]
        out.append(block)
    return out


def entity_height(name, cols, pk, fk):
    blocks = entity_rows(name, cols, pk, fk)
    return HDR_H + 8.5 + max(len(b) for b in blocks) * ROW_H + PAD


def draw_entity(svg, name, cols, pk, fk, counts, geo):
    x, y, w = geo["x"], geo["y"], geo["w"]
    h = geo["h"]
    role = geo["role"]
    fill = ROLE_FILL[role]

    svg.add(f'<g><rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" rx="2.5" '
            f'fill="#ffffff" stroke="{RULE}" stroke-width="0.7"/>')
    # header
    svg.add(f'<path d="M{x:.2f},{y+2.5:.2f} a2.5,2.5 0 0 1 2.5,-2.5 h{w-5:.2f} '
            f'a2.5,2.5 0 0 1 2.5,2.5 v{HDR_H-2.5:.2f} h{-w:.2f} z" fill="{fill}"/>')
    svg.text(x + 4, y + 9.6, geo["label"], size=F_HDR, fill="#ffffff", weight="700", spacing=0.1)
    svg.text(x + w - 4, y + 9.6, f'{counts[name]:,}', size=5.4, fill="#c7d5e6", anchor="end")
    # subtitle strip
    svg.rect(x + 0.4, y + HDR_H, w - 0.8, 8.5, fill=ROLE_TINT[role])
    svg.text(x + 4, y + HDR_H + 6, SUBTITLE.get(name, ""), size=5.0, fill=MUTED, style='font-style="italic"')
    svg.line(x, y + HDR_H + 8.5, x + w, y + HDR_H + 8.5, RULE, 0.4)

    blocks = entity_rows(name, cols, pk, fk)
    cw = (w - 4) / len(blocks)
    pks, fks = pk.get(name, set()), fk.get(name, set())
    for bi, block in enumerate(blocks):
        cx = x + 2 + bi * cw
        ry = y + HDR_H + 8.5
        if bi:
            svg.line(cx - 1, y + HDR_H + 9, cx - 1, y + h - 2, RULE, 0.35)
        for kind, item in block:
            if kind == "hdr":
                svg.text(cx + 2, ry + 6.2, item, size=4.6, fill=fill, weight="700", spacing=0.35)
                ry += ROW_H
                continue
            cname, ctype, nn = item["name"], item["type"], item["nn"]
            is_pk, is_fk = cname in pks, cname in LOGICAL_KEYS.get(name, set()) or cname in fks
            badge = ("PK,FK" if is_fk else "PK") if is_pk else ("FK" if is_fk else "")
            if badge:
                bcol = fill if is_pk else (HARD_LINE if cname in fks else MUTED)
                svg.text(cx + 2, ry + 6.1, badge, size=4.2 if badge == "PK,FK" else F_BADGE,
                         fill=bcol, weight="700")
            tx = cx + 17
            svg.text(tx, ry + 6.1, cname, size=F_COL, fill=INK,
                     weight="700" if is_pk else "400")
            svg.text(cx + cw - 3, ry + 6.1, ctype, size=5.0, fill=MUTED, anchor="end")
            if nn and not is_pk:
                svg.text(cx + 9.5, ry + 6.1, "•", size=5.5, fill=MUTED)
            ry += ROW_H
    svg.add("</g>")


# columns that behave as foreign keys but carry no constraint
LOGICAL_KEYS = {
    "special_teams_play": {"game_id", "venue_id", "kicking_team_id", "receiving_team_id",
                           "kicker_athlete_id", "returner_athlete_id", "tackler_athlete_id"},
    "play_athlete": {"play_uid", "athlete_id"},
    "scrimmage_play": {"game_id", "venue_id", "drive_id", "offense_team_id",
                       "defense_team_id", "end_team_id", "passer_athlete_id",
                       "rusher_athlete_id", "receiver_athlete_id", "tackler_athlete_id"},
    "scrimmage_athlete": {"play_uid", "athlete_id"},
    "drive": {"game_id", "offense_team_id", "defense_team_id"},
    "dim_team_season": {"team_id", "conference_id"},
    "dim_athlete": {"primary_team_id"},
    "fact_game": {"home_team_id", "away_team_id"},
}


def anchor(geo, side, frac):
    x, y, w, h = geo["x"], geo["y"], geo["w"], geo["h"]
    if side == "L":
        return (x, y + h * frac, -1, 0)
    if side == "R":
        return (x + w, y + h * frac, 1, 0)
    if side == "T":
        return (x + w * frac, y, 0, -1)
    return (x + w * frac, y + h, 0, 1)


def crow(svg, x, y, dx, dy, many, optional, colour):
    px, py = -dy, dx
    base = 7.0
    if many:
        ax, ay = x + dx * base, y + dy * base
        for s in (-1, 0, 1):
            svg.path(f"M{ax:.2f},{ay:.2f} L{x + px*4.0*s:.2f},{y + py*4.0*s:.2f}", colour, 0.6)
    else:
        cx, cy = x + dx * base, y + dy * base
        svg.path(f"M{cx + px*4:.2f},{cy + py*4:.2f} L{cx - px*4:.2f},{cy - py*4:.2f}", colour, 0.7)
    if optional:
        svg.circle(x + dx * (base + 5), y + dy * (base + 5), 2.2, "#ffffff", colour, 0.6)


def route(parent_geo, child_geo, spec):
    """Build an orthogonal polyline from a compact waypoint spec."""
    (pside, pfrac), (cside, cfrac) = spec[0], spec[-1]
    px, py, pdx, pdy = anchor(parent_geo, pside, pfrac if pfrac is not None else 0.5)
    mids = spec[1:-1]
    # a child anchor with frac None derives its position from the last routing coordinate
    cy_hint = None
    cx_hint = None
    for kind, val in mids:
        if kind == "Y":
            cy_hint = val
        if kind == "H":
            cx_hint = val
    if cfrac is None:
        if cside in ("L", "R") and cy_hint is not None:
            frac = (cy_hint - child_geo["y"]) / child_geo["h"]
        elif cside in ("T", "B") and cx_hint is not None:
            frac = (cx_hint - child_geo["x"]) / child_geo["w"]
        else:
            frac = 0.5
    else:
        frac = cfrac
    cx, cy, cdx, cdy = anchor(child_geo, cside, frac)

    pts = [(px, py)]
    curx, cury = px, py
    for kind, val in mids:
        if kind == "V":                    # travel horizontally to x=val
            curx = val
        elif kind == "Y":                  # travel vertically to y=val
            cury = val
        elif kind == "H":                  # travel horizontally to x=val (alias, clearer intent)
            curx = val
        pts.append((curx, cury))
    # final approach: square up onto the child anchor
    if cdx:                                 # entering a vertical edge -> match y first
        if abs(cury - cy) > 0.01:
            pts.append((curx, cy))
        pts.append((cx, cy))
    else:                                   # entering a horizontal edge -> match x first
        if abs(curx - cx) > 0.01:
            pts.append((cx, cury))
        pts.append((cx, cy))
    # de-duplicate
    clean = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - clean[-1][0]) > 0.01 or abs(p[1] - clean[-1][1]) > 0.01:
            clean.append(p)
    return clean, (px, py, pdx, pdy), (cx, cy, cdx, cdy)


def draw_rel(svg, geos, rel):
    parent, child, key, spec, optional, declared, note = rel
    colour = HARD_LINE if declared else SOFT_LINE
    dash = "" if declared else "2.4 1.8"
    pts, (px, py, pdx, pdy), (cx, cy, cdx, cdy) = route(geos[parent], geos[child], spec)
    d = f"M{pts[0][0]:.2f},{pts[0][1]:.2f} " + " ".join(f"L{a:.2f},{b:.2f}" for a, b in pts[1:])
    svg.path(d, colour, 0.85 if declared else 0.6, dash)
    crow(svg, px, py, pdx, pdy, False, False, colour)          # parent side: exactly one
    crow(svg, cx, cy, cdx, cdy, True, optional, colour)        # child side: many
    # label at the longest horizontal run
    best, bl = None, 0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        if abs(y1 - y2) < 0.01 and abs(x2 - x1) > bl:
            bl, best = abs(x2 - x1), ((x1 + x2) / 2, y1)
    tw = len(key) * 2.35 + 4
    if best and bl > 18 and tw <= bl + 6:
        svg.rect(best[0] - tw / 2, best[1] - 4.4, tw, 6.2, fill="#ffffff", rx=1)
        svg.text(best[0], best[1] + 0.5, key, size=4.7, fill=MUTED, anchor="middle")
    if note and best:
        svg.text(best[0], best[1] + 7.2, note, size=4.4, fill=WARN, anchor="middle")


def legend(svg, x, y, w, h):
    svg.rect(x, y, w, h, fill=PANEL_BG, stroke=RULE, rx=2.5, sw=0.6)
    svg.text(x + 7, y + 11, "HOW TO READ THIS DIAGRAM", size=6.4, fill=INK, weight="700", spacing=0.5)
    svg.line(x + 7, y + 14.5, x + w - 7, y + 14.5, RULE, 0.5)

    cy = y + 24
    svg.text(x + 7, cy, "CARDINALITY (CROW'S FOOT)", size=5.0, fill=MUTED, weight="700", spacing=0.3)
    for many, opt, lab in [(False, False, "exactly one"), (True, False, "one or many"),
                           (True, True, "zero or many")]:
        cy += 11
        svg.line(x + 16, cy, x + 46, cy, INK, 0.6)
        crow(svg, x + 46, cy, -1, 0, many, opt, INK)
        svg.text(x + 58, cy + 2, lab, size=5.4, fill=INK)

    cy += 15
    svg.text(x + 7, cy, "LINE STYLE", size=5.0, fill=MUTED, weight="700", spacing=0.3)
    cy += 9
    svg.line(x + 16, cy, x + 46, cy, HARD_LINE, 0.85)
    svg.text(x + 58, cy + 2, "declared FOREIGN KEY (1 of 16)", size=5.4, fill=INK)
    cy += 10
    svg.line(x + 16, cy, x + 46, cy, SOFT_LINE, 0.6, "2.4 1.8")
    svg.text(x + 58, cy + 2, "enforced by the build, not Postgres", size=5.4, fill=INK)

    cy += 15
    svg.text(x + 7, cy, "COLUMN MARKS", size=5.0, fill=MUTED, weight="700", spacing=0.3)
    for lab, desc, col, sz in [("PK", "primary key", ROLE_FILL["dim"], 5.2),
                               ("PK,FK", "primary key that is also a foreign key", ROLE_FILL["dim"], 4.2),
                               ("FK", "references another table", MUTED, 5.2),
                               ("\u2022", "NOT NULL", MUTED, 5.2)]:
        cy += 8.6
        svg.text(x + 16, cy + 2, lab, size=sz, fill=col, weight="700")
        svg.text(x + 40, cy + 2, desc, size=5.4, fill=INK)

    cy += 13
    svg.text(x + 7, cy, "TABLE ROLE", size=5.0, fill=MUTED, weight="700", spacing=0.3)
    for role, lab in [("fact", "fact"), ("bridge", "bridge"), ("dim", "dimension")]:
        cy += 8.6
        svg.rect(x + 16, cy - 1.6, 9, 5, fill=ROLE_FILL[role], rx=1)
        svg.text(x + 30, cy + 2.6, lab, size=5.4, fill=INK)


def table_inventory(svg, x, y, w, h, cols, counts, sizes):
    svg.rect(x, y, w, h, fill=PANEL_BG, stroke=RULE, rx=2.5, sw=0.6)
    svg.text(x + 6, y + 10.5, "TABLE INVENTORY", size=6.0, fill=INK, weight="700", spacing=0.45)
    svg.line(x + 6, y + 13.5, x + w - 6, y + 13.5, RULE, 0.5)
    svg.text(x + 6, y + 21, "TABLE", size=4.5, fill=MUTED, weight="700", spacing=0.3)
    svg.text(x + w - 53, y + 21, "COLS", size=4.5, fill=MUTED, weight="700", anchor="end", spacing=0.3)
    svg.text(x + w - 28, y + 21, "ROWS", size=4.5, fill=MUTED, weight="700", anchor="end", spacing=0.3)
    svg.text(x + w - 4, y + 21, "SIZE", size=4.5, fill=MUTED, weight="700", anchor="end", spacing=0.3)
    ty = y + 22
    # ENTITIES is a hand-laid-out diagram, so a table nobody has placed on the page cannot be
    # drawn. It still gets a row here, in grey, rather than being dropped: an inventory that
    # silently omits a table is worse than a cramped one.
    for name in sorted(cols, key=lambda t: -counts[t]):
        ty += 11.2
        placed = name in ALL_PLACED
        role = (ST_ENTITIES.get(name) or SCRIM_ENTITIES.get(name) or {}).get("role")
        svg.rect(x + 6, ty - 4.6, 4.5, 4.5,
                 fill=ROLE_FILL[role] if placed else "#cbd5e1", rx=0.8)
        sheet = "1" if name in ST_ENTITIES else ("2" if name in SCRIM_ENTITIES else "")
        both = "1,2" if name in ST_ENTITIES and name in SCRIM_ENTITIES else sheet
        # "(rollback)" and "(not drawn)" mean different things: the first is deliberate and
        # transient, the second means the layout has fallen behind the schema.
        tag = f"  ·{both}" if placed else ("  (rollback)" if "_pre" in name else "  (not drawn)")
        svg.text(x + 14, ty, name + tag, size=5.4, fill=INK if placed else MUTED)
        svg.text(x + w - 53, ty, str(len(cols[name])), size=5.4, fill=MUTED, anchor="end")
        svg.text(x + w - 28, ty, f"{counts[name]:,}", size=5.4, fill=INK, anchor="end")
        svg.text(x + w - 4, ty, sizes.get(name, ""), size=5.0, fill=MUTED, anchor="end")


def rel_table(svg, x, y, w, h, rows, notes=True):
    svg.rect(x, y, w, h, fill=PANEL_BG, stroke=RULE, rx=2.5, sw=0.6)
    svg.text(x + 7, y + 11, "RELATIONSHIP INVENTORY", size=6.4, fill=INK, weight="700", spacing=0.5)
    svg.text(x + w - 7, y + 11, "cardinality and orphan counts measured live against the data",
             size=5.0, fill=MUTED, anchor="end")
    cols = [8, 62, 120, 196, 226, 263, 320]
    heads = ["PARENT (ONE)", "CHILD (MANY)", "JOIN KEY", "OPTIONAL", "ENFORCED",
             "ORPHAN ROWS", "WHAT IT MEANS"]
    if not notes:                       # a narrow panel would clip the note mid-word
        cols, heads = cols[:6], heads[:6]
    ty = y + 21
    for cx, hd in zip(cols, heads):
        svg.text(x + cx, ty, hd, size=4.7, fill=MUTED, weight="700", spacing=0.3)
    svg.line(x + 7, ty + 3, x + w - 7, ty + 3, RULE, 0.5)
    ty += 3
    # Sheet 2 carries 20 relationships to sheet 1's 16. Tighten the pitch to fit rather than
    # letting the last rows fall out of the panel and land on the footer.
    pitch = min(8.9, (y + h - 8 - ty) / max(len(rows), 1))
    for parent, child, key, optional, declared, orphans in rows:
        ty += pitch
        svg.text(x + cols[0], ty, parent, size=5.2, fill=INK)
        svg.text(x + cols[1], ty, child, size=5.2, fill=INK)
        svg.text(x + cols[2], ty, key, size=5.2, fill=MUTED)
        svg.text(x + cols[3], ty, "yes" if optional else "no", size=5.2, fill=MUTED)
        svg.text(x + cols[4], ty, "FK" if declared else "build", size=5.2,
                 fill=HARD_LINE if declared else MUTED,
                 weight="700" if declared else "400")
        svg.text(x + cols[5], ty, orphans, size=5.2,
                 fill=WARN if orphans not in ("0", "—") else MUTED,
                 weight="700" if orphans not in ("0", "—") else "400")
        if notes:
            svg.text(x + cols[6], ty, REL_NOTES.get((child, key), ""), size=5.0, fill=MUTED)


def render_sheet(sheet, cols, pk, fk, counts, sizes, page_no, n_pages, unplaced):
    """One page of SVG for one fact family."""
    entities, rels = sheet["entities"], sheet["rels"]
    integrity = load_integrity(rels)
    geos = {}
    for name, g in entities.items():
        g = dict(g)
        g["h"] = entity_height(name, cols, pk, fk)
        geos[name] = g

    svg = SVG()
    svg.rect(0, 0, W, H, fill="#ffffff")

    # ---- title band
    svg.text(18, 22, f"Play-by-Play Warehouse — {sheet['title']}",
             size=13.5, fill=INK, weight="700")
    svg.text(18, 32, f"Entity Relationship Diagram — Crow's Foot notation · "
                     f"PostgreSQL database  pbp  ·  schema  pbp  ·  {sheet['blurb']}",
             size=6.2, fill=MUTED)
    stamp = datetime.date.today().isoformat()
    shown = sum(counts[t] for t in entities)
    svg.text(W - 18, 18, f"sheet {page_no} of {n_pages} · {len(entities)} tables · "
                         f"{sum(len(cols[t]) for t in entities)} columns · {shown:,} rows",
             size=6.2, fill=INK, anchor="end", weight="700")
    svg.text(W - 18, 27, f"college football + NFL · seasons 2014–2026 · "
                         f"generated {stamp} from live schema",
             size=5.6, fill=MUTED, anchor="end")
    svg.line(18, 38, W - 18, 38, RULE, 0.7)

    for rel in rels:
        draw_rel(svg, geos, rel)
    for name in entities:
        draw_entity(svg, name, cols, pk, fk, counts, geos[name])

    if sheet["key"] == "st":
        # y=262, not 216: dim_athlete gained `leagues`, `nfl_plays` and the four identity
        # fields the NFL endpoint carries, and grew into where this panel used to start.
        table_inventory(svg, 704, 262, 196, 190, cols, counts, sizes)
        legend(svg, 18, 414, 250, 184)
        rel_table(svg, 284, 414, W - 302, 184, integrity)
        foot = ("Every relationship above is 1:N. The only constraint Postgres actually "
                "enforces is fact_game.venue_id → dim_venue; the rest are invariants of the "
                "build scripts, which is why they are dashed. Sheet 2 carries the scrimmage "
                "fact, which hangs off the same four dimensions.")
    else:
        # y=262, not 216: dim_athlete gained `leagues`, `nfl_plays` and the four identity
        # fields the NFL endpoint carries, and grew into where this panel used to start.
        table_inventory(svg, 704, 262, 196, 190, cols, counts, sizes)
        # drive takes the width sheet 1 gives the legend, so the relationship panel loses its
        # WHAT IT MEANS column rather than clipping it; sheet 1 carries those explanations.
        rel_table(svg, 280, 414, 372, 184, integrity, notes=False)
        legend(svg, 664, 414, 264, 184)
        foot = ("The four dimensions on the left and dim_athlete are the SAME tables as on "
                "sheet 1, drawn in the same place — one warehouse, two facts. play_uid is "
                "unique across both facts, so they UNION cleanly; drive spans them, which is "
                "why plays_total counts kicks that live in special_teams_play.")
    svg.text(18, H - 6, foot, size=5.0, fill=MUTED)
    return svg.out()


def build():
    cols, pk, fk, counts, sizes = load_schema()
    warn_unplaced(cols)

    pages = []
    for i, sheet in enumerate(SHEETS, start=1):
        body = render_sheet(sheet, cols, pk, fk, counts, sizes, i, len(SHEETS), None)
        pages.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{PAGE_W}pt" '
                     f'height="{PAGE_H}pt" viewBox="0 0 {PAGE_W} {PAGE_H}">'
                     f'<g transform="scale({S:.6f})">{body}</g></svg>')

    style = ("<!doctype html><meta charset='utf-8'><style>"
             "@page{size:17in 11in;margin:0}"
             "html,body{margin:0;padding:0;background:#fff}"
             "svg{display:block}"
             ".pg{break-after:page;page-break-after:always}"
             ".pg:last-child{break-after:auto;page-break-after:auto}</style>")
    os.makedirs(OUT, exist_ok=True)
    tmp = tempfile.gettempdir()

    # one PDF, one page per sheet
    both = os.path.join(tmp, "pbp_erd.html")
    with open(both, "w") as f:
        f.write(style + "".join(f'<div class="pg">{p}</div>' for p in pages))
    pdf = os.path.join(OUT, "pbp_erd.pdf")
    subprocess.run([CHROME, "--headless", "--disable-gpu", "--no-sandbox",
                    "--no-pdf-header-footer", f"--print-to-pdf={pdf}",
                    "--virtual-time-budget=6000", f"file://{both}"],
                   capture_output=True, check=True)

    # and one PNG proof per sheet, because a 2-page PDF is awkward to eyeball
    pngs = []
    for sheet, page in zip(SHEETS, pages):
        hp = os.path.join(tmp, f"pbp_erd_{sheet['key']}.html")
        with open(hp, "w") as f:
            f.write(style + page)
        png = os.path.join(OUT, f"pbp_erd_{sheet['key']}.png")
        subprocess.run([CHROME, "--headless", "--disable-gpu", "--no-sandbox",
                        "--screenshot=" + png, "--window-size=1632,1056",
                        "--force-device-scale-factor=2", "--hide-scrollbars",
                        "--virtual-time-budget=4000", f"file://{hp}"],
                       capture_output=True, check=True)
        pngs.append(png)

    total = sum(counts.values())
    print(f"\n{len(SHEETS)} sheets · {len(cols)} tables in schema · "
          f"{sum(len(c) for c in cols.values())} columns · {total:,} rows")
    print(pdf)
    for x in pngs:
        print(x)


if __name__ == "__main__":
    build()
