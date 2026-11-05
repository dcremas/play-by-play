{{ config(post_hook=[
    "create index on {{ this }} (athlete_id)",
    "create index on {{ this }} (play_uid)",
]) }}

-- The bridge has no league of its own; joining the fact scopes it, and also keeps out the
-- orphans whose play_uid is no longer in the fact (README.md Known limits section 14).
select b.*
from {{ ref('stg_pbp__scrimmage_athlete') }} b
join {{ ref('scrimmage_play') }} p on p.play_uid = b.play_uid
