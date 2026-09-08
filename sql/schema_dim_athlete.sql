-- The shared athlete dimension, rebuilt over BOTH fact tables (PLAN.md §10d).
--
-- Supersedes the st.dim_athlete block in sql/enrich.sql, which built it for special teams
-- alone. That version is left in place for history; this file is the current definition.
--
-- Two things changed in the rebuild, both deliberate:
--
-- 1. SCOPE. The dimension is now the union of the athletes on st.special_teams_play and
--    st.scrimmage_play -- 33,891 and 58,800 respectively, overlapping by 29,812, for a union
--    of 62,879. A receiver who also returns kicks must be ONE row or every cross-phase
--    question double-counts him, which is the entire reason this project keys on ESPN
--    athlete ids rather than name strings.
--
-- 2. NAMES. known_name used to be voted out of play text and reached 25.9% of athletes
--    (8,794 of 33,891). It is now ESPN's own displayName, fetched once by
--    scripts/fetch_athletes.py, with the voted name kept alongside it in text_name.
--    The voted name is NOT redundant: it is the only independent evidence that an athlete id
--    is attached to the right play, so a disagreement between the two columns is a signal
--    worth having rather than something to resolve away.

DROP TABLE IF EXISTS st.dim_athlete CASCADE;
CREATE TABLE st.dim_athlete (
  athlete_id           bigint PRIMARY KEY,
  known_name           text,        -- ESPN displayName; falls back to text_name if unfetched
  full_name            text,
  position             text,        -- QB | RB | WR | LB | ...
  jersey               text,        -- text: leading zeros are real, and it is not a number
  text_name            text,        -- voted out of play text, the pre-Stage-3 known_name
  text_name_confidence numeric,     -- share of that athlete's plays agreeing on the person
  primary_role         text,        -- modal role across BOTH bridges
  primary_team_id      integer,
  first_season         smallint,
  last_season          smallint,
  st_plays             integer,     -- distinct special-teams plays
  scrimmage_plays      integer      -- distinct scrimmage plays
);

CREATE INDEX ON st.dim_athlete (known_name);
CREATE INDEX ON st.dim_athlete (position);
CREATE INDEX ON st.dim_athlete (primary_team_id);

COMMENT ON COLUMN st.dim_athlete.known_name IS
  'ESPN displayName from scripts/fetch_athletes.py. Where the fetch has no record this falls
   back to text_name, the name voted out of play text -- so the column is always the best
   available name, and text_name tells you which regime produced it.';
COMMENT ON COLUMN st.dim_athlete.text_name IS
  'Voted out of the play text the parser extracted, requiring the modal name to be seen twice
   and hold a 60% majority. This was known_name before Stage 3. Kept because it is derived
   from a completely different source than known_name: where the two disagree, suspect the
   athlete-to-play link rather than the spelling.';
COMMENT ON COLUMN st.dim_athlete.primary_role IS
  'Modal role over both bridges. Now that `position` exists it is the weaker of the two for
   describing a player -- primary_role says what he did most, position says what he is.';
COMMENT ON COLUMN st.dim_athlete.st_plays IS
  'DISTINCT special-teams plays. Before Stage 3 this counted role-entries rather than plays,
   which double-counted anyone holding two roles on one play; the two agree closely on kicks
   but not on scrimmage, so both columns now count plays.';
