-- The schema's caveats, stored as COMMENTs so `describe_table` can return them.
--
--     psql -d pbp -v schema=cfb -f comment_tables.sql
--     psql -d pbp -v schema=nfl -f comment_tables.sql
--
-- RUN ONCE PER CORPUS. The comments are the only place the traps are written down where
-- describe_table can hand them to a model at the moment it is deciding what to write, so a
-- schema without them is a schema whose caveats silently do not exist.
--
-- WHY THESE LIVE IN THE DATABASE RATHER THAN IN A PROMPT
-- ------------------------------------------------------
-- Every one of these is a rule that, if broken, produces a plausible wrong number
-- rather than an error. A prompt carrying them drifts from the schema the moment
-- either changes, and nothing catches it. A COMMENT sits on the column it is
-- about, travels with a pg_dump, and is read back by describe_table at the moment
-- the caller is deciding what to write. Same discipline the weather warehouse
-- uses for its units (see weather-sql-explorer/mcp_server/comment_tables.sql).
--
-- COMMENT REQUIRES OWNERSHIP of the object. Run as the role that owns the pbp
-- schema (dustincremascoli), not as pbp_ro.
--
-- Re-run after sql/split_leagues.sql: that file does DROP SCHEMA ... CASCADE, and
-- comments go with a dropped table.

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------- tables

COMMENT ON TABLE :schema.play_wide IS
'THE KICKS FACT with dimensions pre-joined -- kickoffs, punts, field goals and the '
'conversion family, one row per kick in this league. Prefer this over '
'special_teams_play: the (team_id, season) conference join is already resolved '
'here, and getting that join wrong is the single most likely way to get a wrong '
'answer from this warehouse. Derived from special_teams_play by '
'sql/split_leagues.sql; rebuild rather than repair. NULL IS MEANINGFUL on the '
'outcome flags -- see the column comments before writing a rate.';

COMMENT ON TABLE :schema.scrimmage_wide IS
'THE SCRIMMAGE FACT with dimensions pre-joined -- rushes, passes, sacks and '
'penalties, one row per play in this league. Disjoint from play_wide by '
'construction; play_uid is unique across both. Derived from scrimmage_play by '
'sql/split_leagues.sql. yards_gained on a turnover is the DEFENCE''s return, not the '
'offence''s gain -- exclude turnovers from any mean-yards measure.';

COMMENT ON TABLE :schema.special_teams_play IS
'The normalised kicks fact, 46 columns. play_wide is the same rows with the '
'dimensions attached and six derived columns; prefer it unless you are auditing. '
'play_text is never cleaned or repaired -- it is the audit trail.';

COMMENT ON TABLE :schema.scrimmage_play IS
'The normalised scrimmage fact. Needs no parser: play_kind is derived from ESPN''s '
'structured fields, and statYardage and down are 100% populated. Prefer '
'scrimmage_wide.';

COMMENT ON TABLE :schema.drive IS
'One row per drive. SPANS BOTH FACTS -- a drive that ends in a punt '
'contains the punt -- so plays_total counts every play in the drive and '
'plays_scrimmage only those that reached the scrimmage fact.';

COMMENT ON TABLE :schema.play_athlete IS
'play x role x athlete for the KICKS fact. This is the full truth about who was '
'involved; the athlete id columns on the fact table are a denormalised hot path '
'holding only the first athlete in each role.';

COMMENT ON TABLE :schema.scrimmage_athlete IS
'play x role x athlete for the SCRIMMAGE fact. Every role ESPN reports is kept, '
'including assistedBy, sackedBy, passDefender, forcedBy and recoverer. Many plays '
'have two tacklers and some have three, so counting tackles off the fact '
'table''s single tackler_athlete_id undercounts.';

COMMENT ON TABLE :schema.dim_athlete IS
'One row per athlete IN THIS CORPUS. Career grain WITHIN this league only -- first_season, '
'last_season, primary_team_id, primary_role and the play counts are computed from this '
'league''s plays alone. A player who appears in both corpora has a row in each and they are '
'NOT linked: there is no cross-league career here, by design. Built by '
'build_dims.py athlete --leagues <this league>, NOT by filtering a combined dimension -- '
'filtering would carry a primary_team_id chosen across both corpora, and team ids collide '
'between them, which put Patrick Mahomes at Arizona rather than Texas Tech.';

COMMENT ON TABLE :schema.dim_team IS
'One row per team in this corpus. team_id is unambiguous here: the leagues are separate '
'schemas, so the collisions that made a league-blind join dangerous -- team 2 is Auburn in '
'the college corpus and the Buffalo Bills in the NFL one -- cannot be reached from this '
'schema at all. display_name is the team''s MOST RECENT name in the window.';

COMMENT ON TABLE :schema.dim_team_season IS
'team x season, carrying conference membership. JOIN ON (team_id, season), NEVER '
'team_id alone: 83 of 275 college teams changed conference at least once in '
'the window, and a team-only join returned 492 Pac-12 punts for 2018 against a '
'true 730. The error drifts with the present day rather than staying put, which is '
'what makes it hard to notice. In college, rows that name a team absent from dim_team are FCS '
'programmes that never played an ingested game. That is expected.';

COMMENT ON TABLE :schema.dim_conference IS
'One row per conference in this corpus. conference_id is unambiguous here -- 8 is the SEC in '
'the college schema and the AFC in the NFL one, and the two never meet.';

COMMENT ON TABLE :schema.dim_venue IS
'One row per venue this league actually played in. Venue ids are a single shared id space '
'upstream -- a bowl game at AT&T Stadium is that same building -- so 35 venues appear in both '
'corpora under the same id. No lat/lon, only city, state and zip, so a weather join would '
'need geocoding first.';

COMMENT ON TABLE :schema.fact_game IS
'One row per game. fact_game.venue_id is THE ONLY DECLARED FOREIGN KEY in this '
'entire schema; the other fifteen joins are invariants of the build scripts, '
'measured by sql/verify.sql rather than enforced. There is no score column -- this '
'is a play-level corpus and a final score has to be derived from scoring plays.';

COMMENT ON TABLE :schema.season_status IS
'One row per season. is_in_progress is self-maintaining -- a season is in progress while its '
'most recent game is within 30 days -- so nothing has to be unset in January. The `plays` '
'column counts SPECIAL TEAMS plays only, because it is built off the kicks fact.';

-- ---------------------------------------------------------------- the NULL semantics
--
-- These four are the ones that produce a wrong number silently. Each is commented
-- on both wide tables where it appears.

COMMENT ON COLUMN :schema.play_wide.fg_made IS
'NULL MEANS NEGATED BY PENALTY, NOT MISSED. A coalesce(fg_made, false) counts a '
'wiped-out kick as a miss and understates every field goal percentage. Exclude '
'NULL from the denominator.';

COMMENT ON COLUMN :schema.play_wide.returned IS
'NULL MEANS TWO DIFFERENT THINGS depending on play_kind. On punts and kickoffs it '
'is an outcome the feed never stated -- about 10% of college punts and kickoffs and 3% '
'of NFL ones; known_limits() carries the current count -- and '
'those rows must be excluded from rate denominators, not counted as "did not '
'happen". On field goals and conversions it means the column does not apply. '
'Scope every returned/touchback question to play_kind IN (''punt'',''kickoff'').';

COMMENT ON COLUMN :schema.play_wide.touchback IS
'Only meaningful where returned IS NOT NULL. Averaging touchback::int over rows '
'whose outcome was never stated understates the rate by whatever share of the '
'season is unstated, which varies by season.';

COMMENT ON COLUMN :schema.play_wide.onside IS
'An onside kick is a DIFFERENT PLAY, not a kickoff outcome. Exclude it from '
'kickoff touchback and return rates or it drags both down.';

COMMENT ON COLUMN :schema.play_wide.return_yds IS
'NULL on a touchback is deliberate -- there was no return, which is not the same as '
'a zero-yard return.';

COMMENT ON COLUMN :schema.play_wide.yards_to_goal IS
'Yards to the opponent''s goal line at the snap. 0 IS A NULL SENTINEL, not the goal '
'line. Exclude it from any field-position analysis.';

COMMENT ON COLUMN :schema.play_wide.wallclock_utc IS
'Per-play UTC timestamp, 96.6% populated overall but only 66.9% in 2017. This is '
'what would make a play-level weather join possible; no weather is in this '
'warehouse.';

COMMENT ON COLUMN :schema.play_wide.parse_confidence IS
'How well the kick-text parser read this row. ''exact'' on 98.04% of college kicks '
'and 98.19% of NFL ones, from two separate dialect modules. Anything else means '
'read play_text before trusting the parsed columns.';

COMMENT ON COLUMN :schema.play_wide.play_text IS
'The raw ESPN text, never cleaned, never repaired. It is the audit trail and the '
'reason the parsed columns are re-derivable.';

COMMENT ON COLUMN :schema.play_wide.both_top_division IS
'Both teams in the top division. The college corpus deliberately includes '
'FBS-vs-FCS games, so set this for any kicker- or player-quality question. '
'CONSTANT TRUE for the NFL, which has no second division -- so filtering on it there '
'changes nothing.';

COMMENT ON COLUMN :schema.play_wide.venue_indoor IS
'A STADIUM property, not a game condition. A retractable roof reads true whether '
'or not it was open that day, and 5 of the 18 indoor venues are retractable. '
'false is a clean "weather applied"; true means "cannot say".';

COMMENT ON COLUMN :schema.play_wide.kicking_team_id IS
'Always the kicking team, on every play_kind. Unambiguous within this schema; team ids '
'collide with the other league''s, which this schema cannot reach.';

COMMENT ON COLUMN :schema.play_wide.is_clutch IS
'4th quarter or later, margin within 8, under five minutes. Derived once here so '
'every consumer cuts it the same way.';

COMMENT ON COLUMN :schema.play_wide.fg_dist_bucket IS
'5-yard bucket keyed by its LOWER BOUND so it sorts numerically. 15 means "<20" '
'and 60 means "60+"; both ends are open.';

-- scrimmage_wide

COMMENT ON COLUMN :schema.scrimmage_wide.yards_gained IS
'ESPN statYardage, 100% populated. ON A TURNOVER THIS IS THE DEFENCE''S RETURN, '
'NOT THE OFFENCE''S GAIN -- ESPN credits 35 yards to the offence row of a 35-yard '
'pick-six. Hold turnovers out of every mean-yards measure. Four impossible values '
'in the source (11,131 and 561 gained, -5,114 and 1,105 on penalties) are NULLed '
'rather than clamped, because a clamped value is indistinguishable from a real one.';

COMMENT ON COLUMN :schema.scrimmage_wide.points_scored IS
'Signed, from the OFFENCE''s perspective: a pick-six is negative. Summing this per '
'offense_team_id therefore misses points the defence scored -- flip the sign onto '
'defense_team_id for those rows.';

COMMENT ON COLUMN :schema.scrimmage_wide.is_complete IS
'Pass plays only. An interception IS a pass attempt that was not completed, which '
'matches NCAA completion percentage. A sack is NULL -- not a pass attempt in NCAA '
'accounting.';

COMMENT ON COLUMN :schema.scrimmage_wide.first_down_gained IS
'Possession is tested first: after a turnover ESPN writes end.down = 1 for the side '
'that took the ball away, so a naive read of end.down credits the offence with a '
'first down on an interception.';

COMMENT ON COLUMN :schema.scrimmage_wide.end_yards_to_goal IS
'Measured from END_TEAM_ID''s perspective, so on a turnover it flips to the other '
'goal line.';

COMMENT ON COLUMN :schema.scrimmage_wide.offense_team_id IS
'From teamParticipants, 99.78% populated. NOT start.team.id, which on a kick is '
'the kicking team.';

COMMENT ON COLUMN :schema.scrimmage_wide.play_kind IS
'DERIVED (rush | pass | sack | penalty | other), not a copy of play_type_espn. '
'play_type_espn is kept beside it so the derivation is auditable.';

COMMENT ON COLUMN :schema.scrimmage_wide.play_text IS
'The raw ESPN text, never cleaned. Nothing is parsed out of it on this fact.';

COMMENT ON COLUMN :schema.scrimmage_wide.both_top_division IS
'Both teams in the top division. Constant true for the NFL; see the same column on '
'play_wide.';

COMMENT ON COLUMN :schema.scrimmage_wide.yards_to_goal IS
'0 is a null sentinel, not the goal line. Exclude it from field-position analysis.';

COMMENT ON COLUMN :schema.scrimmage_wide.distance_bucket IS
'short (<=3) | medium (4-7) | long (8+). Derived once so every consumer cuts down '
'and distance the same way.';

COMMENT ON COLUMN :schema.scrimmage_wide.field_zone IS
'red zone (<=20 to goal) | opponent half | own half.';

-- dimensions

COMMENT ON COLUMN :schema.dim_athlete.known_name IS
'ESPN''s name for the athlete. GROUP BY athlete_id AND LABEL WITH THIS -- never the '
'reverse. 109 names are shared by more than one athlete and that is correct; they '
'are different people.';

COMMENT ON COLUMN :schema.dim_athlete.text_name_confidence IS
'How well the name parsed out of play text agrees with ESPN''s. A low value is a '
'mangled-name guard firing, kept visible rather than cosmetically repaired.';

COMMENT ON COLUMN :schema.dim_athlete.primary_role IS
'Describes the PLAYER, not the row that linked them. patScorer is canonicalised to '
'kicker before the vote, because a placekicker takes far more extra points than '
'field goals and counting the raw role literally would relabel most kickers.';

COMMENT ON COLUMN :schema.dim_venue.indoor IS
'A stadium property, not a game condition. 18 indoor / 183 outdoor / 0 unknown; 5 '
'of the 18 are retractable and read true whether or not the roof was open.';

COMMENT ON COLUMN :schema.drive.start_yards_to_goal IS
'Taken from the FIRST PLAY''s start.yardsToEndzone, not from ESPN''s '
'drive.start.yardLine -- that column is measured in a fixed direction rather than '
'from the possessing team''s own goal, so it reads 25 for one team''s own 25 and 76 '
'for the other''s own 24.';

COMMENT ON COLUMN :schema.drive.plays_total IS
'Every play in the drive, including the punt or field goal that ended it. '
'plays_scrimmage counts only those that reached the scrimmage fact.';

\echo '=== comments applied'
SELECT c.relname AS table_name,
       count(*) FILTER (WHERE d.objsubid > 0) AS column_comments,
       (obj_description(c.oid, 'pg_class') IS NOT NULL) AS has_table_comment
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_description d ON d.objoid = c.oid
WHERE n.nspname = 'pbp' AND c.relkind = 'r'
GROUP BY c.relname, c.oid
ORDER BY c.relname;
