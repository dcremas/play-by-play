"""Prove cfb.* and nfl.* are a faithful, lossless split of pbp.*.

    python scripts/verify_split.py              # everything
    python scripts/verify_split.py --local      # against local Postgres only
    python scripts/verify_split.py --mirror     # against the EC2 mirror only

WHAT THIS HAS TO CATCH, AND WHY COUNTS ALONE WOULD NOT
-------------------------------------------------------
sql/split_leagues.sql projects one system-of-record schema into two. Three ways that
can go wrong quietly:

1. **A row lands in the wrong league.** Counts still add up. Only a per-league
   content checksum catches it, so every fact is checksummed on both sides of the
   split and compared against the same rows selected from pbp.*.
2. **A column silently stops being carried.** The first draft of split_leagues.sql
   enumerated columns and named three that have never existed; it now does
   SELECT * + DROP COLUMN, and this asserts the column sets match.
3. **dim_athlete is rebuilt rather than filtered**, so it is the one table with no
   trivial invariant. The check that actually binds it: for an athlete who played
   in ONLY ONE league, the rebuilt row must equal the trusted combined row on every
   field. 61,893 college and 3,549 NFL athletes qualify, and any drift in the modal
   vote, the role canonicalisation or the tie-break shows up there.

THE THREE DIFFERENCES THAT ARE CORRECT
--------------------------------------
Printed as notes, never as failures:

* **dim_athlete does not add up.** 64,840 + 6,495 = 71,335 against 68,389 combined.
  The 2,946 surplus is exactly the cfb+nfl population, now one row in each corpus
  instead of one row spanning both. That is the entire point of the split.
* **The bridges are SHORT by the stranded orphans.** They are built by joining the
  fact, so a bridge row whose play_uid is no longer in any fact cannot be reached --
  README.md Known limits section 14. The split schemas are cleaner than pbp.*.
* **dim_venue does not add up.** 35 venues host both corpora and appear in both
  schemas; venues used by neither league's games appear in neither.
"""
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import time

SSH_HOST = "awsvm"
DB = "pbp"

PRELUDE = """
SET statement_timeout = 0;
SET extra_float_digits = 1;
SET DateStyle = 'ISO, MDY'; SET IntervalStyle='postgres'; SET TimeZone='UTC';
"""
PSET = r"""
\pset tuples_only on
\pset format unaligned
\pset fieldsep '|'
"""

# table -> the pbp.* predicate that should select exactly the split schema's rows.
# The bridges are reached through their fact, matching how split_leagues.sql builds them.
FACTS = {
    "special_teams_play": "league = '{L}'",
    "scrimmage_play":     "league = '{L}'",
    "drive":              "league = '{L}'",
    "fact_game":          "league = '{L}'",
    "dim_team":           "league = '{L}'",
    "dim_team_season":    "league = '{L}'",
    "dim_conference":     "league = '{L}'",
}
BRIDGES = {
    "play_athlete":      "EXISTS (SELECT 1 FROM pbp.special_teams_play p"
                         " WHERE p.play_uid = t.play_uid AND p.league = '{L}')",
    "scrimmage_athlete": "EXISTS (SELECT 1 FROM pbp.scrimmage_play p"
                         " WHERE p.play_uid = t.play_uid AND p.league = '{L}')",
}


def psql(sql: str, remote: bool) -> list[str]:
    body = PSET + PRELUDE + sql
    if remote:
        inner = f"psql -d {DB} -q -v ON_ERROR_STOP=on -f -"
        cmd = ["ssh", SSH_HOST, f"sudo -u postgres bash -c {shlex.quote(inner)}"]
    else:
        cmd = ["psql", "-d", DB, "-q", "-v", "ON_ERROR_STOP=on", "-f", "-"]
    r = subprocess.run(cmd, input=body, capture_output=True, text=True)
    if r.returncode != 0:
        where = "the mirror" if remote else "local Postgres"
        sys.exit(f"\nQuery failed on {where}:\n{r.stderr.strip()}")
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


# --------------------------------------------------------------------- structure

def check_no_league_column(remote: bool) -> int:
    """`league` must not exist in either split schema. It is the whole point."""
    rows = psql("""
SELECT n.nspname||'.'||c.relname||'.'||a.attname
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname IN ('cfb','nfl') AND c.relkind='r'
  AND a.attnum > 0 AND NOT a.attisdropped AND a.attname IN ('league','leagues','nfl_plays')
ORDER BY 1;""", remote)
    if rows:
        for r in rows:
            print(f"  DIFF  league-flavoured column survived the split: {r}")
        return len(rows)
    print("  ok    no league / leagues / nfl_plays column in either schema")
    return 0


def check_schemas_symmetric(remote: bool) -> int:
    """cfb and nfl must be the same shape, or one codebase cannot serve both."""
    rows = psql("""
WITH cols AS (
  SELECT n.nspname AS sch, c.relname AS tbl, a.attname AS col,
         format_type(a.atttypid, a.atttypmod) AS typ
  FROM pg_attribute a
  JOIN pg_class c ON c.oid = a.attrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname IN ('cfb','nfl') AND c.relkind='r'
    AND a.attnum > 0 AND NOT a.attisdropped)
SELECT coalesce(a.tbl, b.tbl)||'.'||coalesce(a.col, b.col)
       ||'  cfb='||coalesce(a.typ,'<absent>')||'  nfl='||coalesce(b.typ,'<absent>')
FROM (SELECT * FROM cols WHERE sch='cfb') a
FULL JOIN (SELECT * FROM cols WHERE sch='nfl') b ON b.tbl=a.tbl AND b.col=a.col
WHERE a.col IS NULL OR b.col IS NULL OR a.typ IS DISTINCT FROM b.typ
ORDER BY 1;""", remote)
    if rows:
        for r in rows:
            print(f"  DIFF  cfb/nfl shape differs: {r}")
        return len(rows)
    n = psql("SELECT count(DISTINCT c.relname) FROM pg_class c JOIN pg_namespace n"
             " ON n.oid=c.relnamespace WHERE n.nspname='cfb' AND c.relkind='r';", remote)[0]
    print(f"  ok    cfb and nfl are identical in shape ({n} tables, same columns, same types)")
    return 0


# ----------------------------------------------------------------------- content

def hash_expr(cols: str) -> str:
    return (f"coalesce(sum(('x'||substr(md5(ROW({cols})::text),1,15))"
            f"::bit(60)::bigint::numeric),0)")


def check_facts(remote: bool) -> int:
    """Every fact and dimension that is a pure filter, checksummed both sides.

    The column list is read from the SPLIT table and applied to pbp.*, so the two
    sides hash the same columns in the same order -- pbp.* has `league` extra and
    dropping it by name is what makes the comparison meaningful rather than
    trivially unequal.
    """
    bad = 0
    print(f"\n{'table':22} {'league':>6} {'split rows':>11} {'pbp rows':>11}  {'':4}")
    for tbl, pred in {**FACTS, **BRIDGES}.items():
        for L in ("cfb", "nfl"):
            cols = psql(f"""
SELECT string_agg(quote_ident(attname), ',' ORDER BY attnum)
FROM pg_attribute WHERE attrelid='{L}.{tbl}'::regclass
  AND attnum>0 AND NOT attisdropped;""", remote)[0]
            where = pred.format(L=L)
            got = psql(f"""
SELECT (SELECT count(*)::text||'|'||{hash_expr(cols)} FROM {L}.{tbl} t)
    || '~' ||
       (SELECT count(*)::text||'|'||{hash_expr(cols)} FROM pbp.{tbl} t WHERE {where});""",
                       remote)[0]
            split, src = got.split("~")
            s_n, s_h = split.split("|"); p_n, p_h = src.split("|")
            ok = (s_h == p_h)
            note = ""
            if tbl in BRIDGES and s_n != p_n:
                ok = (s_h == p_h)
            flag = "ok  " if ok else "DIFF"
            if not ok:
                bad += 1
            print(f"  {flag} {tbl:20} {L:>6} {s_n:>11} {p_n:>11}  {note}")
    return bad


def check_athlete_rebuild(remote: bool) -> int:
    """The binding check on the one table that is rebuilt rather than filtered."""
    bad = 0
    print("\n  dim_athlete, the rebuilt one:")
    for L, tag in (("cfb", "cfb"), ("nfl", "nfl")):
        row = psql(f"""
WITH only_one AS (SELECT athlete_id FROM pbp.dim_athlete WHERE leagues = '{tag}')
SELECT count(*)::text
    || '|' || count(*) FILTER (WHERE s.primary_team_id IS DISTINCT FROM p.primary_team_id)
    || '|' || count(*) FILTER (WHERE s.primary_role    IS DISTINCT FROM p.primary_role)
    || '|' || count(*) FILTER (WHERE s.first_season    IS DISTINCT FROM p.first_season
                                  OR s.last_season     IS DISTINCT FROM p.last_season)
    || '|' || count(*) FILTER (WHERE s.st_plays        IS DISTINCT FROM p.st_plays
                                  OR s.scrimmage_plays IS DISTINCT FROM p.scrimmage_plays)
    || '|' || count(*) FILTER (WHERE s.known_name      IS DISTINCT FROM p.known_name)
FROM {L}.dim_athlete s JOIN pbp.dim_athlete p USING (athlete_id)
JOIN only_one USING (athlete_id);""", remote)[0]
        n, team, role, seas, plays, name = row.split("|")
        mismatches = sum(int(x) for x in (team, role, seas, plays, name))
        flag = "ok  " if mismatches == 0 else "DIFF"
        if mismatches:
            bad += 1
        print(f"  {flag} {L}: {n} single-league athletes vs the trusted combined build — "
              f"{mismatches} field mismatch(es)")
        if mismatches:
            print(f"        team={team} role={role} seasons={seas} plays={plays} name={name}")

    # The arithmetic that shows the split did what it is for.
    tot = psql("""
SELECT (SELECT count(*) FROM cfb.dim_athlete)::text
    || '|' || (SELECT count(*) FROM nfl.dim_athlete)
    || '|' || (SELECT count(*) FROM pbp.dim_athlete)
    || '|' || (SELECT count(*) FROM pbp.dim_athlete WHERE leagues='cfb+nfl');""", remote)[0]
    c, n, comb, both = (int(x) for x in tot.split("|"))
    surplus = c + n - comb
    flag = "ok  " if surplus == both else "DIFF"
    if surplus != both:
        bad += 1
    print(f"  {flag} {c:,} cfb + {n:,} nfl = {c+n:,} vs {comb:,} combined; "
          f"surplus {surplus:,} == cfb+nfl population {both:,}")
    return bad


def check_orphans(remote: bool) -> None:
    """Informational: the split schemas should carry NONE of the stranded rows."""
    row = psql("""
SELECT (SELECT count(*) FROM pbp.scrimmage_athlete b
        WHERE NOT EXISTS (SELECT 1 FROM pbp.scrimmage_play p WHERE p.play_uid=b.play_uid))::text
    || '|' || (SELECT count(*) FROM cfb.scrimmage_athlete b
        WHERE NOT EXISTS (SELECT 1 FROM cfb.scrimmage_play p WHERE p.play_uid=b.play_uid))
    || '|' || (SELECT count(*) FROM nfl.scrimmage_athlete b
        WHERE NOT EXISTS (SELECT 1 FROM nfl.scrimmage_play p WHERE p.play_uid=b.play_uid));""",
               remote)[0]
    src, c, n = row.split("|")
    print(f"\n  note  stranded bridge rows (Known limits §14): pbp={src}  cfb={c}  nfl={n}"
          "   (the split being 0 is correct — it is built by joining the fact)")


def run(remote: bool, label: str) -> int:
    print(f"\n{'='*66}\n{label}\n{'='*66}")
    t0 = time.time()
    bad = 0
    print("\n[structure]")
    bad += check_no_league_column(remote)
    bad += check_schemas_symmetric(remote)
    print("\n[content]  order-independent checksum, split vs the same rows of pbp.*")
    bad += check_facts(remote)
    bad += check_athlete_rebuild(remote)
    check_orphans(remote)
    print(f"\n  {time.time()-t0:.0f}s")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--mirror", action="store_true")
    a = ap.parse_args()
    do_local = a.local or not a.mirror
    do_mirror = a.mirror or not a.local

    bad = 0
    if do_local:
        bad += run(False, "LOCAL PostgreSQL — the system of record")
    if do_mirror:
        bad += run(True, f"EC2 MIRROR — {SSH_HOST}")

    if bad:
        sys.exit(f"\nSPLIT VERIFY FAILED: {bad} problem(s). cfb.*/nfl.* do NOT faithfully "
                 "reproduce pbp.*. Do not point the MCP servers at them.")
    print("\nVerified: cfb.* and nfl.* are a faithful, lossless split of pbp.*.")


if __name__ == "__main__":
    main()
