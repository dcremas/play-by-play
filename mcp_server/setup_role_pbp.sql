-- The read-only role the pbp MCP server connects as. Idempotent -- safe to re-run.
--
--     sudo -u postgres psql -d pbp -f setup_role_pbp.sql          # on the box
--     psql -h 127.0.0.1 -p 15432 -U dustincremascoli -d pbp -f setup_role_pbp.sql
--
-- THIS CREATES AND OWNS `pbp_ro`. It used to grant the shared `mcp_ro` instead,
-- and the last section of this file revokes that grant.
--
-- WHY THE SPLIT (changed 2026-09-25)
-- ----------------------------------
-- `mcp_ro` is one role shared by the weather warehouse MCP and, formerly, this
-- one. Two things were wrong with that once this server became a public
-- text-to-SQL backend rather than a laptop tool:
--
--   1. ONE PASSWORD IN TWO SERVICES. Both servers run as separate systemd users
--      specifically so neither can read the other's credential file -- and then
--      both files held the SAME secret, which makes the separation cosmetic.
--      Verified before the change: identical MD5 of the password line in
--      pbp/mcp_server/.env and weather-sql-explorer/mcp_server/.env.
--   2. ROTATION WAS COUPLED. Changing this server's password broke the weather
--      server and the weather FDW's pg_user_mapping. A credential you cannot
--      rotate independently is one you will not rotate.
--
-- The cost is a second identity to manage, and that a future cross-warehouse
-- join (plays against weather -- README.md "Not built") needs a role holding
-- both grants rather than reusing this one. That is the right trade for a
-- credential sitting behind a public endpoint.

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------- the role
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pbp_ro') THEN
        -- NOINHERIT: the role does not pick up privileges from any group it is
        -- later added to by accident. Every privilege it has is granted below,
        -- explicitly, and is visible in this file.
        --
        -- CONNECTION LIMIT 10 is not arbitrary. db.py's pool is max_size=4, and
        -- there are legitimately two clients: the systemd service on the box and
        -- a developer's tunnel from the laptop. 4 + 4 + headroom for selftest.
        -- The point is that a connection leak in this server cannot exhaust
        -- max_connections and take the OTHER databases on this box down with it.
        CREATE ROLE pbp_ro LOGIN NOINHERIT CONNECTION LIMIT 10
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
        RAISE NOTICE 'created role pbp_ro -- it has NO PASSWORD yet, see README section 3';
    ELSE
        RAISE NOTICE 'role pbp_ro already exists; re-applying settings and grants';
        -- Re-asserted rather than assumed: this file is the record of what the
        -- role may do, so a hand-edit made in psql is undone by the next run.
        ALTER ROLE pbp_ro LOGIN NOINHERIT CONNECTION LIMIT 10
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
END
$$;

-- ---------------------------------------------------------------- session defaults
--
-- Set at the ROLE, not asked for by the client, so they hold even if a client
-- forgets. This is the layer that actually enforces read-only: not the SQL guard,
-- not the connection flag, this.
--
-- The 60s statement timeout is twice what the weather warehouse uses. That is a
-- measured difference, not a relaxation: this corpus has a 2.4 GB fact table on a
-- 2-vCPU box, and a legitimate full-window aggregate over 1.96M scrimmage rows
-- can genuinely take longer than 30 seconds. The wide tables exist to keep the
-- common cases far below it.
ALTER ROLE pbp_ro IN DATABASE pbp SET default_transaction_read_only = on;
ALTER ROLE pbp_ro IN DATABASE pbp SET statement_timeout = '60s';
ALTER ROLE pbp_ro IN DATABASE pbp SET idle_in_transaction_session_timeout = '60s';
-- So an unqualified table name in run_sql resolves the way guard.DEFAULT_SCHEMA
-- assumes it does.
ALTER ROLE pbp_ro IN DATABASE pbp SET search_path = pbp;

-- A public endpoint is where a runaway plan actually shows up. Neither of these
-- is a security control -- they bound one honest question's blast radius on a
-- 2-vCPU box with 640 MB of shared_buffers, so one expensive sort cannot evict
-- the cache the other five services on this box are using.
ALTER ROLE pbp_ro IN DATABASE pbp SET work_mem = '32MB';
ALTER ROLE pbp_ro IN DATABASE pbp SET temp_file_limit = '2GB';
-- Parallel workers are shared process-wide and this box has 2 vCPU. Leaving the
-- default lets one aggregate take both and stall the loaders.
ALTER ROLE pbp_ro IN DATABASE pbp SET max_parallel_workers_per_gather = 1;

-- ---------------------------------------------------------------- grants
GRANT CONNECT ON DATABASE pbp TO pbp_ro;
GRANT USAGE   ON SCHEMA pbp   TO pbp_ro;

-- SELECT and nothing else, named table by table rather than with ALL TABLES.
-- Explicit is the point: a new table appearing in this schema is NOT readable
-- until someone adds it here and to guard.ALLOWED_TABLES, which is how a table
-- that was not meant to be exposed stays unexposed.
GRANT SELECT ON
    pbp.play_wide,
    pbp.scrimmage_wide,
    pbp.season_status,
    pbp.special_teams_play,
    pbp.scrimmage_play,
    pbp.drive,
    pbp.play_athlete,
    pbp.scrimmage_athlete,
    pbp.dim_athlete,
    pbp.dim_team,
    pbp.dim_team_season,
    pbp.dim_conference,
    pbp.dim_venue,
    pbp.fact_game
TO pbp_ro;

-- Deliberately NOT granted: any future staging table (pbp.stg_*), and default
-- privileges. A table created by a later load is unreadable until granted, which
-- is the behaviour we want -- an in-flight staging table should never be visible
-- to a question.
--
-- NOTE: sql/wide_tables.sql DROPs and recreates play_wide, scrimmage_wide and
-- season_status on every run, and PRIVILEGES GO WITH A DROPPED TABLE. That file
-- therefore has to re-grant them, or the next rebuild silently revokes the MCP's
-- access to exactly the three tables it depends on most. scripts/sync_ec2.py runs
-- this file after wide_tables.sql for that reason, and selftest.py's
-- "all tables readable" check is what catches it if someone changes that order.

-- ---------------------------------------------------------------- retire mcp_ro here
--
-- The whole point of the split. `mcp_ro` keeps everything it has in weatherdata
-- and apple_weatherkit -- nothing below touches those -- but it stops being able
-- to read this database, so the weather server's credential is no longer a pbp
-- credential.
--
-- Safe because nothing else used it here: this warehouse's only consumer is this
-- MCP server, and it now connects as pbp_ro. If you are reverting, re-granting is
-- the block above with the role name changed; the settings are dropped too.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcp_ro') THEN
        REVOKE ALL ON ALL TABLES IN SCHEMA pbp FROM mcp_ro;
        REVOKE ALL ON SCHEMA pbp                FROM mcp_ro;
        REVOKE ALL ON DATABASE pbp              FROM mcp_ro;
        -- Per-database role settings survive a REVOKE and would silently apply
        -- again if the grant ever came back. Clear them with the grant.
        ALTER ROLE mcp_ro IN DATABASE pbp RESET ALL;
        RAISE NOTICE 'revoked mcp_ro from database pbp (it keeps weatherdata / apple_weatherkit)';
    END IF;
END
$$;

-- ---------------------------------------------------------------- proof
\echo ''
\echo '=== pbp_ro privileges on pbp (expect SELECT on 14 tables, nothing else)'
SELECT table_name, string_agg(privilege_type, ',' ORDER BY privilege_type) AS privs
FROM information_schema.table_privileges
WHERE grantee = 'pbp_ro' AND table_schema = 'pbp'
GROUP BY table_name
ORDER BY table_name;

\echo '=== mcp_ro privileges on pbp (expect ZERO rows)'
SELECT table_name, privilege_type
FROM information_schema.table_privileges
WHERE grantee = 'mcp_ro' AND table_schema = 'pbp';

\echo '=== role attributes (expect f for all but rolcanlogin; connlimit 10)'
SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls,
       rolinherit, rolcanlogin, rolconnlimit
FROM pg_roles WHERE rolname = 'pbp_ro';

\echo '=== session defaults'
SELECT s.setconfig
FROM pg_db_role_setting s
JOIN pg_roles r ON r.oid = s.setrole
JOIN pg_database d ON d.oid = s.setdatabase
WHERE r.rolname = 'pbp_ro' AND d.datname = 'pbp';
