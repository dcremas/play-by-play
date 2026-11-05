{{ config(post_hook="alter table {{ this }} add primary key (season)") }}

-- Counts SPECIAL-TEAMS plays, as it always has, so the numbers stay comparable with the
-- snapshot's season_status and the site's live count.
select
    season,
    count(distinct game_id)                          as games,
    count(*)                                         as plays,
    max(week) filter (where season_type = 'regular') as last_regular_week,
    max(kickoff_utc)                                 as last_kickoff,
    max(kickoff_utc) > now() - interval '30 days'    as is_in_progress
from {{ ref('play_wide') }}
group by season
