-- `leagues` and `nfl_plays` are dropped: inside one league's schema they are a constant and
-- a duplicate of the play counts, and keeping them invites cross-league reasoning.
select
    nullif(athlete_id, '')::numeric::bigint        as athlete_id,
    nullif(known_name, '')                         as known_name,
    nullif(full_name, '')                          as full_name,
    nullif(position, '')                           as position,
    nullif(jersey, '')                             as jersey,
    nullif(text_name, '')                          as text_name,
    nullif(text_name_confidence, '')::numeric      as text_name_confidence,
    nullif(primary_role, '')                       as primary_role,
    nullif(primary_team_id, '')::numeric::integer  as primary_team_id,
    nullif(first_season, '')::numeric::smallint    as first_season,
    nullif(last_season, '')::numeric::smallint     as last_season,
    nullif(st_plays, '')::numeric::integer         as st_plays,
    nullif(scrimmage_plays, '')::numeric::integer  as scrimmage_plays,
    nullif(date_of_birth, '')::date                as date_of_birth,
    nullif(debut_year, '')::numeric::smallint      as debut_year,
    nullif(height_in, '')::numeric::smallint       as height_in,
    nullif(weight_lb, '')::numeric::smallint       as weight_lb
from {{ source('raw', 'dim_athlete_league') }}
where league = '{{ league() }}'
