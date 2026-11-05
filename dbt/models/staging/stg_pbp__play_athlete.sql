-- No league column: scoped in the mart, through the facts.
select * from {{ source('pbp', 'play_athlete') }}
