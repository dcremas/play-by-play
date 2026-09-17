"""Final score margins and win-loss records, derived -- because the warehouse has no scores.

There is no score column anywhere in this schema. `fact_game` stops at venue and attendance;
both facts carry only `score_diff_*`, which is the margin **before** the snap. So a final
margin has to be reconstructed as *the last play's pre-play margin plus whatever that play
itself scored*, and the special-teams fact has no points column either, so the points come
from the outcome flags:

    field_goal AND fg_made   -> +3 kicking     two_point AND converted -> +2 kicking
    pat        AND converted -> +1 kicking     returned_for_td         -> -6 (receiving side)
                                               defensive_conversion    -> -2 (receiving side)

**Plays are ordered by ESPN's sequenceNumber, carried in `play_uid`, not by period and clock.**
That is not a stylistic choice. Ordering by `(period, clock)` leaves 210 college games deriving
a 0-0 margin; ordering by sequence number leaves 27. College football has had no ties since
1996, so that count is the error rate, and it falls from 2.04% to 0.26% purely on the sort key
(a period-and-clock sort cannot separate plays sharing a clock reading, and overtime runs no
clock at all).

Three checks say the derivation works, none of them targeted:

    home win %        62.4 college / 55.0 NFL   -- published home-field advantage in both
    margin mode       3, then 7, then 10, 14, 21 -- the football score distribution
    derived ties      27 of 10,301 college (0.26%); 15 of 3,296 NFL, where ties are legal

**The known gap.** 1.9% of college games in `fact_game` have no plays in either fact and so
no derivable margin at all -- 6.4% in 2021 and 4.8% in 2022, under 1% in most seasons. Ohio
State's 2024 reads 12-2 rather than 14-2 for exactly this reason. So **win totals run low and
win rate is the metric to quote**; `scheduled - played` is exposed per team-season so a caller
can restrict to complete schedules. 1,240 of 1,513 team-seasons are complete; 41 miss two or
more games.

    from reports.margins import install
    install(con)                 # creates game_margin, team_game, team_record, team_schedule
    .venv/bin/python -m reports.margins           # prints the validation above
"""
from __future__ import annotations

import os

import duckdb

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(HOME, "data", "out", "pbp.duckdb")

# `try_cast` + `regexp_extract` rather than a plain cast: a reused sequenceNumber carries a
# `#n` suffix and a derived conversion row carries `:pat`, so the third colon-field is not
# always a bare integer. See README "Known limits" 10 and 12.
SEQ = "try_cast(regexp_extract(split_part(play_uid, ':', 3), '^[0-9]+') AS BIGINT)"

SQL = f"""
CREATE OR REPLACE TEMP VIEW _score_event AS
  SELECT game_id, league, play_uid, {SEQ} AS seq,
         offense_team_id            AS ref_team,
         score_diff_offense         AS diff_before,
         coalesce(points_scored, 0) AS pts_ref,     -- already signed from the OFFENCE's side
         'scrimmage'                AS fact
  FROM scrimmage
  UNION ALL
  SELECT game_id, league, play_uid, {SEQ} AS seq,
         kicking_team_id            AS ref_team,
         score_diff_kicking         AS diff_before,
         CASE WHEN play_kind = 'field_goal' AND fg_made   THEN  3 ELSE 0 END
       + CASE WHEN play_kind = 'pat'        AND converted THEN  1 ELSE 0 END
       + CASE WHEN play_kind = 'two_point'  AND converted THEN  2 ELSE 0 END
       + CASE WHEN returned_for_td                        THEN -6 ELSE 0 END
       + CASE WHEN play_kind = 'defensive_conversion' AND converted THEN -2 ELSE 0 END
         AS pts_ref,
         'special'                  AS fact
  FROM play;

CREATE OR REPLACE TEMP VIEW _last_play AS
SELECT game_id, league,
       arg_max(ref_team,    seq) AS ref_team,
       arg_max(diff_before, seq) AS diff_before,
       arg_max(pts_ref,     seq) AS pts_ref,
       arg_max(fact,        seq) AS fact
FROM _score_event WHERE seq IS NOT NULL GROUP BY game_id, league;

CREATE OR REPLACE TEMP VIEW game_margin AS
SELECT l.game_id, l.league, g.season, g.week, g.season_type, l.fact,
       g.home_team_id, g.away_team_id,
       CASE WHEN l.ref_team = g.home_team_id THEN  (l.diff_before + l.pts_ref)
                                             ELSE -(l.diff_before + l.pts_ref) END AS home_margin
FROM _last_play l JOIN fact_game g USING (game_id, league);

-- One row per team per game. A 0 margin is an unresolved derivation, not a tie, and leaves.
CREATE OR REPLACE TEMP VIEW team_game AS
SELECT league, season, week, season_type, home_team_id AS team_id,  home_margin AS margin,
       away_team_id AS opponent_id, true  AS at_home FROM game_margin WHERE home_margin <> 0
UNION ALL
SELECT league, season, week, season_type, away_team_id, -home_margin,
       home_team_id, false FROM game_margin WHERE home_margin <> 0;

-- Scheduled vs resolved, so a caller can tell a 6-win season from a 6-win season missing games.
CREATE OR REPLACE TEMP VIEW team_schedule AS
SELECT league, season, team_id, count(*) AS scheduled,
       count(*) FILTER (WHERE resolved) AS played,
       count(*) - count(*) FILTER (WHERE resolved) AS missing
FROM (
  SELECT g.league, g.season, g.home_team_id AS team_id,
         (m.game_id IS NOT NULL AND m.home_margin <> 0) AS resolved
  FROM fact_game g LEFT JOIN game_margin m USING (game_id, league)
  UNION ALL
  SELECT g.league, g.season, g.away_team_id,
         (m.game_id IS NOT NULL AND m.home_margin <> 0)
  FROM fact_game g LEFT JOIN game_margin m USING (game_id, league))
GROUP BY 1, 2, 3;

CREATE OR REPLACE TEMP VIEW team_record AS
SELECT t.league, t.season, t.team_id, s.scheduled, s.played, s.missing,
       sum((t.margin > 0)::int)                                   AS wins,
       count(*) - sum((t.margin > 0)::int)                        AS losses,
       100.0 * avg((t.margin > 0)::int)                           AS win_pct,
       avg(t.margin)                                              AS avg_margin,
       count(*) FILTER (WHERE t.season_type = 'postseason')        AS postseason_games,
       count(*) FILTER (WHERE abs(t.margin) <= 8)                  AS close_games,
       sum((t.margin > 0)::int) FILTER (WHERE abs(t.margin) <= 8)  AS close_wins
FROM team_game t JOIN team_schedule s USING (league, season, team_id)
GROUP BY 1, 2, 3, 4, 5, 6;
"""


def install(con: duckdb.DuckDBPyConnection) -> None:
    """Create game_margin, team_game, team_schedule and team_record as temp views."""
    con.execute(SQL)


def main() -> None:
    import pandas as pd
    pd.set_option("display.width", 200)
    con = duckdb.connect(DB, read_only=True)
    install(con)
    print("\n--- Derivation error rate: a 0 margin is impossible in college since 1996 ---")
    print(con.execute("""
        SELECT league, count(*) AS games,
               count(*) FILTER (WHERE home_margin = 0) AS zero_margin,
               round(100.0 * avg((home_margin = 0)::int), 2) AS pct,
               round(100.0 * avg((home_margin > 0)::int), 1) AS home_win_pct,
               round(avg(home_margin), 2) AS avg_home_margin
        FROM game_margin GROUP BY 1 ORDER BY 1""").df().to_string(index=False))
    print("\n--- Margin distribution: football scores cluster at 3 and 7 ---")
    print(con.execute("""
        SELECT abs(home_margin) AS margin, count(*) AS games FROM game_margin
        WHERE league = 'cfb' AND home_margin <> 0 GROUP BY 1 ORDER BY 2 DESC LIMIT 8
        """).df().to_string(index=False))
    print("\n--- Coverage: games in fact_game with no plays, by season (college) ---")
    print(con.execute("""
        SELECT g.season, count(*) AS scheduled,
               count(*) FILTER (WHERE m.game_id IS NULL) AS no_plays,
               count(*) FILTER (WHERE m.home_margin = 0) AS unresolved,
               round(100.0 * count(*) FILTER (WHERE m.game_id IS NULL OR m.home_margin = 0)
                     / count(*), 2) AS pct_lost
        FROM fact_game g LEFT JOIN game_margin m USING (game_id, league)
        WHERE g.league = 'cfb' AND g.season BETWEEN 2014 AND 2025
        GROUP BY 1 ORDER BY 1""").df().to_string(index=False))
    print("\n--- Spot check: seasons whose record is independently known ---")
    print(con.execute("""
        SELECT r.season, t.display_name AS team, r.wins, r.losses, r.missing
        FROM team_record r JOIN dim_team t USING (team_id)
        WHERE r.league = 'cfb' AND (r.season, t.display_name) IN (
          (2017,'UCF Knights'), (2018,'Clemson Tigers'), (2019,'LSU Tigers'),
          (2020,'Alabama Crimson Tide'), (2021,'Georgia Bulldogs'), (2022,'Georgia Bulldogs'),
          (2023,'Michigan Wolverines'), (2024,'Ohio State Buckeyes'))
        ORDER BY r.season""").df().to_string(index=False))
    print("  expected 13-0 / 15-0 / 15-0 / 13-0 / 14-1 / 15-0 / 15-0 / 14-2"
          "  (Ohio State 2024 is short two games that carry no plays)")


if __name__ == "__main__":
    main()
