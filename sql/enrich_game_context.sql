-- Copies the three game-context columns from pbp.fact_game onto BOTH fact tables.
--
-- These are NOT in the column lists of load_2_insert.sql or load_scrimmage_2_insert.sql, so
-- their TRUNCATE clears them and the reload leaves them NULL. That is invisible until
-- something joins on them: the field-goal-by-surface check in verify_phase4.sql silently
-- returns zero rows, because every venue_id is NULL. Run this after either loader, before
-- build_snapshot.py.
--
-- The join carries `league` as well as game_id. Game ids do not in fact collide across the
-- two leagues -- 0 overlaps between 3,297 NFL ids and 10,470 college ones -- but relying on
-- that silently is exactly the habit sql/verify.sql exists to break, and the extra
-- predicate costs nothing.
UPDATE pbp.special_teams_play p
   SET venue_id        = g.venue_id,
       neutral_site    = g.neutral_site,
       conference_game = g.conference_game
  FROM pbp.fact_game g
 WHERE g.game_id = p.game_id AND g.league = p.league;

ANALYZE pbp.special_teams_play;

UPDATE pbp.scrimmage_play p
   SET venue_id        = g.venue_id,
       neutral_site    = g.neutral_site,
       conference_game = g.conference_game
  FROM pbp.fact_game g
 WHERE g.game_id = p.game_id AND g.league = p.league;

ANALYZE pbp.scrimmage_play;
