-- Make the warehouse two-league. Run once; idempotent.
--
--     psql -d pbp -f sql/migrate_league.sql
--
-- WHAT IS KEYED BY LEAGUE AND WHAT IS NOT. This is the whole design, and it was measured
-- rather than assumed (README "The NFL"):
--
--   dim_venue    NOT keyed by league. ESPN venue ids are ONE id space across both feeds --
--                27 of 31 sampled NFL venues were already in this table under the same
--                name, because a bowl game at AT&T Stadium is that building. Splitting it
--                would duplicate 27 stadiums and break the venue grain that dim_venue.indoor
--                depends on.
--
--   dim_athlete  NOT keyed by league, for the stronger version of the same reason: 720 of
--                720 athlete ids sampled from NFL participants that also appear in this
--                table returned the IDENTICAL name from the NFL endpoint. Patrick Mahomes
--                is 3139477 in both feeds and is one man. Keying by league would give him
--                two rows and make "his college and pro careers" an unanswerable question.
--                A `leagues` column records which corpora he appears in.
--
--   dim_team,    KEYED BY LEAGUE, because these ids genuinely collide and mean different
--   dim_team_    things. NFL team ids run 1-34; team 2 is Auburn here and the Falcons are
--   season,      team 1. Conference 8 is the SEC here and the AFC there. Without the
--   dim_conf     compound key a join silently mixes them.
--
--   the facts    Carry a `league` column. play_uid is globally unique without it -- 0
--                collisions between 3,297 NFL game ids and 10,470 college ones -- but the
--                column makes that a testable property instead of a lucky one, and it lets
--                the explorer treat league as an ordinary filter.

\set ON_ERROR_STOP on
BEGIN;

-- ---------------------------------------------------------------- facts
ALTER TABLE pbp.special_teams_play ADD COLUMN IF NOT EXISTS league text NOT NULL DEFAULT 'cfb';
ALTER TABLE pbp.scrimmage_play     ADD COLUMN IF NOT EXISTS league text NOT NULL DEFAULT 'cfb';
ALTER TABLE pbp.drive              ADD COLUMN IF NOT EXISTS league text NOT NULL DEFAULT 'cfb';
ALTER TABLE pbp.fact_game          ADD COLUMN IF NOT EXISTS league text NOT NULL DEFAULT 'cfb';

-- The NFL gamebook names the long snapper on every kick and the holder on every place
-- kick. The college feed names neither, so these are NEW columns rather than ported ones
-- and stay NULL for all 316,397 college rows by construction.
ALTER TABLE pbp.special_teams_play ADD COLUMN IF NOT EXISTS snapper_name text;
ALTER TABLE pbp.special_teams_play ADD COLUMN IF NOT EXISTS holder_name  text;

CREATE INDEX IF NOT EXISTS special_teams_play_league_idx ON pbp.special_teams_play (league, season, play_kind);
CREATE INDEX IF NOT EXISTS scrimmage_play_league_idx     ON pbp.scrimmage_play (league, season, play_kind);
CREATE INDEX IF NOT EXISTS drive_league_idx              ON pbp.drive (league, season);
CREATE INDEX IF NOT EXISTS fact_game_league_idx          ON pbp.fact_game (league, season, week);

-- ---------------------------------------------------------------- team dimensions
ALTER TABLE pbp.dim_team        ADD COLUMN IF NOT EXISTS league text NOT NULL DEFAULT 'cfb';
ALTER TABLE pbp.dim_team_season ADD COLUMN IF NOT EXISTS league text NOT NULL DEFAULT 'cfb';
ALTER TABLE pbp.dim_conference  ADD COLUMN IF NOT EXISTS league text NOT NULL DEFAULT 'cfb';

-- `division` means FBS | FCS and is meaningless for the NFL. Overloading it with 'AFC East'
-- would make one column mean two incompatible things across leagues, so the NFL's eight
-- divisions get their own column and `division` stays NULL for every NFL row.
ALTER TABLE pbp.dim_team_season ADD COLUMN IF NOT EXISTS nfl_division text;
ALTER TABLE pbp.dim_team_season RENAME COLUMN division TO ncaa_division;

ALTER TABLE pbp.dim_team        DROP CONSTRAINT IF EXISTS dim_team_pkey;
ALTER TABLE pbp.dim_team        ADD  PRIMARY KEY (league, team_id);
ALTER TABLE pbp.dim_team_season DROP CONSTRAINT IF EXISTS dim_team_season_pkey;
ALTER TABLE pbp.dim_team_season ADD  PRIMARY KEY (league, team_id, season);
ALTER TABLE pbp.dim_conference  DROP CONSTRAINT IF EXISTS dim_conference_pkey;
ALTER TABLE pbp.dim_conference  ADD  PRIMARY KEY (league, conference_id);

-- ---------------------------------------------------------------- athlete dimension
-- One row per person across both leagues; this column says where he was seen. A player who
-- appears in both -- every drafted player in the window -- reads 'cfb+nfl', which is the
-- cross-league question this schema exists to make answerable.
ALTER TABLE pbp.dim_athlete ADD COLUMN IF NOT EXISTS leagues       text;
ALTER TABLE pbp.dim_athlete ADD COLUMN IF NOT EXISTS nfl_plays     integer;
ALTER TABLE pbp.dim_athlete ADD COLUMN IF NOT EXISTS date_of_birth date;
ALTER TABLE pbp.dim_athlete ADD COLUMN IF NOT EXISTS debut_year    smallint;
ALTER TABLE pbp.dim_athlete ADD COLUMN IF NOT EXISTS height_in     smallint;
ALTER TABLE pbp.dim_athlete ADD COLUMN IF NOT EXISTS weight_lb     smallint;

COMMENT ON COLUMN pbp.dim_athlete.leagues IS
  'Which corpora this athlete appears in: cfb | nfl | cfb+nfl. NOT part of the key -- ESPN
   athlete ids are one id space across both feeds (720/720 name agreement on a sampled
   overlap), so a player who went pro is ONE row spanning both, and that is the point.';
COMMENT ON COLUMN pbp.dim_team_season.nfl_division IS
  'The NFL''s eight divisions (AFC East, NFC West, ...). NULL for college, where the
   equivalent column is `division` and means FBS | FCS. Two columns rather than one because
   the two meanings do not survive being merged.';
COMMENT ON COLUMN pbp.special_teams_play.snapper_name IS
  'The long snapper, from the NFL gamebook''s "Center-C.Gresham". NULL for every college
   row: the college feed does not name him.';
COMMENT ON COLUMN pbp.special_teams_play.holder_name IS
  'The holder on a place kick, from the NFL gamebook''s "Holder-J.Ryan". NULL for every
   college row.';
COMMENT ON COLUMN pbp.special_teams_play.league IS
  'cfb | nfl. The two corpora share this table and are disjoint by game_id; play_uid is
   unique across both leagues AND both facts, which sql/verify.sql asserts rather than
   assumes.';

COMMIT;

\echo '=== league column populated (everything existing is cfb)'
SELECT 'special_teams_play' AS t, league, count(*) FROM pbp.special_teams_play GROUP BY 1,2
UNION ALL SELECT 'scrimmage_play', league, count(*) FROM pbp.scrimmage_play GROUP BY 1,2
UNION ALL SELECT 'drive', league, count(*) FROM pbp.drive GROUP BY 1,2
UNION ALL SELECT 'fact_game', league, count(*) FROM pbp.fact_game GROUP BY 1,2
ORDER BY 1,2;
