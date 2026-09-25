-- Swap a freshly built schema into place. The cutover is one transaction and is instant.
--
--     psql -d pbp -v live=cfb -v stage=cfb_next -f sql/swap_schema.sql
--
-- WHY THIS EXISTS
-- ---------------
-- sql/split_leagues.sql used to open with `DROP SCHEMA cfb CASCADE` and rebuild in place.
-- Measured on the EC2 box, the college rebuild takes 1m39s, and for that whole window
-- pbp-mcp@cfb -- the backend of a public endpoint -- has no schema to read. It is worse
-- than the timing suggests: DROP SCHEMA needs ACCESS EXCLUSIVE, so it first queues behind
-- whatever queries are running, and every new query then queues behind IT. A weekly
-- refresh should not be a maintenance window.
--
-- So the rebuild now happens in a staging schema nothing reads, and this file moves it
-- into place. Three facts make that safe, and all three were checked rather than assumed:
--
--   1. DDL is transactional in PostgreSQL. `BEGIN; ALTER SCHEMA ... RENAME; ROLLBACK`
--      leaves nothing behind, so a failure mid-swap cannot leave a half-named database.
--   2. A rename does not touch table oids, so GRANTs and COMMENTs survive it. The staging
--      schema is granted and commented BEFORE the swap, and arrives fully armed.
--   3. It is instant regardless of size: 0.76 ms to rename the 2.5 GB cfb schema, because
--      only a catalog row changes.
--
-- A query already running against the old schema keeps its tables -- it resolved them to
-- oids before the rename and those oids are still valid, now under a different name. It
-- finishes normally rather than failing.

\set ON_ERROR_STOP on
\set old :live '_old'

\pset tuples_only on
\pset format unaligned

-- The staging schema must exist and the swap must be worth doing. Refusing here rather
-- than renaming something absent is the difference between a clear error and a database
-- that has quietly lost a corpus.
SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :'stage') AS stage_exists \gset
\if :stage_exists
\else
\echo 'FATAL: staging schema ":stage" does not exist -- run sql/split_leagues.sql first'
\quit
\endif

SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :'live') AS live_exists \gset

-- A leftover from an interrupted previous swap. Dropped BEFORE the transaction so the
-- cutover itself has nothing slow in it.
DROP SCHEMA IF EXISTS :old CASCADE;

BEGIN;
\if :live_exists
ALTER SCHEMA :live RENAME TO :old;
\endif
ALTER SCHEMA :stage RENAME TO :live;
COMMIT;

-- AFTER the commit, deliberately. Dropping 2.5 GB takes seconds and needs ACCESS
-- EXCLUSIVE on every table in it; doing that inside the transaction would put exactly the
-- pause back that this file exists to remove. Out here it blocks nothing, because nothing
-- resolves the old name any more -- only queries that were already running hold it, and
-- this waits for them rather than interrupting them.
DROP SCHEMA IF EXISTS :old CASCADE;

\echo ''
SELECT 'swapped: ' || :'stage' || ' -> ' || :'live' || '  ('
    || (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = :'live' AND c.relkind = 'r')::text || ' tables, '
    || (SELECT count(*) FROM information_schema.table_privileges
        WHERE grantee = 'pbp_ro' AND table_schema = :'live')::text || ' granted to pbp_ro)';
