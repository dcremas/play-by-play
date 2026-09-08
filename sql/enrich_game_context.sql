-- Copies the three game-context columns from st.fact_game onto the fact table.
--
-- These are NOT in load_2_insert.sql's column list, so its TRUNCATE clears them and the
-- reload leaves them NULL. That is invisible until something joins on them: the
-- field-goal-by-surface check in verify_phase4.sql silently returns zero rows, because
-- every venue_id is NULL. Run this after any load_2_insert.sql, before build_snapshot.py.
UPDATE st.special_teams_play p
   SET venue_id        = g.venue_id,
       neutral_site    = g.neutral_site,
       conference_game = g.conference_game
  FROM st.fact_game g
 WHERE g.game_id = p.game_id;

ANALYZE st.special_teams_play;
