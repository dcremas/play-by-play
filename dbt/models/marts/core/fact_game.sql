{{ config(post_hook="alter table {{ this }} add primary key (game_id)") }}

select * from {{ ref('stg_pbp__fact_game') }}
