{{ config(post_hook="alter table {{ this }} add primary key (team_id, season)") }}

select * from {{ ref('stg_pbp__dim_team_season') }}
