-- College and NFL game id ranges interleave, so non-collision is a fact about ESPN's
-- allocation, not a structural guarantee. Test it; do not trust it.
select game_id
from {{ source('pbp', 'fact_game') }}
group by game_id
having count(distinct league) > 1
