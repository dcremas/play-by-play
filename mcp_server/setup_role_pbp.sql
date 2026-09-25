-- The read-only role the pbp MCP server connects as. Idempotent -- safe to re-run.
--
--     psql -h 127.0.0.1 -p 15432 -U dustincremascoli -d pbp -f setup_role_pbp.sql
--
-- THIS GRANTS AN EXISTING ROLE, IT DOES NOT CREATE A NEW ONE.
-- `mcp_ro` already exists on this server and already reads the weatherdata and
-- apple_weatherkit databases. One role granted per database is the convention
-- here, and it is deliberate: it means a single credential, and it means a future
-- cross-warehouse join (plays against weather -- see README.md "Not built") needs
-- no new identity. The cost is that rotating the password affects both servers'
-- .env files at once.
--
-- The role is created if it is somehow absent, so this file also works on a fresh
-- machine.

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcp_ro') THEN
        -- NOINHERIT: the role does not pick up privileges from any group it is
        -- later added to by accident. Every privilege it has is granted below,
        -- explicitly, and is visible in this file.
        CREATE ROLE mcp_ro LOGIN NOINHERIT
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
        RAISE NOTICE 'created role mcp_ro -- it has NO PASSWORD yet, see step 3c';
    ELSE
        RAISE NOTICE 'role mcp_ro already exists; granting pbp access to it';
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
ALTER ROLE mcp_ro IN DATABASE pbp SET default_transaction_read_only = on;
ALTER ROLE mcp_ro IN DATABASE pbp SET statement_timeout = '60s';
ALTER ROLE mcp_ro IN DATABASE pbp SET idle_in_transaction_session_timeout = '60s';
-- So an unqualified table name in run_sql resolves the way guard.DEFAULT_SCHEMA
-- assumes it does.
ALTER ROLE mcp_ro IN DATABASE pbp SET search_path = pbp;

-- ---------------------------------------------------------------- grants
GRANT CONNECT ON DATABASE pbp TO mcp_ro;
GRANT USAGE   ON SCHEMA pbp   TO mcp_ro;

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
TO mcp_ro;

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

\echo '=== mcp_ro privileges on pbp'
SELECT table_name, string_agg(privilege_type, ',' ORDER BY privilege_type) AS privs
FROM information_schema.table_privileges
WHERE grantee = 'mcp_ro' AND table_schema = 'pbp'
GROUP BY table_name
ORDER BY table_name;

\echo '=== role attributes (all should be false except rolcanlogin)'
SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin
FROM pg_roles WHERE rolname = 'mcp_ro';
