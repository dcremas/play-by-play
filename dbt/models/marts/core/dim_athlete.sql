{{ config(post_hook="alter table {{ this }} add primary key (athlete_id)") }}

select * from {{ ref('stg_raw__dim_athlete') }}
