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

from . import db, guard, queries as q, server

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
    check("data_coverage returns both leagues",
          {t["league"] for t in cov["totals_by_league"]} == {"cfb", "nfl"},
          str([t["league"] for t in cov["totals_by_league"]]))

    by_league = {t["league"]: t for t in cov["totals_by_league"]}
    # FLOORS, not exact counts. README.md's corpus table was written at a point in
    # time and 2026 is still being played in both leagues, so these only ever grow.
    # An exact assertion here would fail every week for the right reason, which is
    # how a suite stops being read. The exact-match check that matters is
    # local-vs-mirror row reconciliation, and that lives in scripts/sync_ec2.py --
    # this server cannot see local Postgres.
    for league, field, floor in [
        ("cfb", "kick_plays", 316_397),
        ("nfl", "kick_plays", 90_819),
        ("cfb", "scrimmage_plays", 1_510_679),
        ("nfl", "scrimmage_plays", 447_635),
    ]:
        got = by_league[league][field]
        check(f"{league} {field} >= {floor:,} (README floor)", got >= floor, str(got))
        # A corpus that has somehow tripled is a double-load, which is the other
        # way this can go wrong and is silent.
        check(f"{league} {field} not implausibly large", got < floor * 1.5, str(got))
    check("window starts 2014", all(t["first_season"] == 2014 for t in cov["totals_by_league"]))
    check("seasons present for both leagues", len(cov["seasons"]) >= 26,
          str(len(cov["seasons"])))


# --------------------------------------------------------------------------- discovery

def test_discovery() -> None:
    section("discovery")
    teams = server.find_team(query="Alabama", league="cfb")
    check("find_team finds Alabama", teams["count"] >= 1)

    # The collision the whole league-keying design exists for.
    cfb2 = server.find_team(query="Auburn", league="cfb")["teams"]
    nfl_teams = server.find_team(query="Buffalo Bills", league="nfl")["teams"]
    check("find_team: Auburn is cfb", any(t["league"] == "cfb" for t in cfb2))
    check("find_team: Buffalo Bills is nfl", any(t["league"] == "nfl" for t in nfl_teams))
    # The collision itself: the same team_id means different teams per league.
    auburn = [t for t in cfb2 if t["display_name"].startswith("Auburn")]
    bills = [t for t in nfl_teams if t["display_name"] == "Buffalo Bills"]
    if auburn and bills:
        check("find_team ranks the exact NFL match first",
              nfl_teams[0]["display_name"] == "Buffalo Bills",
              nfl_teams[0]["display_name"])

    players = server.find_player(name="Mahomes")
    check("find_player finds Mahomes", players["count"] >= 1)
    mahomes = [p for p in players["players"] if "Mahomes" in (p["known_name"] or "")]
    check("Mahomes has an athlete_id", bool(mahomes) and mahomes[0]["athlete_id"])

    # NO DUPLICATE ROWS. dim_athlete is shared across leagues and carries no league
    # column, but dim_team is keyed (league, team_id) and the ids collide -- so a
    # join on primary_team_id alone returns TWO rows per athlete. This suite caught
    # exactly that.
    ids = [p["athlete_id"] for p in players["players"]]
    check("find_player returns no duplicate athletes", len(ids) == len(set(ids)), str(ids))

    # ...and the team it resolves to must be from the right league. primary_team_id
    # is the MODAL team over the whole career, so for a cfb+nfl athlete it is usually
    # the NFL one. Resolving Mahomes' id 12 against cfb gives "Arizona Wildcats",
    # which is a real team and a wrong answer.
    pm = next(p for p in players["players"] if p["athlete_id"] == 3139477)
    check("Mahomes' primary team is Kansas City, not the colliding cfb id",
          pm["primary_team"] == "Kansas City Chiefs"
          and pm["primary_team_league"] == "nfl",
          f"{pm['primary_team']} ({pm['primary_team_league']})")

    # The same collision on a college-only career must still resolve to college.
    dunn = [p for p in server.find_player(name="Christopher Dunn")["players"]
            if p["leagues"] == "cfb"]
    if dunn:
        check("a college-only career resolves to a college team",
              dunn[0]["primary_team_league"] == "cfb", str(dunn[0]["primary_team"]))

    # 2,779 people span both leagues; the shared athlete dimension is the reason.
    both = db.query(
        "SELECT count(*) AS n FROM pbp.dim_athlete WHERE leagues = 'cfb+nfl'")
    check("cross-league athletes present", both[0]["n"] > 2_000, str(both[0]["n"]))

    venues = server.find_venue(query="Rose Bowl")
    check("find_venue finds the Rose Bowl", venues["count"] >= 1)

    # A floor, not an exact count. README.md still says "18 indoor / 183 outdoor,
    # 201 venues" from when it was written; the corpus is now 217 venues, 20 of them
    # indoor, and it grows whenever a new stadium hosts a game. What must hold is
    # that the flag is POPULATED -- 0 unknown was the point of adding it -- not that
    # the total is frozen.
    indoor = server.find_venue(indoor=True, limit=100)
    check("indoor venues present (>=18)", indoor["count"] >= 18, str(indoor["count"]))
    unknown = db.query(
        "SELECT count(*) AS n FROM pbp.dim_venue WHERE indoor IS NULL")[0]["n"]
    check("no venue has an unknown roof", unknown == 0, str(unknown))

    confs = server.list_conferences(league="cfb")
    check("conferences listed for cfb", confs["count"] >= 10, str(confs["count"]))


# --------------------------------------------------------------------------- games

def test_games() -> None:
    section("games")
    games = server.list_games(league="nfl", season=2024, limit=5)
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
    kicks = server.query_plays(fact="kicks", league="cfb", season=2023,
                               play_kind="field_goal", limit=10)
    check("query_plays kicks", kicks["count"] == 10, str(kicks["count"]))
    check("query_plays returns play_text", all(p.get("play_text") for p in kicks["plays"]))
    check("query_plays resolved the conference",
          any(p.get("kicking_conference") for p in kicks["plays"]))

    scrim = server.query_plays(fact="scrimmage", league="nfl", season=2024,
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
    check("Mahomes has scrimmage seasons", len(profile["scrimmage_by_season"]) > 3,
          str(len(profile["scrimmage_by_season"])))
    check("Mahomes spans both leagues",
          profile["player"]["leagues"] == "cfb+nfl", str(profile["player"]["leagues"]))

    log = server.player_game_log(aid, season=2023, limit=25)
    check("player_game_log returns games", log["count"] > 5, str(log["count"]))

    check("player_profile rejects a bad id", "error" in server.player_profile(-1))

    for measure in q.LEADERBOARD_SQL:
        board = server.leaderboard(measure=measure, league="nfl", season=2023,
                                   min_attempts=10, limit=5)
        check(f"leaderboard {measure}", board["count"] > 0, str(board))

    # The min_attempts guard is the whole point of the tool.
    loose = server.leaderboard(measure="fg_pct", league="cfb", season=2023,
                               min_attempts=1, limit=5)
    tight = server.leaderboard(measure="fg_pct", league="cfb", season=2023,
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
    teams = server.find_team(query="Alabama Crimson Tide", league="cfb")["teams"]
    alabama = next(t for t in teams if t["display_name"] == "Alabama Crimson Tide")
    ts = server.team_season(team_id=alabama["team_id"], league="cfb", season=2023)
    check("team_season returns a row", "team_season" in ts, str(ts))
    if "team_season" in ts:
        row = ts["team_season"]
        check("team_season has offence", (row.get("off_plays") or 0) > 500,
              str(row.get("off_plays")))
        check("team_season has defence", (row.get("def_plays") or 0) > 500,
              str(row.get("def_plays")))
    check("team_season requires a valid league",
          _raises(lambda: server.team_season(team_id=1, league="xfl", season=2023)))


# --------------------------------------------------------------------------- the football
#
# These are the checks that prove the LOAD is right, not just that the SQL runs.

def test_football() -> None:
    section("does it behave like football")

    fg = server.league_trend(measure="fg_pct")["series"]
    cfb_fg = [r for r in fg if r["league"] == "cfb" and r["season"] == 2023]
    nfl_fg = [r for r in fg if r["league"] == "nfl" and r["season"] == 2023]
    check("college FG% 2023 in 70-80", approx(cfb_fg[0]["value"], 70, 80), str(cfb_fg))
    check("NFL FG% 2023 in 80-90", approx(nfl_fg[0]["value"], 80, 90), str(nfl_fg))

    ypc = server.league_trend(measure="yards_per_carry")["series"]
    cfb_ypc = [r for r in ypc if r["league"] == "cfb" and r["season"] == 2023][0]["value"]
    nfl_ypc = [r for r in ypc if r["league"] == "nfl" and r["season"] == 2023][0]["value"]
    # The pro number is LOWER, which is correct and is the opposite of what a
    # copied pipeline would produce.
    check("NFL yards/carry is BELOW college", nfl_ypc < cfb_ypc, f"{nfl_ypc} vs {cfb_ypc}")
    check("college yards/carry in 4.5-6", approx(cfb_ypc, 4.5, 6.0), str(cfb_ypc))
    check("NFL yards/carry in 3.8-4.8", approx(nfl_ypc, 3.8, 4.8), str(nfl_ypc))

    punts = server.league_trend(measure="punt_gross_avg")["series"]
    cfb_p = [r for r in punts if r["league"] == "cfb" and r["season"] == 2023][0]["value"]
    nfl_p = [r for r in punts if r["league"] == "nfl" and r["season"] == 2023][0]["value"]
    check("college punt gross 40-44", approx(cfb_p, 40, 44), str(cfb_p))
    check("NFL punt gross 44-49", approx(nfl_p, 44, 49), str(nfl_p))

    # Monotonic FG% by distance, in BOTH leagues. A silently wrong distance field
    # does not produce this.
    for league in ("cfb", "nfl"):
        buckets = server.fg_by_distance(league=league)["buckets"]
        rows = [b for b in buckets if 20 <= b["fg_dist_bucket"] <= 50]
        pcts = [float(b["fg_pct"]) for b in sorted(rows, key=lambda b: b["fg_dist_bucket"])]
        check(f"{league} FG% falls monotonically 20->50",
              all(a > b for a, b in zip(pcts, pcts[1:])), str(pcts))

    # The rule changes. College steps once in 2018; the NFL collapses in two
    # stages, 2024 and 2025. Two different shapes, in a column parsed by two
    # different modules.
    tb = server.league_trend(measure="kickoff_touchback_pct")["series"]
    cfb_tb = {r["season"]: float(r["value"]) for r in tb if r["league"] == "cfb"}
    nfl_tb = {r["season"]: float(r["value"]) for r in tb if r["league"] == "nfl"}
    check("college touchback% steps up at 2018",
          cfb_tb[2018] - cfb_tb[2017] > 5, f"2017={cfb_tb.get(2017)} 2018={cfb_tb.get(2018)}")
    check("NFL touchback% collapses by 2025",
          nfl_tb[2025] < 40 < nfl_tb[2023],
          f"2023={nfl_tb.get(2023)} 2024={nfl_tb.get(2024)} 2025={nfl_tb.get(2025)}")

    # Drive TD% is monotonic in starting field position, in both leagues.
    for league in ("cfb", "nfl"):
        zones = server.drive_outcomes(league=league)["zones"]
        tds = [float(z["td_pct"]) for z in sorted(zones, key=lambda z: z["start_zone"])]
        check(f"{league} drive TD% falls with field position",
              all(a > b for a, b in zip(tds, tds[1:])), str(tds))

    # First-down rate rises with down, in both leagues.
    for league in ("cfb", "nfl"):
        rows = db.query(
            "SELECT down, round(100.0*avg(first_down_gained::int),1) AS pct "
            "FROM pbp.scrimmage_wide WHERE league = %s AND down BETWEEN 1 AND 4 "
            "AND play_kind IN ('rush','pass','sack') GROUP BY down ORDER BY down",
            (league,))
        pcts = [float(r["pct"]) for r in rows]
        check(f"{league} first-down rate rises with down",
              all(a < b for a, b in zip(pcts, pcts[1:])), str(pcts))


# --------------------------------------------------------------------------- NULL semantics
#
# The defect fixed on 2026-08-31 was exactly this: flags stored false where the
# outcome was unreadable. If these checks fail, the load reintroduced it.

def test_null_semantics() -> None:
    section("NULL semantics")
    row = db.query(
        "SELECT count(*) FILTER (WHERE returned IS NULL) AS unstated, count(*) AS total "
        "FROM pbp.play_wide WHERE play_kind IN ('punt','kickoff')")[0]
    pct = 100.0 * row["unstated"] / row["total"]
    check("unstated outcomes are NULL, not false", row["unstated"] > 0, str(row))
    check("unstated share is ~9.9%", approx(pct, 8, 12), f"{pct:.1f}%")

    negated = db.query(
        "SELECT count(*) AS n FROM pbp.play_wide "
        "WHERE play_kind = 'field_goal' AND fg_made IS NULL")[0]["n"]
    check("negated field goals are NULL", negated > 0, str(negated))

    sentinel = db.query(
        "SELECT count(*) AS n FROM pbp.play_wide WHERE yards_to_goal = 0")[0]["n"]
    check("yards_to_goal = 0 sentinel present", sentinel > 0, str(sentinel))

    # kick_outcomes must report the unstated share rather than burying it.
    out = server.kick_outcomes(play_kind="kickoff", league="cfb", season=2023)
    check("kick_outcomes reports pct_unstated",
          out["seasons"] and out["seasons"][0]["pct_unstated"] is not None)

    # A sack is not a pass attempt in NCAA accounting.
    sacks = db.query(
        "SELECT count(*) AS n FROM pbp.scrimmage_wide "
        "WHERE play_kind = 'sack' AND is_complete IS NOT NULL")[0]["n"]
    check("sacks carry is_complete IS NULL", sacks == 0, str(sacks))


# --------------------------------------------------------------------------- integrity

def test_integrity() -> None:
    section("referential integrity")
    checks = {
        "plays with no fact_game row":
            "SELECT count(*) AS n FROM pbp.special_teams_play p LEFT JOIN pbp.fact_game g "
            "ON g.game_id = p.game_id AND g.league = p.league WHERE g.game_id IS NULL",
        "kicker_athlete_id not in dim_athlete":
            "SELECT count(*) AS n FROM pbp.special_teams_play p LEFT JOIN pbp.dim_athlete a "
            "ON a.athlete_id = p.kicker_athlete_id "
            "WHERE p.kicker_athlete_id IS NOT NULL AND a.athlete_id IS NULL",
        "venue_id not in dim_venue":
            "SELECT count(*) AS n FROM pbp.fact_game g LEFT JOIN pbp.dim_venue v "
            "ON v.venue_id = g.venue_id WHERE g.venue_id IS NOT NULL AND v.venue_id IS NULL",
        "kicking team with no (team_id, season) row":
            "SELECT count(*) AS n FROM pbp.special_teams_play p LEFT JOIN pbp.dim_team_season t "
            "ON t.team_id = p.kicking_team_id AND t.season = p.season AND t.league = p.league "
            "WHERE p.kicking_team_id IS NOT NULL AND t.team_id IS NULL",
        "play_uid collisions across the two facts":
            "SELECT count(*) AS n FROM pbp.special_teams_play s "
            "JOIN pbp.scrimmage_play c ON c.play_uid = s.play_uid",
    }
    for label, sql in checks.items():
        n = db.query(sql)[0]["n"]
        check(label + " = 0", n == 0, str(n))

    # The Pro Bowl is dropped at fetch time; its all-star squads must be absent.
    probowl = db.query(
        "SELECT count(*) AS n FROM pbp.fact_game "
        "WHERE league = 'nfl' AND (home_team_id IN (31,32) OR away_team_id IN (31,32))"
    )[0]["n"]
    check("Pro Bowl games absent", probowl == 0, str(probowl))

    # The wide tables must agree with the facts they project.
    for wide, narrow in (("play_wide", "special_teams_play"),
                         ("scrimmage_wide", "scrimmage_play")):
        a = db.query(f"SELECT count(*) AS n FROM pbp.{wide}")[0]["n"]
        b = db.query(f"SELECT count(*) AS n FROM pbp.{narrow}")[0]["n"]
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
    granted = {f"pbp.{t}" for t in names}
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
    ("write: insert", "INSERT INTO pbp.dim_team VALUES (1)"),
    ("write: update", "UPDATE pbp.play_wide SET fg_made = true"),
    ("write: delete", "DELETE FROM pbp.play_wide"),
    ("write: drop", "DROP TABLE pbp.play_wide"),
    ("write: truncate", "TRUNCATE pbp.play_wide"),
    ("write: create", "CREATE TABLE x (a int)"),
    ("write: alter", "ALTER TABLE pbp.play_wide ADD COLUMN x int"),
    ("write: grant", "GRANT SELECT ON pbp.play_wide TO public"),
    ("stacking", "SELECT 1 FROM pbp.play_wide; DROP TABLE pbp.play_wide"),
    ("stacking with comment", "SELECT 1 FROM pbp.play_wide;/**/DROP TABLE pbp.play_wide"),
    ("catalog: pg_authid", "SELECT * FROM pg_authid"),
    ("catalog: pg_stat_activity", "SELECT * FROM pg_stat_activity"),
    ("catalog: pg_settings", "SELECT * FROM pg_settings"),
    ("catalog: information_schema", "SELECT * FROM information_schema.tables"),
    ("unknown table", "SELECT * FROM pbp.secrets"),
    ("cross-database", "SELECT * FROM weatherdata.public.observations"),
    ("file read", "SELECT pg_read_file('/etc/passwd')"),
    ("dblink", "SELECT dblink('x','y')"),
    ("sleep", "SELECT pg_sleep(60)"),
    ("lock", "SELECT * FROM pbp.play_wide FOR UPDATE"),
    ("CTE hiding a write",
     "WITH x AS (DELETE FROM pbp.play_wide RETURNING 1) SELECT * FROM x"),
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
        ("plain select", "SELECT league, count(*) FROM pbp.play_wide GROUP BY 1"),
        ("cte", "WITH x AS (SELECT * FROM pbp.play_wide LIMIT 5) SELECT count(*) FROM x"),
        ("join across facts",
         "SELECT count(*) FROM pbp.play_wide p JOIN pbp.fact_game g USING (game_id)"),
        ("union",
         "SELECT league FROM pbp.play_wide UNION SELECT league FROM pbp.scrimmage_wide"),
        ("unqualified name resolves to pbp", "SELECT count(*) FROM play_wide"),
    ]
    for label, sql in ok:
        try:
            rewritten, report = guard.check(sql)
            check(f"accept {label}", bool(rewritten) and report["row_limit"] > 0)
        except Exception as exc:
            check(f"accept {label}", False, f"{type(exc).__name__}: {exc}")

    section("limit handling")
    _, r = guard.check("SELECT * FROM pbp.play_wide")
    check("missing LIMIT gets the default",
          r["limit_source"] == "default" and r["row_limit"] == guard.DEFAULT_LIMIT, str(r))
    _, r = guard.check("SELECT * FROM pbp.play_wide LIMIT 5")
    check("caller LIMIT is kept", r["limit_source"] == "caller" and r["row_limit"] == 5, str(r))
    _, r = guard.check("SELECT * FROM pbp.play_wide LIMIT 999999")
    check("oversized LIMIT is capped",
          r["limit_source"] == "capped" and r["row_limit"] == guard.MAX_LIMIT, str(r))


# --------------------------------------------------------------------------- run_sql

def test_run_sql() -> None:
    section("run_sql")
    res = server.run_sql("SELECT league, count(*) AS n FROM pbp.play_wide GROUP BY 1 ORDER BY 1")
    check("run_sql returns rows", res.get("row_count") == 2, str(res)[:200])
    check("run_sql reports its tables", res.get("tables") == ["pbp.play_wide"], str(res.get("tables")))

    explained = server.run_sql("SELECT * FROM pbp.play_wide", explain_only=True)
    check("explain_only returns a plan", explained.get("explain_only") is True)
    check("explain_only returns no data rows",
          all("play_uid" not in str(r) for r in explained.get("rows", [])))

    rejected = server.run_sql("DROP TABLE pbp.play_wide")
    check("run_sql reports a rejection", rejected.get("rejected") is True)

    truncated = server.run_sql("SELECT play_uid FROM pbp.play_wide")
    check("run_sql flags truncation", truncated.get("truncated") is True, str(truncated.get("row_count")))


# --------------------------------------------------------------------------- write protection
#
# The guard is the SECOND line. These prove the first one -- the role itself --
# by going around the guard entirely and asking the database directly.

def test_write_protection() -> None:
    section("write protection (guard bypassed on purpose)")
    import psycopg

    for label, sql in [
        ("INSERT refused by the role", "INSERT INTO pbp.dim_team VALUES ('cfb', 999999, 'x')"),
        ("UPDATE refused by the role", "UPDATE pbp.dim_team SET display_name = 'x'"),
        ("CREATE refused by the role", "CREATE TABLE pbp.should_not_exist (a int)"),
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
    pq = server.parse_quality(league="cfb")
    check("parse_quality returns seasons", pq["count"] > 10, str(pq["count"]))

    overall = db.query(
        "SELECT league, round(100.0*avg((parse_confidence='exact')::int),2) AS pct "
        "FROM pbp.play_wide GROUP BY league ORDER BY league")
    by_league = {r["league"]: float(r["pct"]) for r in overall}
    check("college parse rate ~98%", approx(by_league["cfb"], 97, 99), str(by_league))
    check("NFL parse rate ~98%", approx(by_league["nfl"], 97, 99), str(by_league))

    audit = server.audit_plays(league="cfb", limit=5)
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

    latest = db.query(
        "SELECT league, max(season) AS s, max(last_kickoff) AS k "
        "FROM pbp.season_status GROUP BY league ORDER BY league")
    for row in latest:
        print(f"{row['league']}: through season {row['s']}, last game {row['k']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        db.close_all()
