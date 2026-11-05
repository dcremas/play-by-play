{#- Marts land in `<league>_dbt` and staging views in `<league>_dbt_staging`, beside the live
    `cfb` / `nfl` schemas rather than on top of them. Pass --vars '{serving_schema: cfb_next}'
    to build the stage that sql/swap_schema.sql moves into place. -#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- set base = var('serving_schema', league() ~ '_dbt') -%}
    {%- if custom_schema_name is none -%}
        {{ base }}
    {%- else -%}
        {{ base }}_{{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
