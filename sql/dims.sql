-- Phase 4 dimensions.
CREATE SCHEMA IF NOT EXISTS pbp;

DROP TABLE IF EXISTS pbp.dim_conference CASCADE;
CREATE TABLE pbp.dim_conference (
  conference_id   integer PRIMARY KEY,
  conference_name text NOT NULL,
  short_name      text
);

-- Slowly changing on purpose. Between 2014 and 2026 the Pac-12 went 12 teams -> 2 -> 8, the
-- Big Ten 14 -> 18, the Big 12 10 -> 16; 83 of 275 teams changed conference at least once.
-- Never denormalise conference onto a team-only dimension.
DROP TABLE IF EXISTS pbp.dim_team_season CASCADE;
CREATE TABLE pbp.dim_team_season (
  team_id         integer NOT NULL,
  season          smallint NOT NULL,
  conference_id   integer,
  conference_name text,
  division        text,          -- FBS | FCS
  PRIMARY KEY (team_id, season)
);

DROP TABLE IF EXISTS pbp.dim_team CASCADE;
CREATE TABLE pbp.dim_team (
  team_id      integer PRIMARY KEY,
  display_name text
);

DROP TABLE IF EXISTS pbp.dim_venue CASCADE;
CREATE TABLE pbp.dim_venue (
  venue_id   integer PRIMARY KEY,
  venue_name text,
  city       text,
  state      text,
  zip        text,
  country    text,
  surface    text               -- grass | turf
);

DROP TABLE IF EXISTS pbp.fact_game CASCADE;
CREATE TABLE pbp.fact_game (
  game_id         bigint PRIMARY KEY,
  season          smallint NOT NULL,
  week            smallint,
  season_type     text,
  kickoff_utc     timestamptz,
  home_team_id    integer,
  away_team_id    integer,
  venue_id        integer REFERENCES pbp.dim_venue(venue_id),
  attendance      integer,
  neutral_site    boolean,
  conference_game boolean
);
CREATE INDEX ON pbp.fact_game (season, week);
CREATE INDEX ON pbp.fact_game (venue_id);

COMMENT ON TABLE pbp.dim_team_season IS
  'Conference membership BY SEASON. Joining a play to a conference must go through
   (team_id, season) -- a team-only join silently backdates 2024 realignment across all
   ten seasons.';
