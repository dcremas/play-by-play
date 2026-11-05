{{ config(post_hook="alter table {{ this }} add primary key (conference_id)") }}

select * from {{ ref('stg_pbp__dim_conference') }}
