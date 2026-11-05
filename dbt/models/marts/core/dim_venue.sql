{{ config(post_hook="alter table {{ this }} add primary key (venue_id)") }}

-- Venue ids are shared across leagues; keep the venues this league actually played in, so a
-- college venue search does not return an NFL-only stadium.
select v.*
from {{ ref('stg_pbp__dim_venue') }} v
where exists (
    select 1 from {{ ref('stg_pbp__fact_game') }} g
    where g.venue_id = v.venue_id
)
