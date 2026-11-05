{{ config(post_hook="alter table {{ this }} add primary key (drive_uid)") }}

select * from {{ ref('stg_pbp__drive') }}
