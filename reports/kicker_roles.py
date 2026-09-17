"""Who kicks off -- the placekicker, the punter, or a third man nobody lists as anything.

ESPN's `dim_athlete.position` knows two special-teams positions, `PK` and `P`. It has no
label for a kickoff specialist, and it gives one to players who are plainly nothing else:
193 athletes in this corpus are listed `PK`, kicked off 20+ times in a season, and never
attempted a field goal or an extra point in it. So the role split cannot be read off the
roster. It has to be measured from the kicks themselves, which is what this module does.

The unit is the **team-season**, because that is the unit the decision is made at -- a
staff assigns the kickoff job in August and 90.8% of team-games are then kicked off by the
man who led the team for the year. Within each team-season every athlete who took a kick
for that team is scored on three workloads:

    pk   = field goals + extra points     (the placekicking job, counted together --
                                           96.2% of the time it is one man doing both)
    ko   = kickoffs
    punt = punts

and three role leaders fall out: `pk1`, `ko1`, `p1`. The question the user asks is the
relationship between `pk1` and `ko1`, with `p1` as the third candidate.

Three things this module is careful about, each of which would change the answer:

  * **Conference is resolved per season, never per team.** `kicking_conference` on the
    snapshot is already joined on (team_id, season) -- see README, "Conference realignment".
    A team-only join would put 2014 Texas in the SEC and 2024 Oregon in the Pac-12.
  * **"Power 4" is not a constant.** The Pac-12 was a power conference through 2023 and
    was 12 of the ~64 power programs in every season before 2024. `--scope power`
    (the default) includes it through 2023 and drops it after; `--scope p4` is the strict
    four-conference reading. The two differ by 114 team-seasons and by 0.0 points on the
    headline, which is the useful thing to know about the choice.
  * **Rows with no kicker id are dropped from numerator and denominator both**, never
    counted as an unknown fourth kicker. That is 0.6% of kickoffs and 1.2% of placekicks
    in scope, reported on the coverage panel rather than assumed away.

Seasons are 2014-2025. 2026 is in progress -- a team has kicked off eight times -- and a
volume floor would not save it, so it is excluded by default rather than filtered.

    .venv/bin/python -m reports.kicker_roles
    .venv/bin/python -m reports.kicker_roles --scope p4
    .venv/bin/python -m reports.kicker_roles --league nfl --scope all
    .venv/bin/python -m reports.kicker_roles --csv-dir data/out/kicker_roles
"""
from __future__ import annotations

import argparse
import os

import duckdb
import numpy as np
import pandas as pd

from .margins import install as install_margins

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(HOME, "data", "out", "pbp.duckdb")

P4 = ("'Atlantic Coast Conference'", "'Big Ten Conference'",
      "'Big 12 Conference'", "'Southeastern Conference'")
P4_IN = f"({', '.join(P4)})"

# Every scope is a predicate over `play`. The league filter is added separately so
# `--league nfl --scope all` is the only sane NFL combination and the others fail loudly.
SCOPES = {
    "power": (f"kicking_conference IN {P4_IN} "
              f"OR (kicking_conference = 'Pac-12 Conference' AND season <= 2023)"),
    "p4":    f"kicking_conference IN {P4_IN}",
    "g5":    (f"kicking_ncaa_division = 'FBS' AND kicking_conference NOT IN {P4_IN} "
              f"AND kicking_conference <> 'Pac-12 Conference'"),
    "fbs":   "kicking_ncaa_division = 'FBS'",
    "all":   "true",
}

KINDS = "('kickoff', 'field_goal', 'pat', 'punt')"


def build(con, league: str, scope: str, seasons: tuple[int, int],
          min_ko: int, min_pk: int, min_punt: int) -> str:
    """Create the athlete-season, team-season and tagged-kickoff views. Returns the floor."""
    where = SCOPES[scope]
    lo, hi = seasons
    con.execute(f"""
    CREATE OR REPLACE TEMP VIEW scoped AS
    SELECT season, kicking_team_id AS team_id, kicking_team AS team,
           kicking_conference AS conf, kicker_athlete_id AS aid, play_kind
    FROM play
    WHERE league = '{league}' AND play_kind IN {KINDS}
      AND season BETWEEN {lo} AND {hi} AND ({where});

    CREATE OR REPLACE TEMP VIEW athlete_season AS
    SELECT season, team_id, any_value(team) AS team, any_value(conf) AS conf, aid,
           count(*) FILTER (WHERE play_kind = 'kickoff')                 AS ko,
           count(*) FILTER (WHERE play_kind = 'field_goal')              AS fg,
           count(*) FILTER (WHERE play_kind = 'pat')                     AS pat,
           count(*) FILTER (WHERE play_kind IN ('field_goal', 'pat'))    AS pk,
           count(*) FILTER (WHERE play_kind = 'punt')                    AS punt
    FROM scoped WHERE aid IS NOT NULL GROUP BY season, team_id, aid;
    """)

    # arg_max over a composite key: the primary workload dominates, the secondary breaks
    # a tie. Without the tiebreak, a team whose two kickers split 46/46 placekicks would
    # flip roles between runs and the typology would not be reproducible.
    con.execute("""
    CREATE OR REPLACE TEMP VIEW leaders AS
    SELECT season, team_id,
           arg_max(aid, pk   * 100000 + ko) FILTER (WHERE pk   > 0) AS pk1,
           arg_max(aid, ko   * 100000 + pk) FILTER (WHERE ko   > 0) AS ko1,
           arg_max(aid, punt * 100000 + ko) FILTER (WHERE punt > 0) AS p1
    FROM athlete_season GROUP BY season, team_id;

    CREATE OR REPLACE TEMP VIEW ts AS
    SELECT s.season, s.team_id, any_value(s.team) AS team, any_value(s.conf) AS conf,
           sum(s.ko) AS ko_tot, sum(s.pk) AS pk_tot, sum(s.fg) AS fg_tot,
           sum(s.pat) AS pat_tot, sum(s.punt) AS punt_tot,
           count(*) FILTER (WHERE s.ko > 0) AS n_ko_men,
           any_value(l.pk1) AS pk1, any_value(l.ko1) AS ko1, any_value(l.p1) AS p1,
           coalesce(any_value(a.ko),   0) AS ko_by_pk1,
           coalesce(any_value(a.pk),   0) AS pk_by_pk1,
           coalesce(any_value(c.ko),   0) AS ko_by_p1,
           coalesce(any_value(b.ko),   0) AS ko_by_ko1,
           coalesce(any_value(b.pk),   0) AS pk_by_ko1,
           coalesce(any_value(b.punt), 0) AS punt_by_ko1
    FROM athlete_season s
    JOIN leaders l USING (season, team_id)
    LEFT JOIN athlete_season a ON a.season = s.season AND a.team_id = s.team_id AND a.aid = l.pk1
    LEFT JOIN athlete_season b ON b.season = s.season AND b.team_id = s.team_id AND b.aid = l.ko1
    LEFT JOIN athlete_season c ON c.season = s.season AND c.team_id = s.team_id AND c.aid = l.p1
    GROUP BY s.season, s.team_id;
    """)

    floor = f"ko_tot >= {min_ko} AND pk_tot >= {min_pk} AND punt_tot >= {min_punt}"

    # The typology is mutually exclusive and ordered: a man who is both the placekicker
    # and the kickoff leader is "unified" even if he also punts, because the question
    # asked is about the placekicker first.
    con.execute(f"""
    CREATE OR REPLACE TEMP VIEW eligible AS
    SELECT *, ko_by_pk1::double / ko_tot AS pk1_ko_share,
              ko_by_p1::double  / ko_tot AS p1_ko_share,
              pk_by_ko1::double / pk_tot AS ko1_pk_share,
           CASE WHEN pk1 = ko1                 THEN 'placekicker also kicks off'
                WHEN p1  = ko1                 THEN 'punter kicks off'
                ELSE 'kickoff specialist' END  AS model
    FROM ts WHERE {floor};

    CREATE OR REPLACE TEMP VIEW kickoff_tagged AS
    SELECT p.*, e.model,
           CASE WHEN p.kicker_athlete_id = e.pk1 THEN 'placekicker'
                WHEN p.kicker_athlete_id = e.p1  THEN 'punter'
                WHEN p.kicker_athlete_id = e.ko1 THEN 'kickoff specialist'
                ELSE 'someone else' END AS who
    FROM play p JOIN eligible e ON e.season = p.season AND e.team_id = p.kicking_team_id
    WHERE p.league = '{league}' AND p.play_kind = 'kickoff'
      AND p.kicker_athlete_id IS NOT NULL;

    CREATE OR REPLACE TEMP VIEW player_season AS
    SELECT s.*, d.known_name, d.position,
           CASE WHEN s.ko   >= 20 AND s.pk < 5 AND s.punt < 5 THEN 'kickoff specialist'
                WHEN s.pk   >= 20 AND s.ko >= 20              THEN 'placekicker who kicks off'
                WHEN s.pk   >= 20 AND s.ko < 5                THEN 'placekicker only'
                WHEN s.punt >= 20 AND s.ko >= 20              THEN 'punter who kicks off'
                WHEN s.punt >= 20 AND s.ko < 5                THEN 'punter only'
                ELSE 'mixed / low volume' END AS archetype
    FROM athlete_season s JOIN eligible e USING (season, team_id)
    LEFT JOIN dim_athlete d ON d.athlete_id = s.aid;
    """)
    return floor


def build_success(con, league: str, min_scheduled: int = 9) -> None:
    """Join each eligible team-season to its derived win-loss record.

    Records are derived, not read -- the warehouse has no score column at all. See
    `reports/margins.py` for the derivation and its 0.26% error rate. The consequence that
    matters here: 1.9% of college games carry no plays, so **win rate is the metric to
    quote and win totals run low**. `missing` is carried through so any panel can be
    re-run on complete schedules only.
    """
    install_margins(con)
    con.execute(f"""
    CREATE OR REPLACE TEMP VIEW success AS
    SELECT e.*, r.scheduled, r.played, r.missing, r.wins, r.losses,
           r.win_pct, r.avg_margin, r.postseason_games, r.close_games, r.close_wins,
           CASE WHEN r.win_pct >= 85 THEN 'a. .850+ (elite)'
                WHEN r.win_pct >= 70 THEN 'b. .700-.849'
                WHEN r.win_pct >= 50 THEN 'c. .500-.699'
                WHEN r.win_pct >= 30 THEN 'd. .300-.499'
                ELSE                      'e. under .300' END AS tier
    FROM eligible e
    JOIN team_record r ON r.league = '{league}' AND r.season = e.season
                      AND r.team_id = e.team_id
    WHERE r.scheduled >= {min_scheduled};
    """)


def two_sample(con, metric: str, where: str = "1=1") -> dict:
    """Difference in `metric` between the shared and dedicated models, with a CI.

    Reported as a confidence interval rather than a verdict, because the finding here is an
    absence: an interval straddling zero is the answer, and a bare point estimate hides it.
    """
    d = con.execute(f"SELECT model, {metric} AS v FROM success "
                    f"WHERE model IN ('placekicker also kicks off','kickoff specialist') "
                    f"AND {where}").df()
    u = d[d.model == 'placekicker also kicks off'].v
    k = d[d.model == 'kickoff specialist'].v
    se = float(np.sqrt(u.var(ddof=1) / len(u) + k.var(ddof=1) / len(k)))
    diff = float(u.mean() - k.mean())
    return {"metric": metric, "shared": round(float(u.mean()), 2),
            "dedicated": round(float(k.mean()), 2), "diff": round(diff, 2),
            "t": round(diff / se, 2), "ci_lo": round(diff - 1.96 * se, 2),
            "ci_hi": round(diff + 1.96 * se, 2)}


def within_program(con, metric: str, min_seasons: int = 6) -> dict:
    """The same difference, with every program compared only against itself.

    This is the panel that matters. A raw split between models is a comparison of *programs*
    -- and programs that recruit a kicker with range both win more and keep the kickoffs
    unified, so the raw gap is a selection effect before it is anything else. Restricting to
    programs that ran both models removes program quality entirely.
    """
    fe = con.execute(f"""
        WITH dual AS (
          SELECT team_id FROM success
          WHERE model IN ('placekicker also kicks off','kickoff specialist')
          GROUP BY team_id HAVING count(DISTINCT model) = 2 AND count(*) >= {min_seasons})
        SELECT s.team_id, any_value(s.team) AS team, s.model, avg(s.{metric}) AS v
        FROM success s JOIN dual USING (team_id)
        WHERE s.model IN ('placekicker also kicks off','kickoff specialist')
        GROUP BY s.team_id, s.model""").df()
    piv = fe.pivot(index=["team_id", "team"], columns="model", values="v")
    d = (piv["placekicker also kicks off"] - piv["kickoff specialist"]).dropna()
    se = float(d.std(ddof=1) / np.sqrt(len(d)))
    return {"metric": metric, "programs": len(d), "mean_diff": round(float(d.mean()), 2),
            "median_diff": round(float(d.median()), 2), "t": round(float(d.mean()) / se, 2),
            "ci_lo": round(float(d.mean()) - 1.96 * se, 2),
            "ci_hi": round(float(d.mean()) + 1.96 * se, 2),
            "programs_better_shared": int((d > 0).sum())}


# Each panel is (title, sql). Kept as data so --csv-dir can dump them all by name.
PANELS: dict[str, tuple[str, str]] = {
    "coverage": ("Coverage -- kicks in scope, and how many name no kicker", """
        SELECT play_kind, count(*) AS kicks,
               count(*) FILTER (WHERE aid IS NULL) AS no_kicker_id,
               round(100.0 * avg((aid IS NULL)::int), 2) AS pct_unattributed
        FROM scoped GROUP BY 1 ORDER BY 2 DESC"""),

    "eligibility": ("Eligibility -- team-seasons before and after the volume floor", """
        SELECT (SELECT count(*) FROM ts)       AS team_seasons,
               (SELECT count(*) FROM eligible) AS met_floor,
               (SELECT round(avg(n_ko_men), 2) FROM eligible) AS avg_men_kicking_off"""),

    "headline": ("Headline -- who takes the lion's share of the kickoffs", """
        SELECT count(*) AS team_seasons,
          round(100.0 * avg((model = 'placekicker also kicks off')::int), 1) AS pct_placekicker,
          round(100.0 * avg((model = 'kickoff specialist')::int), 1)         AS pct_specialist,
          round(100.0 * avg((model = 'punter kicks off')::int), 1)           AS pct_punter,
          round(100.0 * avg(pk1_ko_share), 1) AS avg_pk_share_of_kickoffs,
          round(100.0 * avg(ko1_pk_share), 1) AS avg_kickoff_leader_share_of_placekicks,
          round(100.0 * avg(p1_ko_share), 1)  AS avg_punter_share_of_kickoffs
        FROM eligible"""),

    "distribution": ("Distribution -- the placekicker's share of his team's kickoffs", """
        SELECT CASE WHEN pk1_ko_share = 0 THEN '0%'
                    WHEN pk1_ko_share >= 1 THEN '100%'
                    ELSE (10 * floor(10 * pk1_ko_share))::int::text
                         || '-' || (10 * floor(10 * pk1_ko_share) + 10)::int::text || '%'
               END AS share_band,
               min(pk1_ko_share) AS lo, count(*) AS team_seasons,
               round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
        FROM eligible GROUP BY 1 ORDER BY lo"""),

    "by_season": ("By season -- is the split getting more or less common", """
        SELECT season, count(*) AS team_seasons,
          round(100.0 * avg((model = 'placekicker also kicks off')::int), 1) AS pct_placekicker,
          round(100.0 * avg((model = 'kickoff specialist')::int), 1)         AS pct_specialist,
          round(100.0 * avg((model = 'punter kicks off')::int), 1)           AS pct_punter,
          round(100.0 * avg(pk1_ko_share), 1)                                AS avg_pk_share,
          round(100.0 * avg((pk1_ko_share >= 0.9)::int), 1)                  AS pct_pk_does_90_plus,
          round(100.0 * avg((pk1_ko_share <= 0.1)::int), 1)                  AS pct_pk_does_10_minus
        FROM eligible GROUP BY 1 ORDER BY 1"""),

    "punters": ("Punters and kickoffs -- how much of the job they actually take", """
        SELECT count(*) AS team_seasons,
          round(100.0 * avg((ko_by_p1 = 0)::int), 1)                  AS pct_never,
          round(100.0 * avg((ko_by_p1 BETWEEN 1 AND 4)::int), 1)      AS pct_1_to_4_situational,
          round(100.0 * avg((ko_by_p1 >= 5 AND p1_ko_share < 0.5)::int), 1) AS pct_5_plus_minority,
          round(100.0 * avg((p1_ko_share >= 0.5)::int), 1)            AS pct_majority,
          round(100.0 * avg((pk1 = p1)::int), 2)                      AS pct_punter_is_also_placekicker
        FROM eligible"""),

    "specialist_profile": ("The third man -- what else he does, when he exists", """
        SELECT count(*) AS team_seasons,
          round(avg(ko_by_ko1), 1)   AS his_kickoffs,
          round(avg(pk_by_ko1), 1)   AS his_placekicks,
          round(avg(punt_by_ko1), 1) AS his_punts,
          round(100.0 * avg(((pk_by_ko1 + punt_by_ko1) = 0)::int), 1) AS pct_kickoffs_only
        FROM eligible WHERE model = 'kickoff specialist'"""),

    "kickoff_quality": ("Does the split buy a better kickoff? Outcomes by who kicked", """
        SELECT who, count(*) AS kickoffs,
          round(100.0 * avg(onside::int), 2) AS pct_onside,
          round(100.0 * avg(touchback::int) FILTER (WHERE NOT onside AND returned IS NOT NULL), 1)
            AS touchback_pct,
          round(avg(kickoff_yds) FILTER (WHERE NOT onside), 1) AS avg_kick_yds
        FROM kickoff_tagged GROUP BY 1 ORDER BY 2 DESC"""),

    "placekick_quality": ("The placekicker himself, by whether he also kicks off", """
        SELECT e.model, count(*) AS fg_att,
          round(100.0 * avg(p.fg_made::int), 1) AS fg_pct,
          round(avg(p.fg_distance_yds), 1)      AS avg_distance,
          count(*) FILTER (WHERE p.fg_distance_yds >= 50) AS att_50_plus,
          round(100.0 * avg(p.fg_made::int) FILTER (WHERE p.fg_distance_yds >= 50), 1)
            AS fg_pct_50_plus
        FROM play p JOIN eligible e ON e.season = p.season AND e.team_id = p.kicking_team_id
        WHERE p.play_kind = 'field_goal' AND p.fg_made IS NOT NULL
          AND p.kicker_athlete_id = e.pk1
        GROUP BY 1 ORDER BY 2 DESC"""),

    "archetypes": ("Player-seasons by archetype -- and what ESPN calls each one", """
        SELECT archetype, count(*) AS player_seasons, count(DISTINCT aid) AS people,
          round(avg(ko), 1) AS ko, round(avg(fg), 1) AS fg,
          round(avg(pat), 1) AS pat, round(avg(punt), 1) AS punt,
          mode(position) AS espn_position
        FROM player_season GROUP BY 1 ORDER BY 2 DESC"""),

    "pipeline": ("Is the specialist job an apprenticeship?", """
        WITH spec AS (SELECT DISTINCT aid FROM player_season WHERE archetype = 'kickoff specialist'),
        promoted AS (SELECT DISTINCT a.aid FROM player_season a JOIN player_season b
                       ON b.aid = a.aid AND b.season > a.season
                     WHERE a.archetype = 'kickoff specialist' AND b.pk >= 20)
        SELECT (SELECT count(*) FROM spec)     AS distinct_specialists,
               (SELECT count(*) FROM promoted) AS later_became_the_placekicker,
               round(100.0 * (SELECT count(*) FROM promoted)
                           / (SELECT count(*) FROM spec), 1) AS pct"""),

    "stability": ("Does the kickoff job change hands mid-season?", """
        WITH per_game AS (
          SELECT season, kicking_team_id AS team_id, game_id,
                 mode(kicker_athlete_id) AS game_kickoff_man
          FROM kickoff_tagged GROUP BY 1, 2, 3 HAVING count(*) >= 2)
        SELECT count(*) AS team_games,
               round(100.0 * avg((g.game_kickoff_man = e.ko1)::int), 1)
                 AS pct_led_by_the_season_kickoff_leader
        FROM per_game g JOIN eligible e USING (season, team_id)"""),

    "top_specialists": ("Longest-serving kickoff specialists", """
        SELECT any_value(known_name) AS player, count(*) AS seasons,
               sum(ko) AS kickoffs, sum(fg) AS fg, sum(pat) AS pat,
               string_agg(DISTINCT team, ', ') AS teams
        FROM player_season WHERE archetype = 'kickoff specialist'
        GROUP BY aid ORDER BY sum(ko) DESC LIMIT 15"""),

    "top_dual": ("Heaviest dual-duty placekickers", """
        SELECT any_value(known_name) AS player, count(*) AS seasons,
               sum(ko) AS kickoffs, sum(fg) AS fg, sum(pat) AS pat,
               string_agg(DISTINCT team, ', ') AS teams
        FROM player_season WHERE archetype = 'placekicker who kicks off'
        GROUP BY aid ORDER BY sum(ko) + sum(fg) + sum(pat) DESC LIMIT 15"""),
}


SUCCESS_PANELS: dict[str, tuple[str, str]] = {
    "success_coverage": ("Record coverage -- derived, so say how much is missing", """
        SELECT count(*) AS team_seasons,
               count(*) FILTER (WHERE missing = 0) AS complete_schedules,
               sum(missing) AS games_with_no_plays,
               round(avg(scheduled), 1) AS avg_scheduled,
               round(avg(wins), 2) AS avg_wins, round(avg(win_pct), 1) AS avg_win_pct
        FROM success"""),

    "success_by_model": ("Success by kicking model -- win RATE primary, totals run low", """
        SELECT model, count(*) AS team_seasons,
               round(avg(win_pct), 1)    AS win_pct,
               round(avg(avg_margin), 2) AS pt_margin,
               round(avg(wins), 2)       AS avg_wins,
               round(100.0 * avg((postseason_games > 0)::int), 1) AS pct_postseason,
               round(100.0 * sum(close_wins) / nullif(sum(close_games), 0), 1) AS close_win_pct
        FROM success GROUP BY 1 ORDER BY 3 DESC"""),

    "model_by_tier": ("The question as asked -- kicking model by success tier", """
        SELECT tier, count(*) AS team_seasons, round(avg(wins), 1) AS avg_wins,
          round(100.0 * avg((model = 'placekicker also kicks off')::int), 1) AS pct_shared_duties,
          round(100.0 * avg((model = 'kickoff specialist')::int), 1) AS pct_dedicated_placekicker,
          round(100.0 * avg((model = 'punter kicks off')::int), 1)   AS pct_punter,
          round(100.0 * avg(pk1_ko_share), 1) AS avg_pk_share_of_kickoffs
        FROM success GROUP BY 1 ORDER BY 1"""),

    "model_by_tier_complete": ("The same, on complete schedules only", """
        SELECT tier, count(*) AS team_seasons,
          round(100.0 * avg((model = 'placekicker also kicks off')::int), 1) AS pct_shared_duties,
          round(100.0 * avg((model = 'kickoff specialist')::int), 1) AS pct_dedicated_placekicker
        FROM success WHERE missing = 0 GROUP BY 1 ORDER BY 1"""),

    "switches": ("Teams switch after a bad year, and both directions then improve", """
        WITH sw AS (SELECT team_id, season, model, win_pct,
              lag(model)   OVER (PARTITION BY team_id ORDER BY season) AS prev_model,
              lag(win_pct) OVER (PARTITION BY team_id ORDER BY season) AS prev_win_pct,
              lag(season)  OVER (PARTITION BY team_id ORDER BY season) AS prev_season
            FROM success)
        SELECT prev_model || ' -> ' || model AS transition, count(*) AS n,
               round(avg(prev_win_pct), 1) AS win_pct_before,
               round(avg(win_pct), 1)      AS win_pct_after,
               round(avg(win_pct - prev_win_pct), 1) AS change
        FROM sw WHERE prev_season = season - 1
          AND model      IN ('placekicker also kicks off','kickoff specialist')
          AND prev_model IN ('placekicker also kicks off','kickoff specialist')
        GROUP BY 1 ORDER BY 1"""),

    "kicker_by_tier": ("The confound -- elite programs simply have kickers with more range", """
        SELECT s.tier, count(*) AS fg_att,
          round(100.0 * avg(p.fg_made::int), 1) AS fg_pct,
          round(100.0 * count(*) FILTER (WHERE p.fg_distance_yds >= 50) / count(*), 1)
            AS pct_att_from_50_plus,
          round(100.0 * avg(p.fg_made::int) FILTER (WHERE p.fg_distance_yds >= 50), 1)
            AS fg_pct_50_plus
        FROM play p JOIN success s ON s.season = p.season AND s.team_id = p.kicking_team_id
                                  AND p.kicker_athlete_id = s.pk1
        WHERE p.play_kind = 'field_goal' AND p.fg_made IS NOT NULL
        GROUP BY 1 ORDER BY 1"""),
}


def compare(con, league: str, seasons, floors) -> pd.DataFrame:
    """The headline panel run over every scope, so the P4 number has something to mean."""
    rows = []
    for name, lg in (("Power (P4 + Pac-12 to 2023)", "cfb"), ("Power 4 strict", "cfb"),
                     ("Group of 5", "cfb"), ("All FBS", "cfb"), ("NFL", "nfl")):
        scope = {"Power (P4 + Pac-12 to 2023)": "power", "Power 4 strict": "p4",
                 "Group of 5": "g5", "All FBS": "fbs", "NFL": "all"}[name]
        build(con, lg, scope, seasons, *floors)
        df = con.execute(PANELS["headline"][1]).df()
        rows.append(df.assign(scope=name))
    return pd.concat(rows).set_index("scope")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--league", default="cfb", choices=("cfb", "nfl"))
    ap.add_argument("--scope", default="power", choices=tuple(SCOPES))
    ap.add_argument("--from-season", type=int, default=2014)
    ap.add_argument("--to-season", type=int, default=2025,
                    help="2026 is in progress and is excluded by default")
    ap.add_argument("--min-ko", type=int, default=30)
    ap.add_argument("--min-pk", type=int, default=20)
    ap.add_argument("--min-punt", type=int, default=20)
    ap.add_argument("--csv-dir", help="write every panel here as <name>.csv")
    ap.add_argument("--no-compare", action="store_true")
    ap.add_argument("--success", action="store_true",
                    help="also relate each team-season to its derived win-loss record")
    args = ap.parse_args()

    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 200)
    pd.set_option("display.max_colwidth", 60)

    con = duckdb.connect(DB, read_only=True)
    seasons = (args.from_season, args.to_season)
    floors = (args.min_ko, args.min_pk, args.min_punt)

    built = con.execute("SELECT built_at FROM snapshot_meta").fetchone()[0]
    print(f"\n{'=' * 96}\n  KICKOFF DUTY AND THE PLACEKICKER"
          f"\n  {args.league} / scope={args.scope} / {seasons[0]}-{seasons[1]}"
          f" / floor: {args.min_ko} kickoffs, {args.min_pk} placekicks, {args.min_punt} punts"
          f"\n  snapshot built {built:%Y-%m-%d}\n{'=' * 96}")

    build(con, args.league, args.scope, seasons, *floors)
    out = {}
    for key, (title, sql) in PANELS.items():
        df = con.execute(sql).df()
        out[key] = df
        print(f"\n--- {title} ---")
        print(df.to_string(index=False) if len(df) else "  (no rows)")

    if not args.no_compare:
        df = compare(con, args.league, seasons, floors)
        out["scope_comparison"] = df.reset_index()
        print("\n--- The same headline, every scope, so the number has a reference ---")
        print(df.to_string())
        build(con, args.league, args.scope, seasons, *floors)   # restore the asked-for scope

    if args.success:
        build_success(con, args.league)
        for key, (title, sql) in SUCCESS_PANELS.items():
            df = con.execute(sql).df()
            out[key] = df
            print(f"\n--- {title} ---")
            print(df.to_string(index=False) if len(df) else "  (no rows)")

        tests = pd.DataFrame([two_sample(con, m) for m in
                              ("win_pct", "avg_margin", "wins")])
        out["success_test"] = tests
        print("\n--- Shared minus dedicated, with a 95% interval "
              "(an interval spanning zero IS the finding) ---")
        print(tests.to_string(index=False))

        fe = pd.DataFrame([within_program(con, m) for m in ("win_pct", "avg_margin")])
        out["success_within_program"] = fe
        print("\n--- The same difference, every program compared only against itself ---")
        print(fe.to_string(index=False))

    if args.csv_dir:
        d = args.csv_dir if os.path.isabs(args.csv_dir) else os.path.join(HOME, args.csv_dir)
        os.makedirs(d, exist_ok=True)
        for key, df in out.items():
            df.to_csv(os.path.join(d, f"{key}.csv"), index=False)
        con.execute("SELECT * FROM eligible ORDER BY season, team").df().to_csv(
            os.path.join(d, "team_season_detail.csv"), index=False)
        con.execute("SELECT * FROM player_season ORDER BY season, team, ko DESC").df().to_csv(
            os.path.join(d, "player_season_detail.csv"), index=False)
        print(f"\nwrote {len(out) + 2} csv files to {d}")


if __name__ == "__main__":
    main()
