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
        -- CONNECTION LIMIT 12, and both the old value and the obvious fix were
        -- wrong. db.py's pool is max_size=4 and there are now THREE legitimate
        -- clients -- pbp-mcp@cfb, pbp-mcp@nfl and a developer's tunnel -- so 12
        -- at saturation against the previous limit of 10 would have surfaced as an
        -- intermittent "too many connections for role" under load.
        --
        -- But this box runs max_connections=40, not the 100 a bigger server would,
        -- and ~18 are already held by the other services. Raising this to 20 would
        -- let pbp alone claim half the instance and leave 2 spare -- trading an
        -- occasional error here for a hard outage everywhere. 12 covers the three
        -- clients exactly and leaves the remaining headroom to everything else,
        -- which is the actual point: a leak here must not take the other databases
        -- on this box down with it.
        CREATE ROLE pbp_ro LOGIN NOINHERIT CONNECTION LIMIT 12
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
        RAISE NOTICE 'created role pbp_ro -- it has NO PASSWORD yet, see README section 3';
    ELSE
        RAISE NOTICE 'role pbp_ro already exists; re-applying settings and grants';
        -- Re-asserted rather than assumed: this file is the record of what the
        -- role may do, so a hand-edit made in psql is undone by the next run.
        ALTER ROLE pbp_ro LOGIN NOINHERIT CONNECTION LIMIT 12
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
-- NO search_path DEFAULT IS SET HERE ANY MORE, and that is deliberate. One role serves two
-- corpora, so a role-level default would silently be wrong for one of them. db.py sets
-- `-c search_path=<schema>` per connection from MCP_DB_SCHEMA instead, which is also what
-- makes the setting impossible to disagree with the guard's allow-list.
ALTER ROLE pbp_ro IN DATABASE pbp RESET search_path;

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
--
-- GRANTED ON THE TWO PER-LEAGUE SCHEMAS, NOT ON pbp.
--
-- `pbp` is the system of record -- eleven normalised tables the loaders build. The MCP
-- servers read the derived per-league serving layer instead (sql/split_leagues.sql), so
-- pbp_ro has no reason to see pbp.* at all and its access there is revoked below. The
-- practical effect: a question can be answered about college football or about the NFL,
-- and there is no table reachable from either server that contains both.
--
-- ONE ROLE FOR BOTH SCHEMAS, deliberately. Two roles would mean two credentials, two
-- systemd EnvironmentFiles and two rotations, and would buy isolation between two servers
-- that are equally trusted, run as the same service user and read the same public corpus.
-- The separation that matters -- this warehouse from the weather warehouse -- is already
-- made by pbp_ro vs mcp_ro and enforced in pg_hba.conf. What keeps a college server out of
-- the NFL tables is `search_path` pinned to one schema plus guard.ALLOWED_TABLES, which is
-- built from that schema alone; a query naming the other corpus is refused by name.
GRANT CONNECT ON DATABASE pbp TO pbp_ro;
GRANT USAGE   ON SCHEMA cfb   TO pbp_ro;
GRANT USAGE   ON SCHEMA nfl   TO pbp_ro;

-- Named table by table rather than with ALL TABLES, in both schemas. Explicit is the
-- point: a new table appearing in either schema is NOT readable until someone adds it here
-- and to guard.READABLE, which is how a table that was not meant to be exposed stays
-- unexposed. selftest.py compares the two lists and fails if they drift.
DO $$
DECLARE s text; t text;
BEGIN
    FOREACH s IN ARRAY ARRAY['cfb','nfl'] LOOP
        FOREACH t IN ARRAY ARRAY[
            'play_wide','scrimmage_wide','season_status',
            'special_teams_play','scrimmage_play','drive',
            'play_athlete','scrimmage_athlete',
            'dim_athlete','dim_team','dim_team_season','dim_conference','dim_venue',
            'fact_game'] LOOP
            EXECUTE format('GRANT SELECT ON %I.%I TO pbp_ro', s, t);
        END LOOP;
    END LOOP;
END $$;

-- sql/split_leagues.sql does DROP SCHEMA ... CASCADE and rebuilds, and PRIVILEGES GO WITH A
-- DROPPED TABLE. scripts/sync_ec2.py therefore runs this file after it, every time. If
-- someone reorders those steps the next sync silently revokes both servers' access to
-- everything, and selftest.py's "guard allow-list matches the grants" check is what catches
-- it.

-- The system of record is not for the MCP. Revoked rather than merely unused, so the
-- servers cannot reach a `league` column even by accident.
REVOKE ALL ON ALL TABLES IN SCHEMA pbp FROM pbp_ro;
REVOKE ALL ON SCHEMA pbp                FROM pbp_ro;

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
\echo '=== pbp_ro privileges (expect SELECT on 14 tables in EACH of cfb and nfl, and none in pbp)'
SELECT table_schema, count(*) AS tables_readable
FROM information_schema.table_privileges
WHERE grantee = 'pbp_ro' AND privilege_type = 'SELECT'
GROUP BY table_schema ORDER BY table_schema;

\echo '=== mcp_ro privileges on pbp (expect ZERO rows)'
SELECT table_name, privilege_type
FROM information_schema.table_privileges
WHERE grantee = 'mcp_ro' AND table_schema = 'pbp';

\echo '=== role attributes (expect f for all but rolcanlogin; connlimit 12)'
SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls,
       rolinherit, rolcanlogin, rolconnlimit
FROM pg_roles WHERE rolname = 'pbp_ro';

\echo '=== session defaults'
SELECT s.setconfig
FROM pg_db_role_setting s
JOIN pg_roles r ON r.oid = s.setrole
JOIN pg_database d ON d.oid = s.setdatabase
WHERE r.rolname = 'pbp_ro' AND d.datname = 'pbp';
