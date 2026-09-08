"""Render the cfb / pbp schema as a one-page Crow's Foot ERD (landscape Letter PDF).

    .venv/bin/python scripts/build_erd.py        -> reports/cfb_st_erd.pdf (+ .png proof)

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
DB = "cfb"
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
    missing = [t for t in cols if t not in ENTITIES and "_pre" not in t]
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
    ("IDENTITY & GRAIN", ["play_uid", "source", "game_id", "season", "week", "season_type",
                          "play_kind"]),
    ("GAME SITUATION", ["period", "clock_secs_period", "wallclock_utc", "down", "distance",
                        "yards_to_goal", "kicking_team_id", "receiving_team_id",
                        "is_home_kicking", "score_diff_kicking"]),
    ("KICK MEASURES", ["fg_distance_yds", "fg_made", "punt_gross_yds", "punt_net_yds",
                       "kickoff_yds", "return_yds"]),
    ("OUTCOME FLAGS", ["returned", "touchback", "onside", "fair_catch", "downed",
                       "out_of_bounds", "kick_blocked", "returned_for_td", "converted",
                       "two_point_type", "miss_reason"]),
    ("PARSE & TEXT", ["negated_by_penalty", "kicker_name", "returner_name", "blocker_name",
                      "play_text", "parse_confidence"]),
    ("LINKS & AUDIT", ["kicker_athlete_id", "returner_athlete_id", "tackler_athlete_id",
                       "venue_id", "conference_game", "neutral_site", "loaded_at"]),
]
PLAY_FLOW = [[0, 1, 2], [3, 4, 5]]               # which groups sit in which internal column

ENTITIES = {
    "dim_venue":          dict(x=18,  y=52,  w=132, role="dim",    label="dim_venue"),
    "dim_conference":     dict(x=18,  y=165, w=132, role="dim",    label="dim_conference"),
    "dim_team_season":    dict(x=18,  y=240, w=132, role="dim",    label="dim_team_season"),
    "dim_team":           dict(x=18,  y=335, w=132, role="dim",    label="dim_team"),
    "fact_game":          dict(x=192, y=52,  w=148, role="fact",   label="fact_game"),
    "special_teams_play": dict(x=374, y=52,  w=282, role="fact",   label="special_teams_play"),
    "dim_athlete":        dict(x=704, y=52,  w=196, role="dim",    label="dim_athlete"),
    "play_athlete":       dict(x=704, y=180, w=196, role="bridge", label="play_athlete"),
}

SUBTITLE = {
    "special_teams_play": "grain: one special-teams play",
    "fact_game":          "grain: one game",
    "play_athlete":       "bridge: play x role x athlete",
    "dim_team_season":    "slowly changing (team, season)",
    "dim_team":           "teams appearing on a play",
    "dim_athlete":        "ESPN athlete identity",
    "dim_venue":          "stadium & surface",
    "dim_conference":     "conference lookup",
}

# parent, child, key text, waypoints, child-optional, declared-FK, note
RELS = [
    # straight runs wherever the two edges can share a y; elbows only where a box is in the way
    ("dim_venue", "fact_game", "venue_id", [("R", 0.516), ("L", 0.370)], True, True, ""),
    ("dim_venue", "special_teams_play", "venue_id",
     [("R", 0.839), ("V", 165), ("Y", 250), ("L", None)], True, False, ""),
    ("dim_conference", "dim_team_season", "conference_id",
     [("B", 0.30), ("T", 0.30)], False, False, ""),
    ("dim_team", "dim_team_season", "team_id",
     [("T", 0.72), ("B", 0.72)], False, False, "201 orphan rows"),
    ("dim_team_season", "special_teams_play", "(kicking_team_id, season)",
     [("R", 0.805), ("V", 356), ("Y", 290), ("L", None)], False, False, ""),
    ("dim_team", "special_teams_play", "kicking_team_id",
     [("R", 0.639), ("L", 0.917)], False, False, ""),
    ("dim_team", "special_teams_play", "receiving_team_id",
     [("R", 0.895), ("L", 0.952)], False, False, ""),
    ("dim_team", "fact_game", "home_team_id",
     [("R", 0.128), ("V", 240), ("B", None)], False, False, ""),
    ("dim_team", "fact_game", "away_team_id",
     [("R", 0.384), ("V", 286), ("B", None)], False, False, ""),
    ("fact_game", "special_teams_play", "game_id",
     [("R", 0.524), ("L", 0.199)], False, False, ""),
    ("dim_athlete", "special_teams_play", "kicker_athlete_id",
     [("L", 0.274), ("R", 0.082)], True, False, ""),
    ("dim_athlete", "special_teams_play", "returner_athlete_id",
     [("L", 0.519), ("R", 0.155)], True, False, ""),
    ("dim_athlete", "special_teams_play", "tackler_athlete_id",
     [("L", 0.764), ("R", 0.229)], True, False, ""),
    ("dim_athlete", "play_athlete", "athlete_id", [("B", 0.30), ("T", 0.30)], False, False, ""),
    ("special_teams_play", "play_athlete", "play_uid",
     [("R", 0.727), ("H", 680), ("L", 0.5)], False, False, ""),
    ("dim_team", "dim_athlete", "primary_team_id",
     [("B", 0.30), ("Y", 404), ("V", 925), ("R", 0.5)], True, False, ""),
]


# One line per relationship explaining what it means in practice. The null shares quoted here
# are the measured ones from load_integrity()'s sibling audit, not estimates.
REL_NOTES = {
    ("fact_game", "venue_id"): "the only constraint Postgres enforces",
    ("special_teams_play", "venue_id"): "denormalised off fact_game so venue filters need no join",
    ("dim_team_season", "conference_id"): "conference membership as of that season",
    ("dim_team_season", "team_id"): "dim_team holds only teams seen on a play; this also carries FCS opponents",
    ("special_teams_play", "(kicking_team_id, season)"): "composite: joining on team alone backdates realignment across all 12 seasons",
    ("special_teams_play", "kicking_team_id"): "team executing the kick",
    ("special_teams_play", "receiving_team_id"): "team receiving it",
    ("fact_game", "home_team_id"): "home side",
    ("fact_game", "away_team_id"): "away side",
    ("special_teams_play", "game_id"): "every play belongs to exactly one game",
    ("special_teams_play", "kicker_athlete_id"): "null on 4,097 plays (1.3%) where ESPN names no kicker",
    ("special_teams_play", "returner_athlete_id"): "null on 212,796 plays: most kicks are not returned",
    ("special_teams_play", "tackler_athlete_id"): "null on 279,338 plays: no tackle recorded",
    ("play_athlete", "athlete_id"): "full-fidelity bridge: every role, every participant",
    ("play_athlete", "play_uid"): "one play carries many role/athlete pairs",
    ("dim_athlete", "primary_team_id"): "modal team; null for 8,684 athletes",
}


INTEGRITY = {}          # filled by load_integrity()

# child table, child column(s), parent table, parent column(s) -- used to measure orphans
JOINS = {
    ("fact_game", "venue_id"):                       ("dim_venue", ["venue_id"], ["venue_id"]),
    ("special_teams_play", "venue_id"):              ("dim_venue", ["venue_id"], ["venue_id"]),
    ("dim_team_season", "conference_id"):            ("dim_conference", ["conference_id"], ["conference_id"]),
    ("dim_team_season", "team_id"):                  ("dim_team", ["team_id"], ["team_id"]),
    ("special_teams_play", "(kicking_team_id, season)"): ("dim_team_season", ["kicking_team_id", "season"], ["team_id", "season"]),
    ("special_teams_play", "kicking_team_id"):       ("dim_team", ["kicking_team_id"], ["team_id"]),
    ("special_teams_play", "receiving_team_id"):     ("dim_team", ["receiving_team_id"], ["team_id"]),
    ("fact_game", "home_team_id"):                   ("dim_team", ["home_team_id"], ["team_id"]),
    ("fact_game", "away_team_id"):                   ("dim_team", ["away_team_id"], ["team_id"]),
    ("special_teams_play", "game_id"):               ("fact_game", ["game_id"], ["game_id"]),
    ("special_teams_play", "kicker_athlete_id"):     ("dim_athlete", ["kicker_athlete_id"], ["athlete_id"]),
    ("special_teams_play", "returner_athlete_id"):   ("dim_athlete", ["returner_athlete_id"], ["athlete_id"]),
    ("special_teams_play", "tackler_athlete_id"):    ("dim_athlete", ["tackler_athlete_id"], ["athlete_id"]),
    ("play_athlete", "athlete_id"):                  ("dim_athlete", ["athlete_id"], ["athlete_id"]),
    ("play_athlete", "play_uid"):                    ("special_teams_play", ["play_uid"], ["play_uid"]),
    ("dim_athlete", "primary_team_id"):              ("dim_team", ["primary_team_id"], ["team_id"]),
}


def load_integrity():
    """Count, for every relationship, child rows whose key finds no parent."""
    rows = []
    for parent, child, key, _spec, optional, declared, _note in RELS:
        _p, ccols, pcols = JOINS[(child, key)]
        on = " AND ".join(f"p.{a}=c.{b}" for b, a in zip(ccols, pcols))
        notnull = " AND ".join(f"c.{c} IS NOT NULL" for c in ccols)
        n = int(psql(f"SELECT count(*) FROM pbp.{child} c LEFT JOIN pbp.{parent} p ON {on} "
                     f"WHERE {notnull} AND p.{pcols[0]} IS NULL")[0][0])
        rows.append((parent, child, key, optional, declared, f"{n:,}"))
    INTEGRITY["rows"] = rows
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
    if name != "special_teams_play":
        return [[("col", c) for c in cols[name]]]
    by_name = {c["name"]: c for c in cols[name]}
    out = []
    for group_ids in PLAY_FLOW:
        block = []
        for gi in group_ids:
            title, names = PLAY_GROUPS[gi]
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
        placed = name in ENTITIES
        svg.rect(x + 6, ty - 4.6, 4.5, 4.5,
                 fill=ROLE_FILL[ENTITIES[name]["role"]] if placed else "#cbd5e1", rx=0.8)
        svg.text(x + 14, ty, name + ("" if placed else "  (not drawn)"),
                 size=5.4, fill=INK if placed else MUTED)
        svg.text(x + w - 53, ty, str(len(cols[name])), size=5.4, fill=MUTED, anchor="end")
        svg.text(x + w - 28, ty, f"{counts[name]:,}", size=5.4, fill=INK, anchor="end")
        svg.text(x + w - 4, ty, sizes.get(name, ""), size=5.0, fill=MUTED, anchor="end")


def rel_table(svg, x, y, w, h, rows):
    svg.rect(x, y, w, h, fill=PANEL_BG, stroke=RULE, rx=2.5, sw=0.6)
    svg.text(x + 7, y + 11, "RELATIONSHIP INVENTORY", size=6.4, fill=INK, weight="700", spacing=0.5)
    svg.text(x + w - 7, y + 11, "cardinality and orphan counts measured live against the data",
             size=5.0, fill=MUTED, anchor="end")
    cols = [8, 62, 120, 196, 226, 263, 320]
    heads = ["PARENT (ONE)", "CHILD (MANY)", "JOIN KEY", "OPTIONAL", "ENFORCED",
             "ORPHAN ROWS", "WHAT IT MEANS"]
    ty = y + 21
    for cx, hd in zip(cols, heads):
        svg.text(x + cx, ty, hd, size=4.7, fill=MUTED, weight="700", spacing=0.3)
    svg.line(x + 7, ty + 3, x + w - 7, ty + 3, RULE, 0.5)
    ty += 3
    for parent, child, key, optional, declared, orphans in rows:
        ty += 8.9
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
        svg.text(x + cols[6], ty, REL_NOTES.get((child, key), ""), size=5.0, fill=MUTED)


def build():
    cols, pk, fk, counts, sizes = load_schema()
    unplaced = warn_unplaced(cols)
    load_integrity()
    geos = {}
    for name, g in ENTITIES.items():
        g = dict(g)
        g["h"] = entity_height(name, cols, pk, fk)
        geos[name] = g

    svg = SVG()
    svg.rect(0, 0, W, H, fill="#ffffff")

    # ---- title band
    svg.text(18, 22, "College Football Special Teams Warehouse", size=13.5, fill=INK, weight="700")
    svg.text(18, 32, "Entity Relationship Diagram — Crow's Foot notation · "
                     "PostgreSQL database  cfb  ·  schema  pbp  (the only user schema)",
             size=6.2, fill=MUTED)
    stamp = datetime.date.today().isoformat()
    total_rows = sum(counts.values())
    svg.text(W - 18, 18, f"{len(ENTITIES)} tables · {sum(len(c) for c in cols.values())} columns "
                         f"· {total_rows:,} rows", size=6.2, fill=INK, anchor="end", weight="700")
    svg.text(W - 18, 27, f"seasons 2014–2026 · generated {stamp} from live schema",
             size=5.6, fill=MUTED, anchor="end")
    svg.line(18, 38, W - 18, 38, RULE, 0.7)

    for rel in RELS:
        draw_rel(svg, geos, rel)
    for name in ENTITIES:
        draw_entity(svg, name, cols, pk, fk, counts, geos[name])

    table_inventory(svg, 704, 268, 196, 126, cols, counts, sizes)
    legend(svg, 18, 408, 250, 186)
    rel_table(svg, 284, 408, W - 302, 186, INTEGRITY["rows"])

    svg.text(18, H - 6, "Every relationship above is 1:N. The only constraint Postgres actually "
                        "enforces is fact_game.venue_id → dim_venue; the other fifteen are "
                        "invariants of the build scripts, which is why they are dashed.",
             size=5.0, fill=MUTED)

    body = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{PAGE_W}pt" height="{PAGE_H}pt" '
            f'viewBox="0 0 {PAGE_W} {PAGE_H}">'
            f'<g transform="scale({S:.6f})">{svg.out()}</g></svg>')
    doc = ("<!doctype html><meta charset='utf-8'><style>"
           "@page{size:17in 11in;margin:0}"
           "html,body{margin:0;padding:0;background:#fff}"
           "svg{display:block}</style>" + body)
    os.makedirs(OUT, exist_ok=True)
    # the HTML is only a vehicle for Chrome's PDF writer; keep reports/ to deliverables
    hp = os.path.join(tempfile.gettempdir(), "cfb_st_erd.html")
    with open(hp, "w") as f:
        f.write(doc)
    pdf = os.path.join(OUT, "cfb_st_erd.pdf")
    subprocess.run([CHROME, "--headless", "--disable-gpu", "--no-sandbox",
                    "--no-pdf-header-footer", f"--print-to-pdf={pdf}",
                    "--virtual-time-budget=4000", f"file://{hp}"],
                   capture_output=True, check=True)
    png = os.path.join(OUT, "cfb_st_erd.png")
    subprocess.run([CHROME, "--headless", "--disable-gpu", "--no-sandbox",
                    "--screenshot=" + png, "--window-size=1632,1056",
                    "--force-device-scale-factor=2", "--hide-scrollbars",
                    "--virtual-time-budget=4000", f"file://{hp}"],
                   capture_output=True, check=True)
    print(f"tables={len(ENTITIES)} columns={sum(len(c) for c in cols.values())} rows={total_rows:,}")
    print(pdf)
    print(png)


if __name__ == "__main__":
    build()
