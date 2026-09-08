"""Pull one in-progress season forward: ESPN -> CSV -> Postgres -> DuckDB snapshot.

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
            play_athlete, play_athlete_wide       season-scoped (--only-season)
  loaded    st.special_teams_play                 season-scoped DELETE + INSERT
            st.scrimmage_play, st.scrimmage_athlete,
            st.drive                              season-scoped DELETE + INSERT
            st.play_athlete + the three id cols   season-scoped
            everything else                       full replace
"""
import argparse, csv, os, subprocess, sys, time

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
OUT = f"{HOME}/data/out"
FULL_PLAYS = f"{OUT}/st_plays.csv"
FULL_SCRIM = f"{OUT}/scrimmage_plays.csv"
FULL_SCRIM_BRIDGE = f"{OUT}/scrimmage_athlete.csv"


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
    ap.add_argument("--db", default="cfb")
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
    plays_csv = f"{OUT}/st_plays_{s}.csv"
    scrim_csv = f"{OUT}/scrimmage_plays_{s}.csv"
    scrim_bridge_csv = f"{OUT}/scrimmage_athlete_{s}.csv"
    drives_csv = f"{OUT}/drives_{s}.csv"

    if not os.path.exists(FULL_PLAYS):
        sys.exit(f"{FULL_PLAYS} is missing. The athlete dimension is a career aggregate and "
                 f"is derived from that full extract plus this season's; without it every "
                 f"returning player would get first_season={s}. Run build_table.py first.")

    # ---------------------------------------------------------------- fetch
    if not a.skip_fetch:
        run([PY, "scripts/fetch_espn.py", "games", "--refresh"], env={"SEASONS": str(s)})
        run([PY, "scripts/fetch_espn.py", "summaries", "--refresh-days", str(a.refresh_days)],
            env={"SEASONS": str(s)})
        run([PY, "scripts/fetch_participants.py", "--seasons", str(s),
             "--refresh-days", str(a.refresh_days)])

    # ---------------------------------------------------------------- build
    if not a.skip_conf:
        run([PY, "scripts/build_dims.py", "conf"])
    run([PY, "scripts/build_dims.py", "venue"])
    run([PY, "scripts/build_table.py", "--seasons", str(s), "--out", plays_csv])
    run([PY, "scripts/build_scrimmage.py", "--seasons", str(s), "--out", scrim_csv,
         "--out-bridge", scrim_bridge_csv, "--out-drives", drives_csv])
    # New players appear every week. This only fetches ids the store has not seen, so in a
    # steady week it is a few hundred calls, not 63,000.
    run([PY, "scripts/fetch_athletes.py", "--sources",
         f"play_athlete_{s}.csv", scrim_bridge_csv, FULL_SCRIM_BRIDGE, "play_athlete.csv"])
    run([PY, "scripts/build_dims.py", "athlete",
         "--plays", FULL_PLAYS, plays_csv,
         "--scrim-fact", FULL_SCRIM, scrim_csv,
         "--scrim-bridge", FULL_SCRIM_BRIDGE, scrim_bridge_csv,
         "--only-season", str(s)])

    # A skipped conference fetch is only safe once the season is already in the CSV. On the
    # first run of a new season it is not, and load_dims.sql would replace dim_team_season
    # with a file that has no rows for it -- silently emptying every conference on every
    # 2026 play, because build_snapshot joins on (team_id, season).
    with open(f"{OUT}/dim_team_season.csv") as f:
        ts = sum(1 for r in csv.DictReader(f) if int(r["season"]) == s)
    if ts == 0:
        sys.exit(f"dim_team_season.csv has no rows for {s}. Re-run without --skip-conf; "
                 f"loading it as-is would leave every {s} play with a NULL conference.")
    print(f"\ndim_team_season: {ts:,} team-seasons for {s}")

    # ---------------------------------------------------------------- report
    built = csv_rows(plays_csv)
    built_scrim = csv_rows(scrim_csv)
    live_scrim = scalar(a.db, f"SELECT count(*) FROM st.scrimmage_play WHERE season = {s}")
    live = scalar(a.db, f"SELECT count(*) FROM st.special_teams_play WHERE season = {s}")
    total = scalar(a.db, "SELECT count(*) FROM st.special_teams_play")
    games = scalar(a.db, f"SELECT count(DISTINCT game_id) FROM st.special_teams_play "
                         f"WHERE season = {s}")
    print(f"\n\033[1m{s}\033[0m  in Postgres now: {live or '?'} plays over {games or '?'} games"
          f"\n      built from ESPN: {built:,} plays"
          f"\n      other seasons ({int(total or 0) - int(live or 0):,} plays) are untouched by this load")
    print(f"      scrimmage: {live_scrim or '?'} in Postgres, {built_scrim:,} built from ESPN")

    if a.dry_run:
        print(f"\n--dry-run: the database was not modified. To apply:\n"
              f"  psql -d {a.db} -f sql/load_dims.sql\n"
              f"  psql -d {a.db} -f sql/load_1_stage.sql\n"
              f"  psql -d {a.db} -c \"\\copy st.stg_plays FROM '{plays_csv}' WITH (FORMAT csv, HEADER true)\"\n"
              f"  psql -d {a.db} -v season={s} -f sql/load_3_season.sql\n"
              f"  psql -d {a.db} -f sql/load_scrimmage_1_stage.sql\n"
              f"  psql -d {a.db} -f sql/load_bridge_drive_1_stage.sql\n"
              f"  ... three \\copy commands, then\n"
              f"  psql -d {a.db} -v season={s} -f sql/load_scrimmage_3_season.sql\n"
              f"  ... then the athlete steps, then build_snapshot.py")
        return

    # ---------------------------------------------------------------- load
    psql(a.db, "-f", "sql/load_dims.sql")

    psql(a.db, "-f", "sql/load_1_stage.sql")
    psql(a.db, "-c", f"\\copy st.stg_plays FROM '{plays_csv}' WITH (FORMAT csv, HEADER true)")
    psql(a.db, "-v", f"season={s}", "-f", "sql/load_3_season.sql")

    psql(a.db, "-f", "sql/load_scrimmage_1_stage.sql")
    psql(a.db, "-f", "sql/load_bridge_drive_1_stage.sql")
    for tbl, f in (("stg_scrimmage", scrim_csv), ("stg_scrimmage_athlete", scrim_bridge_csv),
                   ("stg_drive", drives_csv)):
        psql(a.db, "-c", f"\\copy st.{tbl} FROM '{f}' WITH (FORMAT csv, HEADER true)")
    psql(a.db, "-v", f"season={s}", "-f", "sql/load_scrimmage_3_season.sql")

    psql(a.db, "-f", "sql/load_athletes_1_stage.sql")
    # dim_athlete.csv is the global career dimension; the other two are this season only and
    # carry the season in their name, so they can never be mistaken for the global pair that
    # sql/load_athletes_2_apply.sql TRUNCATEs and reloads.
    for tbl, f in (("stg_dim_athlete", "dim_athlete.csv"),
                   ("stg_play_athlete", f"play_athlete_{s}.csv"),
                   ("stg_play_athlete_wide", f"play_athlete_wide_{s}.csv")):
        psql(a.db, "-c", f"\\copy st.{tbl} FROM '{OUT}/{f}' WITH (FORMAT csv, HEADER true)")
    psql(a.db, "-v", f"season={s}", "-f", "sql/load_athletes_3_season.sql")

    # ---------------------------------------------------------------- snapshot
    run([PY, "scripts/build_snapshot.py", "--db", a.db])
    print(f"\n\033[1m{s} is in.\033[0m Both apps read the snapshot, so they pick it up on "
          f"restart. A season whose last game kicked off inside 30 days is marked in "
          f"progress and is held out of the fitted baselines -- see season_status.")


if __name__ == "__main__":
    main()
