-- Phase 4: athlete identity on the fact table.
-- Names collide across ten seasons and 130+ teams; the ESPN athlete id is the stable key.
-- ESPN exposes no 'blocker' role, so blocker identity remains a parsed name string only.

DROP TABLE IF EXISTS st.dim_athlete CASCADE;
CREATE TABLE st.dim_athlete (
  athlete_id      bigint PRIMARY KEY,
  known_name      text,        -- as the play text writes it; derived, not from a roster feed
  primary_role    text,        -- kicker | punter | returner | tackler | ...
  primary_team_id integer,
  first_season    smallint,
  last_season     smallint,
  st_plays        integer
);

-- Full-fidelity bridge: a play has many participants in many roles.
DROP TABLE IF EXISTS st.play_athlete CASCADE;
CREATE TABLE st.play_athlete (
  play_uid   text NOT NULL,
  role       text NOT NULL,
  athlete_id bigint NOT NULL,
  ordinal    smallint,
  PRIMARY KEY (play_uid, role, athlete_id)
);
CREATE INDEX ON st.play_athlete (athlete_id);
CREATE INDEX ON st.play_athlete (role);

ALTER TABLE st.special_teams_play
  ADD COLUMN IF NOT EXISTS kicker_athlete_id   bigint,
  ADD COLUMN IF NOT EXISTS returner_athlete_id bigint,
  ADD COLUMN IF NOT EXISTS tackler_athlete_id  bigint,
  ADD COLUMN IF NOT EXISTS venue_id            integer,
  ADD COLUMN IF NOT EXISTS conference_game     boolean,
  ADD COLUMN IF NOT EXISTS neutral_site        boolean;

COMMENT ON COLUMN st.special_teams_play.kicker_athlete_id IS
  'ESPN athlete id from the play participants feed (role kicker for kickoffs/FGs, punter for
   punts). Prefer this over kicker_name for any grouping -- names collide.';
COMMENT ON COLUMN st.dim_athlete.known_name IS
  'Derived from the play text the parser extracted, not from a roster feed. A player whose
   name the feed renders inconsistently may appear under its most frequent form.';
