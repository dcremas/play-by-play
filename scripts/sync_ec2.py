"""Push the warehouse from local Postgres to the EC2 mirror.

    python scripts/sync_ec2.py                      # both leagues, whole window
    python scripts/sync_ec2.py --season 2026        # the weekly in-season push
    python scripts/sync_ec2.py --league nfl         # one league only
    python scripts/sync_ec2.py --skip-transfer      # CSVs already staged
    python scripts/sync_ec2.py --dry-run            # print the plan, do nothing

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
Local Postgres stays the system of record. This makes EC2 a MIRROR of it, so the
MCP server (and anything else without a tunnel to this laptop) can read the same
corpus. Nothing here builds data -- it replays the same loaders the local
warehouse was built with, against a second target.

**It syncs the CSV EXTRACTS, not Postgres itself.** That is deliberate and it is
the only version-safe route: local runs PostgreSQL 18 and the EC2 box runs 16, so
a pg_dump from here cannot be restored there -- dumping a newer server and
restoring into an older one is the unsupported direction. The extracts in
data/out/ are the same files the local load read, so replaying them reproduces the
same tables rather than approximating them. The schema was applied once from a
schema-only dump with the single PG17+ line (`SET transaction_timeout`) stripped.

So: run the local pipeline first (runbook A, B or C in README.md), confirm it,
then run this. If data/out/ is stale, this faithfully mirrors stale data.

WHY THE LOAD RUNS ON THE BOX RATHER THAN THROUGH THE TUNNEL
-----------------------------------------------------------
sql/load_league.sql uses server-side `COPY ... FROM '<path>'`, not `\\copy`, and
its own header explains why: psql's `\\copy` does not interpolate `:'variables'`
and silently loads ZERO rows instead of erroring. So the CSVs have to be on the
database server's filesystem, and the loader has to run there. The loaders'
existing `-v root=` parameter is what makes this work with no change to them --
they were already written to take their path prefix from outside.

The staging directory lives on the Postgres EBS volume (/var/lib/pgsql/staging)
rather than the root volume, because the root volume has ~8 GB free and the
extracts are ~940 MB with the warehouse itself on the other disk.
"""
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
SSH_HOST = "awsvm"                       # see ~/.ssh/config
REMOTE_ROOT = "/var/lib/pgsql/staging"   # on the Postgres volume, owned by postgres
REMOTE_DB = "pbp"

# The extracts each load reads. Season-scoped runs read the `_<season>` variants,
# which build_table.py and friends write alongside the full ones.
FULL_EXTRACTS = [
    "st_plays{L}.csv", "scrimmage_plays{L}.csv", "scrimmage_athlete{L}.csv",
    "drives{L}.csv", "fact_game{L}.csv",
]
SEASON_EXTRACTS = [
    "st_plays{L}_{Y}.csv", "scrimmage_plays{L}_{Y}.csv", "scrimmage_athlete{L}_{Y}.csv",
    "drives{L}_{Y}.csv", "fact_game{L}.csv",
]
DIM_EXTRACTS = ["dim_venue{L}.csv", "dim_team{L}.csv", "dim_conference{L}.csv",
                "dim_team_season{L}.csv"]
# The athlete link spans both leagues and both facts, so it is never league-scoped.
ATHLETE_EXTRACTS = ["dim_athlete.csv", "play_athlete.csv", "play_athlete_nfl.csv",
                    "play_athlete_wide.csv", "play_athlete_wide_nfl.csv",
                    # The per-league dimensions sql/split_leagues.sql loads. Built by
                    # `build_dims.py athlete --leagues <one league>`; they are NOT filters of
                    # dim_athlete.csv and cannot be derived from it -- see that file's header
                    # for why (primary_team_id is chosen across a combined career, and team
                    # ids collide between the leagues).
                    "dim_athlete_cfb.csv", "dim_athlete_nfl.csv"]


def suffix(league: str) -> str:
    """College extracts keep their historical bare names; every other league is suffixed."""
    return "" if league == "cfb" else f"_{league}"


def run(cmd: list[str], dry: bool, label: str = "") -> None:
    printable = " ".join(shlex.quote(c) for c in cmd)
    print(f"\n$ {printable}" if not label else f"\n[{label}]\n$ {printable}")
    if dry:
        return
    t0 = time.time()
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(f"\nFAILED ({result.returncode}) after {time.time() - t0:.0f}s: {label or printable}")
    print(f"  ok  {time.time() - t0:.0f}s")


def remote_psql(sql_file: str, *vars_: str) -> list[str]:
    """Run a SQL file on the box as postgres.

    `cd` happens inside the postgres shell, not the ssh shell: ec2-user cannot
    traverse /var/lib/pgsql, which is mode 700 and owned by postgres.
    """
    var_args = " ".join(f"-v {shlex.quote(v)}" for v in vars_)
    inner = (
        f"cd {REMOTE_ROOT} && psql -d {REMOTE_DB} -v ON_ERROR_STOP=on "
        f"-v root={REMOTE_ROOT} {var_args} -f {sql_file}"
    )
    return ["ssh", SSH_HOST, f"sudo -u postgres bash -c {shlex.quote(inner)}"]


def transfer(files: list[str], dry: bool) -> None:
    """Copy extracts to the staging dir on the Postgres volume.

    --rsync-path="sudo rsync" so the remote side can write into a directory
    ec2-user cannot even list. macOS ships openrsync, which does NOT support
    --chown or --info, so ownership is fixed in a separate step afterwards.
    """
    missing = [f for f in files if not (HOME / "data" / "out" / f).exists()]
    if missing:
        sys.exit(
            "These extracts are missing from data/out/ -- run the local pipeline "
            "first (README.md runbooks):\n  " + "\n  ".join(missing)
        )
    sources = [str(HOME / "data" / "out" / f) for f in files]
    total_mb = sum((HOME / "data" / "out" / f).stat().st_size for f in files) / 1e6
    run(
        ["rsync", "-az", "--rsync-path=sudo rsync", *sources,
         f"{SSH_HOST}:{REMOTE_ROOT}/data/out/"],
        dry, f"transfer {len(files)} extracts, {total_mb:.0f} MB",
    )
    run(["ssh", SSH_HOST, f"sudo chown -R postgres:postgres {REMOTE_ROOT}"], dry,
        "fix ownership")


def remote_psql_path(path: str, label_vars: tuple[str, ...] = ()) -> list[str]:
    """Run a SQL file by absolute remote path, as postgres, with no `root` variable.

    Used for the two mcp_server files, which take no path parameters.
    """
    var_args = " ".join(f"-v {shlex.quote(v)}" for v in label_vars)
    inner = f"psql -d {REMOTE_DB} -v ON_ERROR_STOP=on {var_args} -f {path}"
    return ["ssh", SSH_HOST, f"sudo -u postgres bash -c {shlex.quote(inner)}"]


def transfer_sql(dry: bool) -> None:
    """Ship both SQL trees: the loaders, and the MCP's grant/comment scripts.

    The second pair matters as much as the first -- sql/split_leagues.sql does
    DROP SCHEMA ... CASCADE, and privileges and comments go with a dropped table.
    """
    run(["rsync", "-az", "--rsync-path=sudo rsync", f"{HOME}/sql/",
         f"{SSH_HOST}:{REMOTE_ROOT}/sql/"], dry, "transfer sql/")
    run(["rsync", "-az", "--rsync-path=sudo rsync",
         f"{HOME}/mcp_server/setup_role_pbp.sql", f"{HOME}/mcp_server/comment_tables.sql",
         f"{SSH_HOST}:{REMOTE_ROOT}/mcp_setup/"], dry, "transfer mcp_server/*.sql")
    run(["ssh", SSH_HOST, f"sudo chown -R postgres:postgres {REMOTE_ROOT}"], dry,
        "fix ownership")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--league", choices=("cfb", "nfl"), action="append",
                    help="repeatable; default is both")
    ap.add_argument("--season", type=int,
                    help="in-season mode: replace only this season within each league")
    ap.add_argument("--skip-transfer", action="store_true",
                    help="the extracts are already staged on the box")
    ap.add_argument("--skip-athletes", action="store_true",
                    help="skip the shared athlete dimension (it spans both leagues, "
                         "so it only needs running once per sync)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    leagues = args.league or ["cfb", "nfl"]
    dry = args.dry_run

    print(f"target: {SSH_HOST}:{REMOTE_DB}  leagues: {', '.join(leagues)}"
          + (f"  season: {args.season}" if args.season else "  (whole window)"))

    # ------------------------------------------------------------ 1. stage the files
    if not args.skip_transfer:
        wanted: list[str] = []
        for league in leagues:
            L = suffix(league)
            template = SEASON_EXTRACTS if args.season else FULL_EXTRACTS
            wanted += [f.format(L=L, Y=args.season) for f in template]
            wanted += [f.format(L=L) for f in DIM_EXTRACTS]
        if not args.skip_athletes:
            wanted += ATHLETE_EXTRACTS
        transfer(sorted(set(wanted)), dry)
    transfer_sql(dry)

    # ------------------------------------------------------------ 2. the facts
    # One league per run, exactly as the local loader works. Everything inside
    # load_league.sql is scoped `WHERE league = :'league'`, so loading one corpus
    # cannot touch the other.
    for league in leagues:
        season_arg = [f"season={args.season}"] if args.season else []
        run(remote_psql("sql/load_league.sql", f"league={league}", *season_arg),
            dry, f"load {league}" + (f" season {args.season}" if args.season else ""))

    # ------------------------------------------------------------ 3. the athlete link
    # Spans BOTH leagues and BOTH facts, so it runs once, after every league is in.
    if not args.skip_athletes:
        run(remote_psql("sql/load_athletes_league.sql"), dry, "athlete dimension")

    # ------------------------------------------------------------ 4. the serving layer
    #
    # BUILD, GRANT, COMMENT, THEN SWAP -- per league, in that order.
    #
    # The rebuild happens in `<league>_next`, a schema nothing reads, and sql/swap_schema.sql
    # renames it into place in one transaction. Measured: the college rebuild is 1m39s on
    # this box and the rename is 0.76 ms, so a weekly refresh costs a public endpoint
    # effectively nothing instead of a minute and a half of errors. It used to DROP the live
    # schema first, and DROP SCHEMA takes ACCESS EXCLUSIVE -- it queues behind running
    # queries and every new query queues behind it, so the real outage was longer than the
    # rebuild.
    #
    # Grants and comments go on the STAGING schema. They live on table oids and survive the
    # rename, so the corpus is readable and documented the instant it becomes live. Applying
    # them after the swap would leave a window where the schema exists and the servers
    # cannot read it -- the same bug in a smaller costume.
    #
    # Both leagues are rebuilt whichever league was loaded: dim_venue and the athlete
    # dimension are shared upstream, so a college-only sync can still change what the NFL
    # schema should contain.
    for league in ("cfb", "nfl"):
        stage = f"{league}_next"
        run(remote_psql("sql/split_leagues.sql", f"league={league}", f"schema={stage}"),
            dry, f"build {stage}")
        run(remote_psql_path(f"{REMOTE_ROOT}/mcp_setup/setup_role_pbp.sql",
                             (f"schemas={stage}",)), dry, f"grant {stage}")
        run(remote_psql_path(f"{REMOTE_ROOT}/mcp_setup/comment_tables.sql",
                             (f"schema={stage}",)), dry, f"comment {stage}")
        run(remote_psql("sql/swap_schema.sql", f"live={league}", f"stage={stage}"),
            dry, f"swap {stage} -> {league}")

    # ------------------------------------------------------------ 5. the role itself
    # Re-asserted after the swaps, not before: this is the authoritative definition of
    # pbp_ro -- its attributes, session defaults and connection limit -- and the per-schema
    # grants above are the same file called with one schema named. Running it here with no
    # `schemas` re-grants both live corpora, which is a no-op on a healthy database and the
    # repair if a swap was interrupted.
    run(remote_psql_path(f"{REMOTE_ROOT}/mcp_setup/setup_role_pbp.sql"),
        dry, "re-assert pbp_ro")

    # ------------------------------------------------------------ 6. reconcile
    if not dry:
        reconcile()

    print("\nDone. Verify the tools with:")
    print("  cd mcp_server && ./.venv/bin/python -m pbp_mcp.selftest")


# The check that actually proves this is a mirror rather than an approximation:
# count the same things on both sides and require them to agree exactly. Everything
# upstream can succeed and still leave a silent shortfall -- a missing extract, a
# season predicate that matched nothing, a load that replaced less than intended.
# THE BRIDGES ARE COUNTED THROUGH THEIR FACT, NOT RAW, and that is not a detail.
#
# sql/load_league.sql deletes bridge rows by joining the fact:
#
#     DELETE FROM pbp.scrimmage_athlete sa USING pbp.scrimmage_play sp
#      WHERE sa.play_uid = sp.play_uid AND sp.league = :'league'
#
# and it runs AFTER the fact has already been replaced. So a bridge row whose
# play_uid is no longer in the fact cannot be matched and therefore cannot be
# deleted -- it is stranded permanently. That happens whenever a play_uid changes
# under a re-parse, which the `#n` duplicate-sequence suffix does by design.
#
# Local Postgres has accumulated 73 such orphans (two 2026 week-1 college games,
# 11 of them `#n`-suffixed). A MIRROR LOADED FROM SCRATCH HAS NONE, because the
# orphans were never in the extracts -- they are an artefact of loading in place,
# repeatedly, over time.
#
# So a raw count would report a 73-row "failure" on every single sync, for a
# difference that means the mirror is CLEANER than the source. Counting through
# the fact compares the rows that actually carry meaning, and the orphans are
# reported separately below rather than hidden.
RECONCILE_SQL = """
SELECT 'special_teams_play' AS t, league, count(*) AS n FROM pbp.special_teams_play GROUP BY 2
UNION ALL SELECT 'scrimmage_play',    league, count(*) FROM pbp.scrimmage_play    GROUP BY 2
UNION ALL SELECT 'scrimmage_athlete/linked', '(all)', count(*)
     FROM pbp.scrimmage_athlete b JOIN pbp.scrimmage_play p ON p.play_uid = b.play_uid
UNION ALL SELECT 'play_athlete/linked', '(all)', count(*)
     FROM pbp.play_athlete b JOIN pbp.special_teams_play p ON p.play_uid = b.play_uid
UNION ALL SELECT 'drive',             league, count(*) FROM pbp.drive             GROUP BY 2
UNION ALL SELECT 'fact_game',         league, count(*) FROM pbp.fact_game         GROUP BY 2
UNION ALL SELECT 'dim_athlete',       '(all)', count(*) FROM pbp.dim_athlete
UNION ALL SELECT 'dim_team',          league, count(*) FROM pbp.dim_team          GROUP BY 2
UNION ALL SELECT 'dim_team_season',   league, count(*) FROM pbp.dim_team_season   GROUP BY 2
UNION ALL SELECT 'dim_venue',         '(all)', count(*) FROM pbp.dim_venue
ORDER BY 1, 2
"""

# Informational, never a failure -- see the note above.
ORPHAN_SQL = """
SELECT 'scrimmage_athlete' AS t, '(orphans)' AS k, count(*) AS n
  FROM pbp.scrimmage_athlete b
  LEFT JOIN pbp.scrimmage_play p ON p.play_uid = b.play_uid WHERE p.play_uid IS NULL
UNION ALL
SELECT 'play_athlete', '(orphans)', count(*)
  FROM pbp.play_athlete b
  LEFT JOIN pbp.special_teams_play p ON p.play_uid = b.play_uid WHERE p.play_uid IS NULL
ORDER BY 1
"""


def counts(cmd: list[str], label: str) -> dict[str, int]:
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"reconcile: could not read counts from {label}:\n{out.stderr}")
    result: dict[str, int] = {}
    for line in out.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) == 3:
            result[f"{parts[0]}/{parts[1]}"] = int(parts[2])
    return result


def reconcile() -> None:
    print("\n[reconcile local vs mirror]")
    sql = " ".join(RECONCILE_SQL.split())
    local = counts(["psql", "-d", "pbp", "-tA", "-c", sql], "local")
    remote_inner = f"psql -d {REMOTE_DB} -tA -c {shlex.quote(sql)}"
    remote = counts(
        ["ssh", SSH_HOST, f"sudo -u postgres bash -c {shlex.quote(remote_inner)}"],
        "the mirror",
    )

    keys = sorted(set(local) | set(remote))
    bad = []
    for key in keys:
        a, b = local.get(key), remote.get(key)
        flag = "ok " if a == b else "DIFF"
        if a != b:
            bad.append(key)
        print(f"  {flag}  {key:<34} local={a!s:>10}  mirror={b!s:>10}")

    # Informational: stranded bridge rows. Expected to be >0 locally and 0 on a
    # mirror loaded from scratch. Never a failure -- see the note on RECONCILE_SQL.
    osql = " ".join(ORPHAN_SQL.split())
    o_local = counts(["psql", "-d", "pbp", "-tA", "-c", osql], "local")
    o_remote_inner = f"psql -d {REMOTE_DB} -tA -c {shlex.quote(osql)}"
    o_remote = counts(
        ["ssh", SSH_HOST, f"sudo -u postgres bash -c {shlex.quote(o_remote_inner)}"],
        "the mirror",
    )
    for key in sorted(set(o_local) | set(o_remote)):
        a, b = o_local.get(key, 0), o_remote.get(key, 0)
        if a or b:
            print(f"  note  {key:<34} local={a!s:>10}  mirror={b!s:>10}"
                  "   (stranded bridge rows; the mirror being lower is correct)")

    if bad:
        sys.exit(
            f"\nRECONCILE FAILED on {len(bad)} of {len(keys)} counts: "
            + ", ".join(bad)
            + "\nThe mirror does NOT match local Postgres. Do not trust it until this "
              "is resolved."
        )
    print(f"  all {len(keys)} counts match")


if __name__ == "__main__":
    main()
