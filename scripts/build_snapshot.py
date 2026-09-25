"""Materialise ONE LEAGUE's warehouse into a local DuckDB file the apps read.

    python scripts/build_snapshot.py                 # both -> pbp_cfb.duckdb, pbp_nfl.duckdb
    python scripts/build_snapshot.py --league nfl    # one
    python scripts/build_snapshot.py --db pbp        # different source database

The apps read only these files, so they start instantly and keep working when Postgres is
down. DuckDB's postgres extension does the extract, so there is no ORM and no driver
dependency -- ATTACH, then CREATE TABLE AS.

ONE FILE PER LEAGUE, AND NO `league` COLUMN IN EITHER. College football and the NFL are
answered separately, the way ESPN segregates them; `sql/split_leagues.sql` does the same
thing inside Postgres and this is its local twin. A combined snapshot was the last place a
league-blind query could still be written, and it carried the bug that split fixes:
dim_athlete's primary_team_id is the modal team of a COMBINED career, and because team ids
collide between the corpora, resolving Patrick Mahomes' id 12 against college returned
"Arizona Wildcats" -- a real team, a wrong answer, and entirely plausible on screen.

THIS FILE USED TO REBUILD THE WIDE JOINS ITSELF, and that is the other thing that changed.
It carried its own ~200-line copies of the play and scrimmage projections, kept
"deliberately identical" to sql/wide_tables.sql by hand, with a comment warning that the two
drifting apart was the most likely way for the pair to start lying. They drifted the moment
the warehouse was split. Now the projections are read straight out of `cfb.*` / `nfl.*`,
which sql/split_leagues.sql already built and scripts/verify_split.py already proved
faithful -- so there is exactly one definition of `play` and it lives in SQL.

Naming: the Postgres tables are `play_wide` and `scrimmage_wide`; here they are `play` and
`scrimmage`, which is what the apps have always called them. Everything else keeps its name.
"""
import argparse, os, sys, time

import duckdb

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEAGUES = ("cfb", "nfl")


def out_path(league: str) -> str:
    return os.path.join(HOME, "data", "out", f"pbp_{league}.duckdb")


# destination name -> source table in the league's schema. Straight copies: every join was
# resolved when split_leagues.sql built the wide tables.
COPIES = {
    "play":              "play_wide",
    "scrimmage":         "scrimmage_wide",
    "drive":             "drive",
    "fact_game":         "fact_game",
    "play_athlete":      "play_athlete",
    "scrimmage_athlete": "scrimmage_athlete",
    "dim_athlete":       "dim_athlete",
    "dim_team":          "dim_team",
    "dim_team_season":   "dim_team_season",
    "dim_conference":    "dim_conference",
    "dim_venue":         "dim_venue",
    "season_status":     "season_status",
}


def build(db: str, league: str, out: str) -> None:
    if os.path.exists(out):
        os.remove(out)
    con = duckdb.connect(out)
    con.execute("INSTALL postgres; LOAD postgres;")
    # The scanner parallelises a table read by opening one COPY stream per ctid range, up to
    # pg_connection_limit (64 by default). At 321k rows that is fine; at 1.5M it exhausted
    # the machine's socket buffers and Postgres killed the transfer with "No buffer space
    # available" / "connection to client lost". The extract is I/O bound on one local disk
    # anyway, so the cap costs nothing measurable.
    con.execute("SET pg_connection_limit = 4")
    con.execute(f"ATTACH '{'dbname=' + db}' AS pg (TYPE POSTGRES, READ_ONLY)")

    counts = {}
    for name, src in COPIES.items():
        t0 = time.time()
        con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM pg.{league}.{src}")
        n = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        counts[name] = n
        print(f"  {name:<18}{n:>10,} rows   {time.time() - t0:5.1f}s")

    # THE GUARD THAT MATTERS HERE. A league column in this file means the source schema was
    # not what it claimed, and a combined snapshot is exactly what this rewrite removes.
    for name in COPIES:
        cols = [r[0] for r in con.execute(f"DESCRIBE {name}").fetchall()]
        stray = [c for c in cols if "league" in c.lower()]
        if stray:
            con.close()
            sys.exit(f"snapshot aborted: {name} carries {stray} -- the source is not a "
                     f"single-league schema. Rebuild {league}.* with sql/split_leagues.sql.")

    live = con.execute("SELECT season, games, plays FROM season_status "
                       "WHERE is_in_progress ORDER BY season").fetchall()
    print("  season in progress:      "
          + (", ".join(f"{y} ({g:,} games, {pl:,} plays)" for y, g, pl in live)
             if live else "none"))

    # Stamp the snapshot so an app can show its age rather than pretend it is live, and name
    # the league so a file cannot be loaded as the wrong corpus.
    con.execute("CREATE OR REPLACE TABLE snapshot_meta AS SELECT now() AS built_at, "
                "? AS source_db, ? AS league, ? AS play_rows, ? AS scrimmage_rows",
                [db, league, counts["play"], counts["scrimmage"]])
    con.execute("DETACH pg")
    con.close()
    print(f"  {out}  ({os.path.getsize(out) / 1e6:.1f} MB)\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="pbp")
    ap.add_argument("--league", choices=LEAGUES, action="append",
                    help="repeatable; default is both")
    a = ap.parse_args()
    try:
        for lg in (a.league or list(LEAGUES)):
            print(f"{lg}:")
            build(a.db, lg, out_path(lg))
    except duckdb.Error as e:
        sys.exit(f"snapshot failed: {e}")
