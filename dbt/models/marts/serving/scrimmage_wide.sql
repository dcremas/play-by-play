{{ config(post_hook=[
    "alter table {{ this }} add primary key (play_uid)",
    "create index on {{ this }} (season, play_kind)",
    "create index on {{ this }} (passer_athlete_id) where passer_athlete_id is not null",
    "create index on {{ this }} (rusher_athlete_id) where rusher_athlete_id is not null",
    "create index on {{ this }} (receiver_athlete_id) where receiver_athlete_id is not null",
    "create index on {{ this }} (tackler_athlete_id) where tackler_athlete_id is not null",
    "create index on {{ this }} (game_id)",
    "create index on {{ this }} (offense_team_id, season)",
    "create index on {{ this }} (defense_team_id, season)",
    "create index on {{ this }} (drive_id)",
]) }}

-- The scrimmage fact with every dimension pre-joined, same rules as play_wide.
select
    p.play_uid, p.source, p.game_id, p.season, p.week, p.season_type,
    p.play_kind, p.play_type_espn, p.drive_id, p.drive_number,
    p.period, p.clock_secs_period, p.wallclock_utc, p.down, p.distance, p.yards_to_goal,
    p.offense_team_id, p.defense_team_id, p.is_home_offense, p.score_diff_offense,
    p.yards_gained, p.end_down, p.end_distance, p.end_yards_to_goal, p.end_team_id,
    p.first_down_gained, p.is_complete, p.is_touchdown, p.is_turnover, p.is_penalty,
    p.is_scoring_play, p.points_scored,
    p.passer_athlete_id, p.rusher_athlete_id, p.receiver_athlete_id, p.tackler_athlete_id,
    pa.known_name as passer_name,   pa.position as passer_position,
    ra.known_name as rusher_name,   ra.position as rusher_position,
    wa.known_name as receiver_name, wa.position as receiver_position,
    ta.known_name as tackler_name,  ta.position as tackler_position,
    g.kickoff_utc, g.attendance, p.neutral_site, p.conference_game,
    p.venue_id, v.venue_name, v.city as venue_city, v.state as venue_state,
    v.country as venue_country, v.surface, v.indoor as venue_indoor,
    ot.display_name as offense_team, dt.display_name as defense_team,
    ots.conference_name as offense_conference, ots.ncaa_division as offense_ncaa_division,
    ots.nfl_division   as offense_nfl_division,
    dts.conference_name as defense_conference, dts.ncaa_division as defense_ncaa_division,
    dts.nfl_division   as defense_nfl_division,
    p.play_text,
    case when p.period is null or p.clock_secs_period is null then null
         when p.period > 4 then 0
         else (4 - p.period) * 900 + p.clock_secs_period end as game_secs_remaining,
    (p.period >= 4 and abs(p.score_diff_offense) <= 8
        and (p.period > 4 or p.clock_secs_period <= 300)) as is_clutch,
    case when p.down is null or p.distance is null then null
         when p.distance <= 3 then 'short'
         when p.distance <= 7 then 'medium'
         else 'long' end as distance_bucket,
    case when p.yards_to_goal is null then null
         when p.yards_to_goal <= 20 then 'red zone'
         when p.yards_to_goal <= 50 then 'opponent half'
         else 'own half' end as field_zone,
    extract(month from g.kickoff_utc)::smallint as game_month,
    {% if league() == 'nfl' -%}
    true
    {%- else -%}
    (ots.ncaa_division = 'FBS' and dts.ncaa_division = 'FBS')
    {%- endif %} as both_top_division
from {{ ref('scrimmage_play') }} p
left join {{ ref('fact_game') }}       g   on g.game_id = p.game_id
left join {{ ref('dim_venue') }}       v   on v.venue_id = p.venue_id
left join {{ ref('dim_team') }}        ot  on ot.team_id = p.offense_team_id
left join {{ ref('dim_team') }}        dt  on dt.team_id = p.defense_team_id
left join {{ ref('dim_team_season') }} ots on ots.team_id = p.offense_team_id and ots.season = p.season
left join {{ ref('dim_team_season') }} dts on dts.team_id = p.defense_team_id and dts.season = p.season
left join {{ ref('dim_athlete') }}     pa  on pa.athlete_id = p.passer_athlete_id
left join {{ ref('dim_athlete') }}     ra  on ra.athlete_id = p.rusher_athlete_id
left join {{ ref('dim_athlete') }}     wa  on wa.athlete_id = p.receiver_athlete_id
left join {{ ref('dim_athlete') }}     ta  on ta.athlete_id = p.tackler_athlete_id
