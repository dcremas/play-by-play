-- The two facts are disjoint and share one play_uid space, across both leagues.
select play_uid, count(*) as n
from (
    select play_uid from {{ source('pbp', 'special_teams_play') }}
    union all
    select play_uid from {{ source('pbp', 'scrimmage_play') }}
) u
group by play_uid
having count(*) > 1
