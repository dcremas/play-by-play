{{ config(post_hook=[
    "alter table {{ this }} add primary key (play_uid)",
    "create index on {{ this }} (season, play_kind)",
    "create index on {{ this }} (kicker_athlete_id) where kicker_athlete_id is not null",
    "create index on {{ this }} (returner_athlete_id) where returner_athlete_id is not null",
    "create index on {{ this }} (game_id)",
    "create index on {{ this }} (kicking_team_id, season)",
    "create index on {{ this }} (fg_dist_bucket) where play_kind = 'field_goal'",
]) }}

-- The kicks fact with every dimension pre-joined. Conference resolves on (team_id, season),
-- which is the join most likely to be written wrong by hand; here it is written once.
select
    p.play_uid, p.source, p.game_id, p.season, p.week, p.season_type, p.play_kind,
    p.period, p.clock_secs_period, p.wallclock_utc, p.down, p.distance, p.yards_to_goal,
    p.kicking_team_id, p.receiving_team_id, p.is_home_kicking, p.score_diff_kicking,
    p.fg_distance_yds, p.fg_made, p.punt_gross_yds, p.punt_net_yds, p.kickoff_yds,
    p.return_yds, p.returned, p.touchback, p.onside, p.fair_catch, p.downed,
    p.out_of_bounds, p.kick_blocked, p.returned_for_td, p.converted, p.two_point_type,
    p.miss_reason, p.negated_by_penalty,
    p.kicker_athlete_id, p.returner_athlete_id, p.tackler_athlete_id,
    p.kicker_name, p.returner_name, p.blocker_name, p.snapper_name, p.holder_name,
    ka.known_name           as kicker_known_name,
    ka.text_name_confidence as kicker_name_confidence,
    ka.position             as kicker_position,
    g.kickoff_utc, g.attendance, p.neutral_site, p.conference_game,
    p.venue_id, v.venue_name, v.city as venue_city, v.state as venue_state,
    v.country as venue_country, v.surface, v.indoor as venue_indoor,
    kt.display_name as kicking_team, rt.display_name as receiving_team,
    kts.conference_name as kicking_conference, kts.ncaa_division as kicking_ncaa_division,
    kts.nfl_division   as kicking_nfl_division,
    rts.conference_name as receiving_conference, rts.ncaa_division as receiving_ncaa_division,
    rts.nfl_division   as receiving_nfl_division,
    p.play_text, p.parse_confidence,
    case when p.period is null or p.clock_secs_period is null then null
         when p.period > 4 then 0
         else (4 - p.period) * 900 + p.clock_secs_period end as game_secs_remaining,
    (p.period >= 4 and abs(p.score_diff_kicking) <= 8
        and (p.period > 4 or p.clock_secs_period <= 300)) as is_clutch,
    case when p.fg_distance_yds is null then null
         when p.fg_distance_yds < 20 then 15
         when p.fg_distance_yds >= 60 then 60
         else (p.fg_distance_yds / 5) * 5 end as fg_dist_bucket,
    extract(month from g.kickoff_utc)::smallint as game_month,
    -- The NFL has no second division, so this is constant true there.
    {% if league() == 'nfl' -%}
    true
    {%- else -%}
    (kts.ncaa_division = 'FBS' and rts.ncaa_division = 'FBS')
    {%- endif %} as both_top_division
from {{ ref('special_teams_play') }} p
left join {{ ref('fact_game') }}       g   on g.game_id = p.game_id
left join {{ ref('dim_venue') }}       v   on v.venue_id = p.venue_id
left join {{ ref('dim_team') }}        kt  on kt.team_id = p.kicking_team_id
left join {{ ref('dim_team') }}        rt  on rt.team_id = p.receiving_team_id
left join {{ ref('dim_team_season') }} kts on kts.team_id = p.kicking_team_id   and kts.season = p.season
left join {{ ref('dim_team_season') }} rts on rts.team_id = p.receiving_team_id and rts.season = p.season
left join {{ ref('dim_athlete') }}     ka  on ka.athlete_id = p.kicker_athlete_id
