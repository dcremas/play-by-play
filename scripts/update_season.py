"""Pull one league's in-progress season forward: ESPN -> CSV -> Postgres -> DuckDB snapshot.

    .venv/bin/python scripts/update_season.py 2026                 college (the default)
    .venv/bin/python scripts/update_season.py 2026 --league nfl    the NFL

The two corpora are updated INDEPENDENTLY and both are live in 2026. Every stage below is
scoped to the league named on the command line; the only thing that spans both is
pbp.dim_athlete, which is one row per person across the two corpora and is therefore always
rebuilt from both.

The backfill path in README's "Adding a season" assumes a season that is over. Run weekly
against a season still being played it is wrong in three ways, and this script is the
counterpart that is not:

  * `fetch_espn.py games` skips the games list if the file exists, so a live season would
    freeze at whatever had finished the first time it ran. Here it is always refreshed.
  * `load_2_insert.sql` TRUNCATEs the whole fact table and needs the game-context enrichment
    and the entire athlete link re-run behind it. Here one season is deleted and re-inserted,
    with its enrichment in the same transaction.
  * Nothing ever revisits a game once fetched, so a late box-score correction is missed.
    Here anything that kicked off inside --refresh-days is re-pulled.

    .venv/bin/python scripts/update_season.py 2026                # the weekly run
    .venv/bin/python scripts/update_season.py 2026 --dry-run      # fetch, build, change nothing
    .venv/bin/python scripts/update_season.py 2026 --skip-conf    # no conference re-fetch

Every stage is idempotent, so an interrupted run is fixed by running it again. Run it from
the repository root: sql/load_dims.sql resolves its CSV paths relative to the caller.

Where the season boundary sits, and why:

  fetched   games list, summaries, participants   season-scoped (--seasons / SEASONS env)
  built     st_plays_<season>.csv                 season-scoped (build_table --seasons)
            scrimmage_plays_<season>.csv,
            scrimmage_athlete_<season>.csv,
            drives_<season>.csv                   season-scoped (build_scrimmage --seasons)
            athletes.json.gz                      GLOBAL, incremental -- only new ids
            dim_team_season, dim_venue, dim_team,
            fact_game, dim_athlete                GLOBAL -- small, and dim_athlete is a
                                                  career aggregate that a season-scoped
                                                  rebuild would corrupt
            play_athlete, play_athlete_wide       GLOBAL per league -- the loader replaces
                                                  both wholesale, and the build walks every
                                                  participants file regardless
  loaded    pbp.special_teams_play                 (league, season)-scoped DELETE + INSERT
            pbp.scrimmage_play, pbp.scrimmage_athlete,
            pbp.drive, pbp.fact_game               (league, season)-scoped DELETE + INSERT
            pbp.dim_team/_season/_conference        league-scoped full replace
            pbp.dim_venue                          UPSERT -- shared across leagues
            pbp.dim_athlete, pbp.play_athlete      GLOBAL full replace, both leagues
"""
import argparse, csv, os, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import league as lg

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
OUT = f"{HOME}/data/out"


def sfx(league):
    """The per-league filename suffix. College keeps its historical bare names."""
    return "" if league == lg.CFB else f"_{league}"


def run(cmd, env=None, cwd=HOME):
    """Run a stage, streaming its output. Any non-zero exit stops the whole update."""
    print(f"\n\033[1m$ {' '.join(cmd)}\033[0m", flush=True)
    t0 = time.time()
    r = subprocess.run(cmd, cwd=cwd, env={**os.environ, **(env or {})})
    if r.returncode:
        sys.exit(f"\nstage failed ({r.returncode}); nothing after this point ran")
    print(f"  [{time.time() - t0:.1f}s]", flush=True)


def psql(db, *args):
    run(["psql", "-d", db, "-v", "ON_ERROR_STOP=1", *args])


def scalar(db, sql):
    r = subprocess.run(["psql", "-d", db, "-tAc", sql], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def csv_rows(path):
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        return sum(1 for _ in f) - 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("season", type=int)
    ap.add_argument("--league", default=lg.CFB, choices=sorted(lg.SPEC),
                    help="which corpus to pull forward (default college). Both are "
                         "in-progress in 2026 and each is updated independently.")
    ap.add_argument("--db", default="pbp")
    ap.add_argument("--refresh-days", type=int, default=21,
                    help="re-pull summaries and participants for games that kicked off "
                         "inside this window, so late stat corrections land (default 21)")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and build every CSV, report what would change in Postgres, "
                         "then stop without touching the database")
    ap.add_argument("--skip-conf", action="store_true",
                    help="skip the ~250-call conference re-fetch; conferences do not move "
                         "mid-season, so this is safe after the season's first run")
    ap.add_argument("--skip-fetch", action="store_true",
                    help="rebuild and reload from summaries already on disk")
    a = ap.parse_args()
    s = a.season
    L = a.league
    x = sfx(L)
    FULL_PLAYS = f"{OUT}/st_plays{x}.csv"
    FULL_SCRIM = f"{OUT}/scrimmage_plays{x}.csv"
    FULL_SCRIM_BRIDGE = f"{OUT}/scrimmage_athlete{x}.csv"
    plays_csv = f"{OUT}/st_plays{x}_{s}.csv"
    scrim_csv = f"{OUT}/scrimmage_plays{x}_{s}.csv"
    scrim_bridge_csv = f"{OUT}/scrimmage_athlete{x}_{s}.csv"
    drives_csv = f"{OUT}/drives{x}_{s}.csv"

    if not os.path.exists(FULL_PLAYS):
        sys.exit(f"{FULL_PLAYS} is missing. The athlete dimension is a career aggregate and "
                 f"is derived from that full extract plus this season's; without it every "
                 f"returning player would get first_season={s}. Run build_table.py first.")

    # ---------------------------------------------------------------- fetch
    if not a.skip_fetch:
        run([PY, "scripts/fetch_espn.py", "games", "--refresh", "--league", L],
            env={"SEASONS": str(s)})
        run([PY, "scripts/fetch_espn.py", "summaries", "--refresh-days",
             str(a.refresh_days), "--league", L], env={"SEASONS": str(s)})
        run([PY, "scripts/fetch_participants.py", "--seasons", str(s),
             "--refresh-days", str(a.refresh_days), "--league", L])

    # ---------------------------------------------------------------- build
    if not a.skip_conf:
        run([PY, "scripts/build_dims.py", "conf", "--league", L])
    run([PY, "scripts/build_dims.py", "venue", "--league", L])
    run([PY, "scripts/build_table.py", "--league", L, "--seasons", str(s),
         "--out", plays_csv])
    run([PY, "scripts/build_scrimmage.py", "--league", L, "--seasons", str(s),
         "--out", scrim_csv, "--out-bridge", scrim_bridge_csv,
         "--out-drives", drives_csv])
    # New players appear every week. This only fetches ids the store has not seen, so in a
    # steady week it is a few hundred calls, not 63,000.
    run([PY, "scripts/fetch_athletes.py", "--league", L, "--sources",
         f"play_athlete{x}_{s}.csv", scrim_bridge_csv, FULL_SCRIM_BRIDGE,
         f"play_athlete{x}.csv"])
    # The DIMENSION spans both leagues even when only one is being pulled forward: it is
    # one row per person over both corpora, and rebuilding it from this league alone would
    # drop every athlete who only appears in the other one.
    # NO --only-season, deliberately, and this is a change from the pre-NFL pipeline.
    #
    # That flag narrowed the BRIDGE output to one season, because the old apply script
    # TRUNCATEd pbp.play_athlete and reloaded it from the global file -- so writing one
    # season under the global name would have destroyed the other twelve. The replacement,
    # sql/load_athletes_league.sql, replaces the bridge wholesale from the global files for
    # both leagues, so it wants the complete extract.
    #
    # It costs nothing. stage_athlete walks every participants file on disk either way --
    # all 13,767 of them across both leagues -- and --only-season only ever filtered what
    # got WRITTEN. The dimension was always global, because it is a career aggregate.
    run([PY, "scripts/build_dims.py", "athlete", "--league", L,
         "--leagues", *lg.SPEC,
         "--plays", FULL_PLAYS, plays_csv,
         "--scrim-fact", FULL_SCRIM, scrim_csv,
         "--scrim-bridge", FULL_SCRIM_BRIDGE, scrim_bridge_csv])

    # A skipped conference fetch is only safe once the season is already in the CSV. On the
    # first run of a new season it is not, and load_dims.sql would replace dim_team_season
    # with a file that has no rows for it -- silently emptying every conference on every
    # 2026 play, because build_snapshot joins on (team_id, season).
    with open(f"{OUT}/dim_team_season{x}.csv") as f:
        ts = sum(1 for r in csv.DictReader(f) if int(r["season"]) == s)
    if ts == 0:
        sys.exit(f"dim_team_season.csv has no rows for {s}. Re-run without --skip-conf; "
                 f"loading it as-is would leave every {s} play with a NULL conference.")
    print(f"\ndim_team_season: {ts:,} team-seasons for {s}")

    # ---------------------------------------------------------------- report
    built = csv_rows(plays_csv)
    built_scrim = csv_rows(scrim_csv)
    scope = f"season = {s} AND league = '{L}'"
    live_scrim = scalar(a.db, f"SELECT count(*) FROM pbp.scrimmage_play WHERE {scope}")
    live = scalar(a.db, f"SELECT count(*) FROM pbp.special_teams_play WHERE {scope}")
    total = scalar(a.db, f"SELECT count(*) FROM pbp.special_teams_play "
                         f"WHERE league = '{L}'")
    games = scalar(a.db, f"SELECT count(DISTINCT game_id) FROM pbp.special_teams_play "
                         f"WHERE {scope}")
    print(f"\n\033[1m{L} {s}\033[0m  in Postgres now: {live or '?'} plays over {games or '?'} games"
          f"\n      built from ESPN: {built:,} plays"
          f"\n      other {L} seasons ({int(total or 0) - int(live or 0):,} plays) are "
          f"untouched by this load, as is the other league entirely")
    print(f"      scrimmage: {live_scrim or '?'} in Postgres, {built_scrim:,} built from ESPN")

    if a.dry_run:
        print(f"\n--dry-run: the database was not modified. To apply:\n"
              f"  psql -d {a.db} -v league={L} -v season={s} -f sql/load_league.sql\n"
              f"  psql -d {a.db} -f sql/load_athletes_league.sql\n"
              f"  .venv/bin/python scripts/build_snapshot.py")
        return

    # ---------------------------------------------------------------- load
    # ONE loader, scoped to (league, season). It replaces the five single-league scripts
    # this script used to drive, and it had to: those TRUNCATE, so running them now would
    # delete the OTHER league to make room for this one.
    #
    # The load, the dimension refresh and the game-context enrichment are all inside that
    # one transaction, which is why there is no separate enrich step here any more.
    psql(a.db, "-v", f"league={L}", "-v", f"season={s}", "-f", "sql/load_league.sql")

    # The athlete dimension is global across BOTH leagues and both facts, so it is replaced
    # wholesale from the extract build_dims just wrote. The bridge is per league and this
    # reloads both, which is correct and cheap.
    psql(a.db, "-f", "sql/load_athletes_league.sql")

    # ---------------------------------------------------------------- snapshot
    run([PY, "scripts/build_snapshot.py", "--db", a.db])
    print(f"\n\033[1m{s} is in.\033[0m Both apps read the snapshot, so they pick it up on "
          f"restart. A season whose last game kicked off inside 30 days is marked in "
          f"progress and is held out of the fitted baselines -- see season_status.")


if __name__ == "__main__":
    main()
