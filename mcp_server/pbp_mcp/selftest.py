"""Live checks against the real warehouse. Run after ANY change to this server.

    ./.venv/bin/python -m pbp_mcp.selftest

Three kinds of check, and the second is the one that matters most:

1. **Every tool returns plausible data.** Shape, not just absence of an error.
2. **The numbers the README says are true, are true.** Field goal percentage,
   yards per carry, the monotonic FG-by-distance curve, and the two kickoff rule
   changes. These are not arbitrary: a distance field that was silently wrong
   would not produce either field-goal curve, and a touchback flag reading the
   wrong thing would not step on the exact season each league changed its rule.
   They are the cheapest available proof that the load landed correctly.
3. **The guard rejects what it should, and the database refuses writes even when
   the guard is bypassed.** The last one is deliberate: it proves the role is the
   real protection rather than the parser.
"""
from __future__ import annotations

import sys
import traceback

from . import config, db, guard, queries as q, server

_passed = 0
_failed: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global _passed
    if condition:
        _passed += 1
    else:
        _failed.append(f"{label}{(' -- ' + detail) if detail else ''}")
        print(f"  FAIL  {label}{(' -- ' + detail) if detail else ''}")


def section(name: str) -> None:
    print(f"\n--- {name}")


def approx(value, low, high) -> bool:
    try:
        return value is not None and low <= float(value) <= high
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- coverage

def test_coverage() -> None:
    section("coverage")
    cov = server.data_coverage()
    check("data_coverage names this corpus", cov.get("league") == config.LEAGUE,
          str(cov.get("league")))
    totals = cov["totals"]

    # FLOORS, not exact counts. README.md's corpus table was written at a point in
    # time and 2026 is still being played, so these only ever grow. An exact
    # assertion here would fail every week for the right reason, which is how a
    # suite stops being read. The exact-match check that matters is
    # split-vs-system-of-record reconciliation, and that lives in
    # scripts/verify_split.py -- this server cannot see the pbp schema at all.
    FLOORS = {
        "cfb": {"kick_plays": 316_397, "scrimmage_plays": 1_510_679},
        "nfl": {"kick_plays":  90_819, "scrimmage_plays":   447_635},
    }[config.LEAGUE]
    for field, floor in FLOORS.items():
        got = totals[field]
        check(f"{field} >= {floor:,} (README floor)", got >= floor, str(got))
        # A corpus that has somehow tripled is a double-load, the other way this
        # can go wrong and be silent.
        check(f"{field} not implausibly large", got < floor * 1.5, str(got))

    check("window starts 2014", totals["first_season"] == 2014, str(totals["first_season"]))
    check("thirteen seasons present", len(cov["seasons"]) >= 13, str(len(cov["seasons"])))

    # THE SPLIT'S DEFINING PROPERTY, asserted rather than assumed: this server can
    # see one corpus and only one. If the other league's rows were reachable the
    # counts above would be roughly 4x (cfb) or 0.25x (nfl) off, but a direct check
    # states the intent.
    # One role holds SELECT on both schemas -- see setup_role_pbp.sql for why two
    # roles would have been two credentials for no isolation between equally trusted
    # servers. What keeps the corpora apart is (a) search_path pinned to one schema,
    # so an unqualified name cannot resolve to the other, and (b) the guard's
    # allow-list, built from that one schema. Assert BOTH, since (a) alone would let
    # a fully-qualified name through.
    other = "nfl" if config.LEAGUE == "cfb" else "cfb"
    check("search_path names exactly this corpus",
          db.query("SELECT current_schema() AS s")[0]["s"] == config.SCHEMA)
    refused = server.run_sql(f"SELECT count(*) FROM {other}.play_wide")
    check(f"run_sql refuses the {other} corpus by name",
          refused.get("rejected") is True, str(refused)[:120])


# --------------------------------------------------------------------------- discovery

def test_discovery() -> None:
    section("discovery")
    HOME = {"cfb": ("Alabama", "Auburn Tigers"), "nfl": ("Buffalo", "Buffalo Bills")}[config.LEAGUE]
    teams = server.find_team(query=HOME[0])
    check(f"find_team finds {HOME[0]}", teams["count"] >= 1, str(teams["count"]))

    # THE COLLISION, from the other side. team 2 is Auburn in the college corpus and
    # the Buffalo Bills in the NFL one. Before the split a join that forgot `league`
    # matched both; now the other league's name is simply absent here, which is the
    # stronger guarantee.
    foreign = {"cfb": "Buffalo Bills", "nfl": "Auburn Tigers"}[config.LEAGUE]
    hits = server.find_team(query=foreign)["teams"]
    check(f"{foreign!r} is not in this corpus",
          not any(t["display_name"] == foreign for t in hits),
          str([t["display_name"] for t in hits][:3]))

    # team_id 2 exists in BOTH corpora and means different things. Assert it resolves
    # to this corpus's team, which is the bug the split fixes at the source.
    two = db.query("SELECT display_name FROM dim_team WHERE team_id = 2")
    if two:
        expect = {"cfb": "Auburn", "nfl": "Buffalo"}[config.LEAGUE]
        check("team_id 2 resolves to this corpus",
              two[0]["display_name"].startswith(expect), two[0]["display_name"])

    if config.LEAGUE == "cfb":
        players = server.find_player(name="Mahomes")
        check("find_player finds Mahomes", players["count"] >= 1)
        pm = next((p for p in players["players"] if p["athlete_id"] == 3139477), None)
        check("Mahomes is present in the college corpus", pm is not None)
        # THE BUG THE REBUILT DIMENSION EXISTS TO FIX. primary_team_id used to be the
        # modal team over a COMBINED career -- id 12, Kansas City -- and resolving 12
        # against college gave "Arizona Wildcats": a real team, a wrong answer, and
        # entirely plausible on screen. Rebuilt per corpus, it is Texas Tech.
        if pm:
            check("Mahomes' college team is Texas Tech, not the colliding id",
                  pm["primary_team"] == "Texas Tech Red Raiders", str(pm["primary_team"]))
            check("Mahomes' college career is 2014-2016",
                  (pm["first_season"], pm["last_season"]) == (2014, 2016),
                  f"{pm['first_season']}-{pm['last_season']}")
    else:
        players = server.find_player(name="Mahomes")
        pm = next((p for p in players["players"] if p["athlete_id"] == 3139477), None)
        check("Mahomes is present in the NFL corpus", pm is not None)
        if pm:
            check("Mahomes' NFL team is Kansas City",
                  pm["primary_team"] == "Kansas City Chiefs", str(pm["primary_team"]))
            check("Mahomes' NFL career starts 2017", pm["first_season"] == 2017,
                  str(pm["first_season"]))

    # No duplicate rows. dim_team is keyed by team_id alone inside a corpus, so the
    # two-rows-per-athlete failure this suite once caught cannot recur -- assert it
    # anyway, because that is what a regression test is for.
    ids = [p["athlete_id"] for p in players["players"]]
    check("find_player returns no duplicate athletes", len(ids) == len(set(ids)), str(ids))

    # The cross-league columns are GONE, by design. Their presence would mean a
    # filtered dimension had been shipped in place of a rebuilt one.
    cols = {c["column_name"] for c in server.describe_table("dim_athlete")["columns"]}
    check("dim_athlete carries no cross-league columns",
          not ({"leagues", "nfl_plays"} & cols), str({"leagues", "nfl_plays"} & cols))

    venue_q = {"cfb": "Rose Bowl", "nfl": "Lambeau"}[config.LEAGUE]
    check(f"find_venue finds {venue_q}", server.find_venue(query=venue_q)["count"] >= 1)

    unknown = db.query("SELECT count(*) AS n FROM dim_venue WHERE indoor IS NULL")[0]["n"]
    check("no venue has an unknown roof", unknown == 0, str(unknown))

    confs = server.list_conferences()
    floor = {"cfb": 10, "nfl": 2}[config.LEAGUE]
    check(f"conferences listed (>={floor})", confs["count"] >= floor, str(confs["count"]))


# --------------------------------------------------------------------------- games

def test_games() -> None:
    section("games")
    games = server.list_games(season=2024, limit=5)
    check("list_games returns NFL 2024", games["count"] == 5, str(games["count"]))

    game_id = games["games"][0]["game_id"]
    summary = server.game_summary(game_id)
    check("game_summary has a header", "game" in summary)
    check("game_summary derives two teams' scores",
          len(summary.get("derived_score", [])) == 2,
          str(summary.get("derived_score")))
    check("game_summary counts plays", len(summary.get("play_counts", [])) > 0)

    chart = server.drive_chart(game_id)
    check("drive_chart returns drives", chart.get("count", 0) > 10, str(chart.get("count")))
    check("drives are ordered",
          [d["drive_number"] for d in chart["drives"]]
          == sorted(d["drive_number"] for d in chart["drives"]))

    check("game_summary rejects a bad id", "error" in server.game_summary(1))


# --------------------------------------------------------------------------- plays

def test_plays() -> None:
    section("plays")
    kicks = server.query_plays(fact="kicks", season=2023,
                               play_kind="field_goal", limit=10)
    check("query_plays kicks", kicks["count"] == 10, str(kicks["count"]))
    check("query_plays returns play_text", all(p.get("play_text") for p in kicks["plays"]))
    check("query_plays resolved the conference",
          any(p.get("kicking_conference") for p in kicks["plays"]))

    scrim = server.query_plays(fact="scrimmage", season=2024,
                               play_kind="pass", is_touchdown=True, limit=5)
    check("query_plays scrimmage + touchdown filter", scrim["count"] == 5)
    check("touchdown filter held", all(p["is_touchdown"] for p in scrim["plays"]))

    long_fg = server.query_plays(fact="kicks", min_fg_distance=60, fg_made=True, limit=20)
    check("60+ yard makes exist", long_fg["count"] > 0, str(long_fg["count"]))
    check("60+ filter held",
          all(p["fg_distance_yds"] >= 60 for p in long_fg["plays"]))

    uid = kicks["plays"][0]["play_uid"]
    detail = server.play_detail(uid)
    check("play_detail finds the play", detail.get("fact") == "kicks")
    check("play_detail lists participants", isinstance(detail.get("participants"), list))

    check("query_plays rejects a bad fact", _raises(lambda: server.query_plays(fact="nope")))
    check("query_plays rejects a bad play_kind",
          _raises(lambda: server.query_plays(fact="kicks", play_kind="touchdown")))
    check("play_detail rejects a bad uid", "error" in server.play_detail("nope"))


def _raises(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    except Exception:
        return False
    return False


# --------------------------------------------------------------------------- players

def test_players() -> None:
    section("players")
    found = server.find_player(name="Mahomes", limit=5)["players"]
    aid = next(p["athlete_id"] for p in found if "Mahomes" in (p["known_name"] or ""))

    profile = server.player_profile(aid)
    check("player_profile returns identity", profile["player"]["athlete_id"] == aid)
    # Career grain is now WITHIN this corpus. Mahomes has three college seasons
    # (2014-2016) and ten NFL ones; the old suite asserted >3 against a combined
    # career, which is exactly the blending the split removed.
    seasons_floor = {"cfb": 3, "nfl": 8}[config.LEAGUE]
    check("Mahomes' scrimmage seasons are this corpus's only",
          len(profile["scrimmage_by_season"]) >= seasons_floor,
          str(len(profile["scrimmage_by_season"])))
    check("player_profile carries no cross-league field",
          "leagues" not in profile["player"] and "nfl_plays" not in profile["player"],
          str(sorted(profile["player"])[:8]))

    # A season INSIDE this corpus's career for this player. 2023 is right for the NFL
    # and empty for college, where Mahomes last played in 2016 -- asking for it there
    # asserted a career that spans both corpora, which is what the split removed.
    log_season = {"cfb": 2016, "nfl": 2023}[config.LEAGUE]
    log = server.player_game_log(aid, season=log_season, limit=25)
    check(f"player_game_log returns {log_season} games", log["count"] > 5, str(log["count"]))

    check("player_profile rejects a bad id", "error" in server.player_profile(-1))

    for measure in q.LEADERBOARD_SQL:
        board = server.leaderboard(measure=measure, season=2023,
                                   min_attempts=10, limit=5)
        check(f"leaderboard {measure}", board["count"] > 0, str(board))

    # The min_attempts guard is the whole point of the tool.
    loose = server.leaderboard(measure="fg_pct", season=2023,
                               min_attempts=1, limit=5)
    tight = server.leaderboard(measure="fg_pct", season=2023,
                               min_attempts=25, limit=5)
    check("min_attempts filters", loose["count"] >= tight["count"])
    check("min_attempts is honoured",
          all(r["attempts"] >= 25 for r in tight["leaders"]))
    check("leaderboard rejects a bad measure",
          _raises(lambda: server.leaderboard(measure="vibes")))


# --------------------------------------------------------------------------- teams

def test_teams() -> None:
    section("teams")
    # Pick the exact team rather than trusting position. find_team ranks by
    # division and then name, so [0] is usually right -- but "Alabama" legitimately
    # matches five programmes and a test that depends on the ordering is testing
    # the ordering, not team_season.
    NAME = {"cfb": "Alabama Crimson Tide", "nfl": "Kansas City Chiefs"}[config.LEAGUE]
    teams = server.find_team(query=NAME)["teams"]
    picked = next(t for t in teams if t["display_name"] == NAME)
    ts = server.team_season(team_id=picked["team_id"], season=2023)
    check("team_season returns a row", "team_season" in ts, str(ts))
    if "team_season" in ts:
        row = ts["team_season"]
        check("team_season has offence", (row.get("off_plays") or 0) > 500,
              str(row.get("off_plays")))
        check("team_season has defence", (row.get("def_plays") or 0) > 500,
              str(row.get("def_plays")))
    # team_season no longer takes a league -- inside one corpus a team_id is
    # unambiguous. What must still hold is that an id from the OTHER corpus is not
    # silently answered: NFL ids run 1-34 and collide with college ones, so the
    # check is that an id absent from THIS dim_team is refused rather than blended.
    absent = db.query("SELECT max(team_id) + 1 AS t FROM dim_team")[0]["t"]
    check("team_season refuses a team_id not in this corpus",
          "error" in server.team_season(team_id=absent, season=2023))


# --------------------------------------------------------------------------- the football
#
# These are the checks that prove the LOAD is right, not just that the SQL runs.

def test_football() -> None:
    section("does it behave like football")

    fg = server.league_trend(measure="fg_pct")["series"]
    row = [r for r in fg if r["season"] == 2023]
    # The two corpora kick differently and that difference is the cheapest proof the
    # right rows are in the right schema: a college server reading NFL kicks would
    # land in the 80s, not the 70s.
    lo, hi = {"cfb": (70, 80), "nfl": (80, 90)}[config.LEAGUE]
    check(f"FG% 2023 in {lo}-{hi}", approx(row[0]["value"], lo, hi), str(row))

    ypc = server.league_trend(measure="yards_per_carry")["series"]
    got = [r for r in ypc if r["season"] == 2023][0]["value"]
    # The pro number is LOWER than the college one, which is correct football and is
    # the opposite of what a copied pipeline produces. The comparison itself can no
    # longer be made from one server -- so each asserts its OWN band, and the bands
    # do not overlap. A college server reading NFL rushes fails the college band.
    lo, hi = {"cfb": (4.5, 6.0), "nfl": (3.9, 4.6)}[config.LEAGUE]
    check(f"yards/carry 2023 in {lo}-{hi}", approx(got, lo, hi), str(got))

    punts = server.league_trend(measure="punt_gross_avg")["series"]
    gross = [r for r in punts if r["season"] == 2023][0]["value"]
    lo, hi = {"cfb": (40, 44), "nfl": (44, 49)}[config.LEAGUE]
    check(f"punt gross {lo}-{hi}", approx(gross, lo, hi), str(gross))

    # Monotonic FG% by distance. A silently wrong distance field does not produce
    # this, in either corpus -- which is why it is worth asserting in both, once each.
    buckets = server.fg_by_distance()["buckets"]
    rows = [b for b in buckets if 20 <= b["fg_dist_bucket"] <= 50]
    pcts = [float(b["fg_pct"]) for b in sorted(rows, key=lambda b: b["fg_dist_bucket"])]
    check("FG% falls monotonically 20->50",
          all(a > b for a, b in zip(pcts, pcts[1:])), str(pcts))

    # The rule changes. College steps once in 2018; the NFL collapses in two
    # stages, 2024 and 2025. Two different shapes, in a column parsed by two
    # different modules.
    # THE SHARPEST PROOF THE RIGHT ROWS ARE IN THE RIGHT SCHEMA. The two leagues
    # changed the kickoff rule in different years and in different shapes: college
    # steps once at 2018, the NFL collapses in two stages across 2024-2025. A server
    # reading the other corpus's kickoffs fails its own assertion outright, because
    # the step is in the wrong season.
    tb = server.league_trend(measure="kickoff_touchback_pct")["series"]
    by_season = {r["season"]: float(r["value"]) for r in tb}
    if config.LEAGUE == "cfb":
        check("college touchback% steps up at 2018",
              by_season[2018] - by_season[2017] > 5,
              f"2017={by_season.get(2017)} 2018={by_season.get(2018)}")
    else:
        check("NFL touchback% collapses by 2025",
              by_season[2025] < 40 < by_season[2023],
              f"2023={by_season.get(2023)} 2024={by_season.get(2024)} "
              f"2025={by_season.get(2025)}")

    # Drive TD% is monotonic in starting field position.
    zones = server.drive_outcomes()["zones"]
    tds = [float(z["td_pct"]) for z in sorted(zones, key=lambda z: z["start_zone"])]
    check("drive TD% falls with field position",
          all(a > b for a, b in zip(tds, tds[1:])), str(tds))

    # First-down rate rises with down.
    rows = db.query(
        "SELECT down, round(100.0*avg(first_down_gained::int),1) AS pct "
        f"FROM {config.SCHEMA}.scrimmage_wide WHERE down BETWEEN 1 AND 4 "
        "AND play_kind IN ('rush','pass','sack') GROUP BY down ORDER BY down")
    pcts = [float(r["pct"]) for r in rows]
    check("first-down rate rises with down",
          all(a < b for a, b in zip(pcts, pcts[1:])), str(pcts))


# --------------------------------------------------------------------------- NULL semantics
#
# The defect fixed on 2026-08-31 was exactly this: flags stored false where the
# outcome was unreadable. If these checks fail, the load reintroduced it.

def test_null_semantics() -> None:
    section("NULL semantics")
    row = db.query(
        "SELECT count(*) FILTER (WHERE returned IS NULL) AS unstated, count(*) AS total "
        f"FROM {config.SCHEMA}.play_wide WHERE play_kind IN ('punt','kickoff')")[0]
    pct = 100.0 * row["unstated"] / row["total"]
    check("unstated outcomes are NULL, not false", row["unstated"] > 0, str(row))
    # PER CORPUS, and the split is what surfaced this: README.md and known_limits()
    # both quote 9.9% as if it were corpus-wide. It is the COLLEGE figure. The NFL
    # feed states an outcome far more often -- 3.4% unstated against college's 9.9%
    # -- so the blended number (8.4%) describes neither corpus.
    lo, hi = {"cfb": (8.0, 12.0), "nfl": (2.5, 5.0)}[config.LEAGUE]
    check(f"unstated share in {lo}-{hi}%", approx(pct, lo, hi), f"{pct:.1f}%")

    negated = db.query(
        f"SELECT count(*) AS n FROM {config.SCHEMA}.play_wide "
        "WHERE play_kind = 'field_goal' AND fg_made IS NULL")[0]["n"]
    check("negated field goals are NULL", negated > 0, str(negated))

    sentinel = db.query(
        f"SELECT count(*) AS n FROM {config.SCHEMA}.play_wide WHERE yards_to_goal = 0")[0]["n"]
    check("yards_to_goal = 0 sentinel present", sentinel > 0, str(sentinel))

    # kick_outcomes must report the unstated share rather than burying it.
    out = server.kick_outcomes(play_kind="kickoff", season=2023)
    check("kick_outcomes reports pct_unstated",
          out["seasons"] and out["seasons"][0]["pct_unstated"] is not None)

    # A sack is not a pass attempt in NCAA accounting.
    sacks = db.query(
        f"SELECT count(*) AS n FROM {config.SCHEMA}.scrimmage_wide "
        "WHERE play_kind = 'sack' AND is_complete IS NOT NULL")[0]["n"]
    check("sacks carry is_complete IS NULL", sacks == 0, str(sacks))


# --------------------------------------------------------------------------- integrity

def test_integrity() -> None:
    section("referential integrity")
    checks = {
        "plays with no fact_game row":
            f"SELECT count(*) AS n FROM {config.SCHEMA}.special_teams_play p LEFT JOIN {config.SCHEMA}.fact_game g "
            "ON g.game_id = p.game_id WHERE g.game_id IS NULL",
        "kicker_athlete_id not in dim_athlete":
            f"SELECT count(*) AS n FROM {config.SCHEMA}.special_teams_play p LEFT JOIN {config.SCHEMA}.dim_athlete a "
            "ON a.athlete_id = p.kicker_athlete_id "
            "WHERE p.kicker_athlete_id IS NOT NULL AND a.athlete_id IS NULL",
        "venue_id not in dim_venue":
            f"SELECT count(*) AS n FROM {config.SCHEMA}.fact_game g LEFT JOIN {config.SCHEMA}.dim_venue v "
            "ON v.venue_id = g.venue_id WHERE g.venue_id IS NOT NULL AND v.venue_id IS NULL",
        "kicking team with no (team_id, season) row":
            f"SELECT count(*) AS n FROM {config.SCHEMA}.special_teams_play p LEFT JOIN {config.SCHEMA}.dim_team_season t "
            "ON t.team_id = p.kicking_team_id AND t.season = p.season "
            "WHERE p.kicking_team_id IS NOT NULL AND t.team_id IS NULL",
        "play_uid collisions across the two facts":
            f"SELECT count(*) AS n FROM {config.SCHEMA}.special_teams_play s "
            f"JOIN {config.SCHEMA}.scrimmage_play c ON c.play_uid = s.play_uid",
    }
    for label, sql in checks.items():
        n = db.query(sql)[0]["n"]
        check(label + " = 0", n == 0, str(n))

    # The Pro Bowl is dropped at fetch time; its all-star squads must be absent. Only
    # asks the question of the corpus it can be asked of -- team ids 31/32 are real
    # college programmes, so running this against cfb would assert a falsehood.
    if config.LEAGUE == "nfl":
        probowl = db.query(
            f"SELECT count(*) AS n FROM {config.SCHEMA}.fact_game "
            "WHERE home_team_id IN (31,32) OR away_team_id IN (31,32)"
        )[0]["n"]
        check("Pro Bowl games absent", probowl == 0, str(probowl))

    # The wide tables must agree with the facts they project.
    for wide, narrow in (("play_wide", "special_teams_play"),
                         ("scrimmage_wide", "scrimmage_play")):
        a = db.query(f"SELECT count(*) AS n FROM {config.SCHEMA}.{wide}")[0]["n"]
        b = db.query(f"SELECT count(*) AS n FROM {config.SCHEMA}.{narrow}")[0]["n"]
        check(f"{wide} row count matches {narrow}", a == b, f"{a} vs {b}")


# --------------------------------------------------------------------------- schema tools

def test_schema_tools() -> None:
    section("schema tools")
    schema = server.list_schema()
    names = {t["table_name"] for t in schema["tables"]}
    check("list_schema returns every readable table",
          set(q.READABLE_TABLES).issubset(names),
          str(set(q.READABLE_TABLES) - names))

    # The grants and the guard's allow-list are two separate lists on purpose.
    # This is the check that catches them drifting.
    granted = {f"{config.SCHEMA}.{t}" for t in names}
    check("guard allow-list matches the grants",
          guard.ALLOWED_TABLES == granted,
          f"guard-only={guard.ALLOWED_TABLES - granted} grants-only={granted - guard.ALLOWED_TABLES}")

    for table in q.READABLE_TABLES:
        desc = server.describe_table(table)
        check(f"describe_table {table}", desc.get("column_count", 0) > 0, str(desc)[:120])

    # The caveats reached the database rather than living only in a prompt.
    pw = server.describe_table("play_wide")
    commented = [c for c in pw["columns"] if c.get("description")]
    check("play_wide carries column comments", len(commented) >= 10, str(len(commented)))
    fg_made = next(c for c in pw["columns"] if c["column_name"] == "fg_made")
    check("fg_made comment explains the NULL",
          "NEGATED" in (fg_made["description"] or "").upper(), str(fg_made["description"]))

    check("describe_table rejects an unknown table",
          "error" in server.describe_table("pg_authid"))


# --------------------------------------------------------------------------- the guard

_REJECTIONS = [
    ("write: insert", f"INSERT INTO {config.SCHEMA}.dim_team VALUES (1)"),
    ("write: update", f"UPDATE {config.SCHEMA}.play_wide SET fg_made = true"),
    ("write: delete", f"DELETE FROM {config.SCHEMA}.play_wide"),
    ("write: drop", f"DROP TABLE {config.SCHEMA}.play_wide"),
    ("write: truncate", f"TRUNCATE {config.SCHEMA}.play_wide"),
    ("write: create", "CREATE TABLE x (a int)"),
    ("write: alter", f"ALTER TABLE {config.SCHEMA}.play_wide ADD COLUMN x int"),
    ("write: grant", f"GRANT SELECT ON {config.SCHEMA}.play_wide TO public"),
    ("stacking", f"SELECT 1 FROM {config.SCHEMA}.play_wide; DROP TABLE {config.SCHEMA}.play_wide"),
    ("stacking with comment", f"SELECT 1 FROM {config.SCHEMA}.play_wide;/**/DROP TABLE {config.SCHEMA}.play_wide"),
    ("catalog: pg_authid", "SELECT * FROM pg_authid"),
    ("catalog: pg_stat_activity", "SELECT * FROM pg_stat_activity"),
    ("catalog: pg_settings", "SELECT * FROM pg_settings"),
    ("catalog: information_schema", "SELECT * FROM information_schema.tables"),
    ("unknown table", f"SELECT * FROM {config.SCHEMA}.secrets"),
    ("cross-database", "SELECT * FROM weatherdata.public.observations"),
    ("file read", "SELECT pg_read_file('/etc/passwd')"),
    ("dblink", "SELECT dblink('x','y')"),
    ("sleep", "SELECT pg_sleep(60)"),
    ("lock", f"SELECT * FROM {config.SCHEMA}.play_wide FOR UPDATE"),
    ("CTE hiding a write",
     f"WITH x AS (DELETE FROM {config.SCHEMA}.play_wide RETURNING 1) SELECT * FROM x"),
    ("set", "SET statement_timeout = 0"),
    ("empty", ""),
    ("not sql", "this is not sql at all ((("),
]


def test_guard() -> None:
    section("guard rejections")
    for label, sql in _REJECTIONS:
        try:
            guard.check(sql)
            check(f"reject {label}", False, "was ACCEPTED")
        except guard.SQLNotAllowed:
            check(f"reject {label}", True)
        except Exception as exc:  # a parse failure is also a rejection, but say so
            check(f"reject {label}", False, f"raised {type(exc).__name__}: {exc}")

    section("guard acceptances")
    ok = [
        ("plain select", f"SELECT league, count(*) FROM {config.SCHEMA}.play_wide GROUP BY 1"),
        ("cte", f"WITH x AS (SELECT * FROM {config.SCHEMA}.play_wide LIMIT 5) SELECT count(*) FROM x"),
        ("join across facts",
         f"SELECT count(*) FROM {config.SCHEMA}.play_wide p JOIN {config.SCHEMA}.fact_game g USING (game_id)"),
        ("union",
         f"SELECT league FROM {config.SCHEMA}.play_wide UNION SELECT league FROM {config.SCHEMA}.scrimmage_wide"),
        ("unqualified name resolves to pbp", "SELECT count(*) FROM play_wide"),
    ]
    for label, sql in ok:
        try:
            rewritten, report = guard.check(sql)
            check(f"accept {label}", bool(rewritten) and report["row_limit"] > 0)
        except Exception as exc:
            check(f"accept {label}", False, f"{type(exc).__name__}: {exc}")

    section("limit handling")
    _, r = guard.check(f"SELECT * FROM {config.SCHEMA}.play_wide")
    check("missing LIMIT gets the default",
          r["limit_source"] == "default" and r["row_limit"] == guard.DEFAULT_LIMIT, str(r))
    _, r = guard.check(f"SELECT * FROM {config.SCHEMA}.play_wide LIMIT 5")
    check("caller LIMIT is kept", r["limit_source"] == "caller" and r["row_limit"] == 5, str(r))
    _, r = guard.check(f"SELECT * FROM {config.SCHEMA}.play_wide LIMIT 999999")
    check("oversized LIMIT is capped",
          r["limit_source"] == "capped" and r["row_limit"] == guard.MAX_LIMIT, str(r))


# --------------------------------------------------------------------------- run_sql

def test_run_sql() -> None:
    section("run_sql")
    res = server.run_sql(f"SELECT season, count(*) AS n FROM {config.SCHEMA}.play_wide GROUP BY 1 ORDER BY 1")
    # Thirteen seasons, not two leagues: the grouping column changed with the split.
    check("run_sql returns rows", res.get("row_count") >= 13, str(res)[:200])
    check("run_sql reports its tables", res.get("tables") == [f"{config.SCHEMA}.play_wide"], str(res.get("tables")))

    explained = server.run_sql(f"SELECT * FROM {config.SCHEMA}.play_wide", explain_only=True)
    check("explain_only returns a plan", explained.get("explain_only") is True)
    check("explain_only returns no data rows",
          all("play_uid" not in str(r) for r in explained.get("rows", [])))

    rejected = server.run_sql(f"DROP TABLE {config.SCHEMA}.play_wide")
    check("run_sql reports a rejection", rejected.get("rejected") is True)

    truncated = server.run_sql(f"SELECT play_uid FROM {config.SCHEMA}.play_wide")
    check("run_sql flags truncation", truncated.get("truncated") is True, str(truncated.get("row_count")))


# --------------------------------------------------------------------------- write protection
#
# The guard is the SECOND line. These prove the first one -- the role itself --
# by going around the guard entirely and asking the database directly.

def test_write_protection() -> None:
    section("write protection (guard bypassed on purpose)")
    import psycopg

    for label, sql in [
        ("INSERT refused by the role", "INSERT INTO {config.SCHEMA}.dim_team VALUES ('cfb', 999999, 'x')"),
        ("UPDATE refused by the role", "UPDATE {config.SCHEMA}.dim_team SET display_name = 'x'"),
        ("CREATE refused by the role", f"CREATE TABLE {config.SCHEMA}.should_not_exist (a int)"),
    ]:
        try:
            # db.query_guarded deliberately does NOT re-validate; that is what lets
            # this test reach the database with a write.
            db.query_guarded(sql)
            check(label, False, "the database ACCEPTED a write")
        except (psycopg.errors.InsufficientPrivilege,
                psycopg.errors.ReadOnlySqlTransaction) as exc:
            check(label, True, type(exc).__name__)
        except Exception as exc:
            # Any other refusal still counts as refused, but name it.
            check(label, True, f"refused as {type(exc).__name__}")

    # The pool wraps a dead tunnel as `PoolTimeout: couldn't get a connection`,
    # which matches none of the connection markers and used to be reported as
    # "the connection itself looks fine" -- the exact opposite of the truth, and
    # it sends someone to debug their SQL. Checked on the message classifier
    # rather than by actually breaking the connection.
    hint = db._operational_hint(
        Exception("couldn't get a connection after 15.00 sec"))
    check("a pool timeout is diagnosed as a connection failure",
          "tunnel" in hint.lower(), hint[:120])

    role = db.query(
        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
        "FROM pg_roles WHERE rolname = current_user")[0]
    check("connected role has no elevated attributes",
          not any(role.values()), str(role))
    check("connected as pbp_ro",
          db.query("SELECT current_user AS u")[0]["u"] == "pbp_ro",
          db.query("SELECT current_user AS u")[0]["u"])


# --------------------------------------------------------------------------- quality tools

def test_quality_tools() -> None:
    section("quality tools")
    pq = server.parse_quality()
    check("parse_quality returns seasons", pq["count"] > 10, str(pq["count"]))

    overall = db.query(
        "SELECT round(100.0*avg((parse_confidence='exact')::int),2) AS pct "
        f"FROM {config.SCHEMA}.play_wide")
    pct = float(overall[0]["pct"])
    check("parse rate ~98%", approx(pct, 97, 99), str(pct))

    audit = server.audit_plays(limit=5)
    check("audit_plays returns rows with text",
          audit["count"] > 0 and all(p["play_text"] for p in audit["plays"]))

    limits = server.known_limits()
    check("known_limits returns the caveats", limits["count"] >= 12, str(limits["count"]))
    check("known_limits filters", server.known_limits(topic="turnover")["count"] >= 1)


# --------------------------------------------------------------------------- main

def main() -> int:
    print("pbp warehouse MCP -- selftest")
    print(f"database: {db.database()}")

    suites = [
        test_coverage, test_discovery, test_games, test_plays, test_players,
        test_teams, test_football, test_null_semantics, test_integrity,
        test_schema_tools, test_guard, test_run_sql, test_write_protection,
        test_quality_tools,
    ]
    for suite in suites:
        try:
            suite()
        except Exception:
            _failed.append(f"{suite.__name__} raised")
            print(f"  ERROR in {suite.__name__}:")
            traceback.print_exc()

    total = _passed + len(_failed)
    print(f"\n{_passed}/{total} checks passed")
    if _failed:
        print("\nfailures:")
        for item in _failed:
            print(f"  - {item}")
        return 1

    # No `league` column here, by design -- c8a38b1 removed it from the serving schemas
    # and this line, in that same commit, kept selecting it. Every assertion above passed
    # and then the summary raised UndefinedColumn, so the process exited 1 with "146/146
    # checks passed" on its last line. The server IS the league; name it from the schema.
    latest = db.query(
        "SELECT max(season) AS s, max(last_kickoff) AS k "
        f"FROM {config.SCHEMA}.season_status")
    for row in latest:
        print(f"{config.SCHEMA}: through season {row['s']}, last game {row['k']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        db.close_all()
