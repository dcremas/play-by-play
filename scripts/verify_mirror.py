"""Prove the EC2 mirror matches local Postgres -- in structure AND in content.

    python scripts/verify_mirror.py                  # everything (~2 min)
    python scripts/verify_mirror.py --structure      # catalogs only, seconds
    python scripts/verify_mirror.py --content        # checksums only
    python scripts/verify_mirror.py --table drive    # repeatable, content phase

WHAT THIS ADDS OVER sync_ec2.py's reconcile()
---------------------------------------------
reconcile() counts rows. That catches a shortfall -- a missing extract, a season
predicate that matched nothing -- and it runs on every sync, which is where a
cheap check belongs. It cannot catch a row that is PRESENT ON BOTH SIDES AND
DIFFERENT, and it says nothing at all about the schema.

This reads both catalogs and compares every column, constraint and index, then
checksums every row of every table. Two identical counts with different contents
fail here and pass there. Run it after a schema change, after a full resync, or
any time the mirror's answers stop agreeing with local.

SCOPE: THE SYSTEM OF RECORD ONLY
--------------------------------
This compares `pbp.*` -- the eleven tables the loaders build and README.md's data
dictionary describes. It deliberately does NOT compare `cfb.*` and `nfl.*`.

Those are a DERIVED serving layer, rebuilt on each host by sql/split_leagues.sql
rather than copied, so they legitimately differ in ways that mean nothing: the
schemas are owned by `dustincremascoli` here and by `postgres` on the box, which
alone produced 196 spurious GRANT differences and zero real ones. What must be
true of them is not "identical to the other host" but "a faithful projection of
the pbp schema on THIS host", and that is a different question, asked by
scripts/verify_split.py -- which runs against both hosts and checksums every fact
against the same rows of pbp.*.

So: verify_mirror proves the two copies of the source agree; verify_split proves
each host's serving layer agrees with its own source. Together they cover the
chain. Neither subsumes the other.

THE THREE DIVERGENCES THAT ARE CORRECT, AND WHY THEY ARE NOT HIDDEN
-------------------------------------------------------------------
A verifier that fails on expected differences gets ignored within two runs, so
each one is named, allow-listed, and PRINTED as a note rather than silently
dropped. If one of them stops appearing, that is itself worth knowing.

1. `loaded_at` is `DEFAULT now()` on both fact tables -- it records when a row was
   inserted into THIS database, so it is different by construction and excluded
   from the content hash. Every other column is compared.
2. The mirror carries the serving layer (`play_wide`, `scrimmage_wide`,
   `season_status`, their indexes, and the `mcp_ro` grants). sql/split_leagues.sql
   builds it there for the MCP server and its own header calls it derived, not
   part of the system of record. Local has never built it.
3. The bridges are compared THROUGH THEIR FACT, never raw. See the long note on
   RECONCILE_SQL in sync_ec2.py: load_league.sql strands bridge rows whose
   play_uid changed, so local accumulates orphans a freshly loaded mirror does
   not have. Raw counts would fail every run over a difference that means the
   mirror is cleaner. Orphans are reported separately at the end.

TWO TRAPS THAT WILL MAKE THIS LIE IF YOU CHANGE IT
--------------------------------------------------
* **Read the catalogs as a superuser on both sides.** `information_schema` shows
  only objects the current role holds privileges on, so probing the mirror as
  `mcp_ro` would silently omit whatever it cannot see and report a match. That is
  why the remote half runs `sudo -u postgres` and not through the tunnel.
* **PostgreSQL 18 catalogs NOT NULL in `pg_constraint`; 16 does not.** Local is
  18.6 and the box is 16.15, so a naive constraint diff reports ~33 phantom
  extra constraints. They are dropped here, and `attnotnull` -- which both
  versions do keep in pg_attribute -- is compared per column instead, which is
  the real check.

Text rendering is the other version hazard: `extra_float_digits`, `DateStyle`,
`IntervalStyle` and `TimeZone` are pinned identically on both servers before any
hashing, so a GUC set differently on one box cannot masquerade as a data
difference.
"""
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import time

SSH_HOST = "awsvm"        # see ~/.ssh/config
REMOTE_DB = "pbp"
LOCAL_DB = "pbp"

# The combined serving layer sql/wide_tables.sql used to build inside `pbp`. It was
# dropped on 2026-09-25 when sql/split_leagues.sql replaced it with per-league
# schemas, so these names should now appear on NEITHER host. Kept listed so a
# mirror that has not yet been resynced still verifies rather than reporting three
# spurious tables.
MIRROR_ONLY_TABLES = ("play_wide", "scrimmage_wide", "season_status")
# The MCP role exists only on the box. Table owner is the same name on both.
# `pbp_ro` replaced the shared `mcp_ro` on 2026-09-25 -- see
# mcp_server/setup_role_pbp.sql. Both names stay listed: mcp_ro so a mirror that
# has not yet had the new role file applied still verifies, and so that the
# grants left on an older snapshot do not read as drift.
MIRROR_ONLY_GRANTEES = ("pbp_ro", "mcp_ro", "postgres")
# Extensions that legitimately differ. amcheck is a local maintenance tool.
EXPECTED_EXTENSION_DIFF = ("amcheck",)

# Insert-time audit columns: `DEFAULT now()`, different by construction.
IGNORED_COLUMNS = ("loaded_at",)

# table -> predicate restricting the rows compared. The bridges are joined to
# their fact so stranded local orphans are not counted as a mismatch.
CONTENT_TABLES: dict[str, str] = {
    "dim_athlete": "",
    "dim_conference": "",
    "dim_team": "",
    "dim_team_season": "",
    "dim_venue": "",
    "drive": "",
    "fact_game": "",
    "special_teams_play": "",
    "scrimmage_play": "",
    "play_athlete":
        "EXISTS (SELECT 1 FROM pbp.special_teams_play p WHERE p.play_uid = t.play_uid)",
    "scrimmage_athlete":
        "EXISTS (SELECT 1 FROM pbp.scrimmage_play p WHERE p.play_uid = t.play_uid)",
}

# Pinned before every hash so the two servers render values identically.
PRELUDE = """
SET statement_timeout = 0;
SET extra_float_digits = 1;
SET DateStyle = 'ISO, MDY';
SET IntervalStyle = 'postgres';
SET TimeZone = 'UTC';
"""

PSQL_SETTINGS = r"""
\pset tuples_only on
\pset format unaligned
\pset fieldsep '|'
"""


def psql(sql: str, remote: bool) -> list[str]:
    """Run SQL on one side and return its non-empty output lines.

    Both halves run as a superuser -- locally the table owner, remotely `postgres`
    over ssh. See the note on catalog visibility in the module docstring.
    """
    body = PSQL_SETTINGS + PRELUDE + sql
    if remote:
        inner = f"psql -d {REMOTE_DB} -q -v ON_ERROR_STOP=on -f -"
        cmd = ["ssh", SSH_HOST, f"sudo -u postgres bash -c {shlex.quote(inner)}"]
    else:
        cmd = ["psql", "-d", LOCAL_DB, "-q", "-v", "ON_ERROR_STOP=on", "-f", "-"]
    result = subprocess.run(cmd, input=body, capture_output=True, text=True)
    if result.returncode != 0:
        where = "the mirror" if remote else "local Postgres"
        sys.exit(f"\nQuery failed on {where} ({result.returncode}):\n{result.stderr.strip()}")
    return [ln for ln in result.stdout.splitlines() if ln.strip()]


def both(sql: str) -> tuple[list[str], list[str]]:
    return psql(sql, remote=False), psql(sql, remote=True)


# --------------------------------------------------------------- structure phase

STRUCTURE_SQL = r"""
SELECT 'REL|'||n.nspname||'|'||c.relname||'|'||c.relkind::text||'|persist='||c.relpersistence::text
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'pbp'
  AND c.relkind IN ('r','p','v','m','S','f')
ORDER BY 1;

-- attnotnull is carried here rather than read from pg_constraint: PG18 catalogs
-- NOT NULL as a constraint and PG16 does not, but both keep this column.
SELECT 'COL|'||n.nspname||'|'||c.relname||'|'||a.attname||'|'||format_type(a.atttypid, a.atttypmod)
       ||'|notnull='||a.attnotnull
       ||'|default='||coalesce(pg_get_expr(d.adbin, d.adrelid), '-')
       ||'|ident='||coalesce(nullif(a.attidentity::text, ''), '-')
       ||'|gen='||coalesce(nullif(a.attgenerated::text, ''), '-')
       ||'|collate='||coalesce((SELECT co.collname FROM pg_collation co WHERE co.oid = a.attcollation), '-')
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE n.nspname = 'pbp'
  AND c.relkind IN ('r','p','v','m','f') AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY 1;

SELECT 'CON|'||n.nspname||'|'||rel.relname||'|'||con.conname||'|'||pg_get_constraintdef(con.oid)
FROM pg_constraint con
JOIN pg_class rel ON rel.oid = con.conrelid
JOIN pg_namespace n ON n.oid = rel.relnamespace
WHERE n.nspname = 'pbp'
  AND pg_get_constraintdef(con.oid) NOT LIKE 'NOT NULL %'
ORDER BY 1;

SELECT 'IDX|'||schemaname||'|'||tablename||'|'||indexname||'|'||indexdef
FROM pg_indexes
WHERE schemaname = 'pbp'
ORDER BY 1;

SELECT 'VIEWDEF|'||n.nspname||'|'||c.relname||'|'||md5(pg_get_viewdef(c.oid, true))
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'pbp'
  AND c.relkind IN ('v','m')
ORDER BY 1;

SELECT 'FUNC|'||n.nspname||'|'||p.proname||'|'||pg_get_function_identity_arguments(p.oid)
       ||'|'||p.prokind::text||'|'||md5(coalesce(p.prosrc, ''))
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'pbp'
ORDER BY 1;

SELECT 'TRG|'||n.nspname||'|'||c.relname||'|'||t.tgname||'|'||pg_get_triggerdef(t.oid)
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE NOT t.tgisinternal AND n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
ORDER BY 1;

SELECT 'TYPE|'||n.nspname||'|'||t.typname||'|'||t.typtype::text||'|'||coalesce(
    (SELECT string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder) FROM pg_enum e WHERE e.enumtypid = t.oid), '-')
FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE n.nspname = 'pbp'
  AND t.typtype IN ('e','c','d')
  AND NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.oid = t.typrelid AND c.relkind <> 'c')
ORDER BY 1;

SELECT 'SEQ|'||schemaname||'|'||sequencename||'|'||coalesce(data_type::text, '-')
       ||'|inc='||coalesce(increment_by::text, '-')||'|cycle='||cycle::text
FROM pg_sequences WHERE schemaname = 'pbp' ORDER BY 1;

SELECT 'GRANT|'||table_schema||'|'||table_name||'|'||grantee||'|'||privilege_type
FROM information_schema.role_table_grants
WHERE table_schema = 'pbp'
ORDER BY 1;

SELECT 'EXT|'||extname FROM pg_extension ORDER BY 1;

SELECT 'SCHEMA|'||nspname FROM pg_namespace
WHERE nspname IN ('pbp','cfb','nfl')
ORDER BY 1;
"""


def is_expected(line: str, mirror_side: bool) -> bool:
    """True if this catalog row is one of the three documented divergences."""
    parts = line.split("|")
    kind = parts[0]
    if kind == "EXT":
        return parts[1] in EXPECTED_EXTENSION_DIFF and not mirror_side
    if not mirror_side:
        return False                     # everything else must exist on both sides
    if kind == "GRANT" and len(parts) > 3 and parts[3] in MIRROR_ONLY_GRANTEES:
        return True
    # REL/COL/CON/IDX/VIEWDEF all carry the table name in field 2.
    return len(parts) > 2 and parts[2] in MIRROR_ONLY_TABLES


def check_structure() -> int:
    print("[structure]  catalogs, as superuser on both sides")
    t0 = time.time()
    local, remote = both(STRUCTURE_SQL)
    lset, rset = set(local), set(remote)

    only_local = sorted(lset - rset)
    only_mirror = sorted(rset - lset)

    expected_l = [ln for ln in only_local if is_expected(ln, mirror_side=False)]
    expected_r = [ln for ln in only_mirror if is_expected(ln, mirror_side=True)]
    bad_l = [ln for ln in only_local if ln not in expected_l]
    bad_r = [ln for ln in only_mirror if ln not in expected_r]

    for label, rows in (("mirror-only, by design", expected_r),
                        ("local-only, by design", expected_l)):
        if rows:
            by_kind: dict[str, int] = {}
            for ln in rows:
                by_kind[ln.split("|")[0]] = by_kind.get(ln.split("|")[0], 0) + 1
            summary = ", ".join(f"{n} {k}" for k, n in sorted(by_kind.items()))
            print(f"  note  {label}: {summary}")

    print(f"  compared {len(lset)} local / {len(rset)} mirror catalog rows "
          f"in {time.time() - t0:.0f}s")
    for ln in bad_l:
        print(f"  DIFF  local only : {ln}")
    for ln in bad_r:
        print(f"  DIFF  mirror only: {ln}")
    if not bad_l and not bad_r:
        print("  ok    structure is identical")
    return len(bad_l) + len(bad_r)


# ----------------------------------------------------------------- content phase

# An order-independent checksum: sum, as numeric, of the top 60 bits of each row's
# md5. Order-independent so neither side needs a sort of 4.4M rows, and summing in
# numeric cannot overflow. It is a checksum, not a proof -- it will catch any real
# divergence, but it is not hardened against a deliberately constructed collision.
HASH_FN = r"""
CREATE OR REPLACE FUNCTION pg_temp.hash_table(tbl regclass, ignore text[], pred text)
RETURNS text LANGUAGE plpgsql AS $fn$
DECLARE cols text; n bigint; h numeric;
BEGIN
  SELECT string_agg(quote_ident(attname), ',' ORDER BY attnum) INTO cols
  FROM pg_attribute
  WHERE attrelid = tbl AND attnum > 0 AND NOT attisdropped AND NOT (attname = ANY(ignore));
  EXECUTE format(
    'SELECT count(*), coalesce(sum((''x''||substr(md5(ROW(%s)::text), 1, 15))::bit(60)::bigint::numeric), 0)'
    ' FROM %s t WHERE %s', cols, tbl::text, coalesce(nullif(pred, ''), 'true'))
  INTO n, h;
  RETURN n || '|' || h;
END $fn$;
"""

# Run when a whole-table hash disagrees: hashes each column on its own, so the
# output names the columns that actually differ instead of just the table.
COLUMN_HASH_FN = r"""
CREATE OR REPLACE FUNCTION pg_temp.hash_columns(tbl regclass, ignore text[], pred text)
RETURNS SETOF text LANGUAGE plpgsql AS $fn$
DECLARE c record; h numeric; nulls bigint;
BEGIN
  FOR c IN SELECT attname FROM pg_attribute
           WHERE attrelid = tbl AND attnum > 0 AND NOT attisdropped
             AND NOT (attname = ANY(ignore)) ORDER BY attnum
  LOOP
    EXECUTE format(
      'SELECT coalesce(sum((''x''||substr(md5(coalesce(%I::text, ''<NULL>'')), 1, 15))::bit(60)::bigint::numeric), 0),'
      ' count(*) FILTER (WHERE %I IS NULL) FROM %s t WHERE %s',
      c.attname, c.attname, tbl::text, coalesce(nullif(pred, ''), 'true'))
    INTO h, nulls;
    RETURN NEXT c.attname || '|' || h || '|nulls=' || nulls;
  END LOOP;
END $fn$;
"""


def sql_array(items) -> str:
    return "ARRAY[" + ",".join(f"'{i}'" for i in items) + "]::text[]"


def hash_sql(tables: dict[str, str]) -> str:
    calls = "\n".join(
        f"SELECT '{name}|' || pg_temp.hash_table('pbp.{name}'::regclass, "
        f"{sql_array(IGNORED_COLUMNS)}, {quote(pred)});"
        for name, pred in tables.items()
    )
    return HASH_FN + calls


def quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def explain_table(name: str, pred: str) -> None:
    """Name the columns behind a whole-table mismatch."""
    sql = COLUMN_HASH_FN + (
        f"SELECT pg_temp.hash_columns('pbp.{name}'::regclass, "
        f"{sql_array(IGNORED_COLUMNS)}, {quote(pred)});"
    )
    local, remote = both(sql)
    lmap = {ln.split("|")[0]: ln for ln in local}
    rmap = {ln.split("|")[0]: ln for ln in remote}
    for col in sorted(set(lmap) | set(rmap)):
        if lmap.get(col) != rmap.get(col):
            print(f"          column {col}")
            print(f"            local : {lmap.get(col, '<absent>')}")
            print(f"            mirror: {rmap.get(col, '<absent>')}")


def check_content(tables: dict[str, str]) -> int:
    print(f"\n[content]  checksumming {len(tables)} table(s); "
          f"{', '.join(IGNORED_COLUMNS)} excluded (DEFAULT now())")
    t0 = time.time()
    sql = hash_sql(tables)
    local, remote = both(sql)
    lmap = {ln.split("|")[0]: ln.split("|", 1)[1] for ln in local}
    rmap = {ln.split("|")[0]: ln.split("|", 1)[1] for ln in remote}

    failed = []
    for name in tables:
        a, b = lmap.get(name), rmap.get(name)
        rows = (a or "?|?").split("|")[0]
        if a == b:
            print(f"  ok    {name:<22} {rows:>9} rows, checksums agree")
        else:
            failed.append(name)
            print(f"  DIFF  {name:<22} local={a}  mirror={b}")
    print(f"  {len(tables) - len(failed)}/{len(tables)} tables match "
          f"in {time.time() - t0:.0f}s")

    for name in failed:
        print(f"\n  which columns differ in {name}:")
        explain_table(name, tables[name])
    return len(failed)


# ------------------------------------------------------------------ orphan note

ORPHAN_SQL = """
SELECT 'scrimmage_athlete|' || count(*) FROM pbp.scrimmage_athlete b
 WHERE NOT EXISTS (SELECT 1 FROM pbp.scrimmage_play p WHERE p.play_uid = b.play_uid);
SELECT 'play_athlete|' || count(*) FROM pbp.play_athlete b
 WHERE NOT EXISTS (SELECT 1 FROM pbp.special_teams_play p WHERE p.play_uid = b.play_uid);
"""


def report_orphans() -> None:
    """Never a failure. The mirror being lower is correct -- see sync_ec2.py."""
    local, remote = both(ORPHAN_SQL)
    lmap = dict(ln.split("|") for ln in local)
    rmap = dict(ln.split("|") for ln in remote)
    shown = False
    for name in sorted(set(lmap) | set(rmap)):
        a, b = int(lmap.get(name, 0)), int(rmap.get(name, 0))
        if a or b:
            if not shown:
                print("\n[stranded bridge rows]  Known limits §14; informational only")
                shown = True
            print(f"  note  {name:<22} local={a:>6}  mirror={b:>6}"
                  "   (the mirror being lower is correct)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--structure", action="store_true", help="catalogs only")
    ap.add_argument("--content", action="store_true", help="checksums only")
    ap.add_argument("--table", action="append", choices=sorted(CONTENT_TABLES),
                    help="repeatable; restrict the content phase")
    args = ap.parse_args()

    do_structure = args.structure or not args.content
    do_content = args.content or not args.structure

    versions = [psql("SELECT version();", remote=r)[0].split(" on ")[0] for r in (False, True)]
    print(f"local  {LOCAL_DB}: {versions[0]}")
    print(f"mirror {SSH_HOST}:{REMOTE_DB}: {versions[1]}\n")

    problems = 0
    if do_structure:
        problems += check_structure()
    if do_content:
        tables = ({t: CONTENT_TABLES[t] for t in args.table} if args.table
                  else CONTENT_TABLES)
        problems += check_content(tables)
        report_orphans()

    if problems:
        sys.exit(f"\nVERIFY FAILED: {problems} unexplained difference(s). The mirror does "
                 "NOT match local Postgres. Do not trust it until this is resolved.")
    print("\nVerified: the mirror matches local Postgres.")


if __name__ == "__main__":
    main()
