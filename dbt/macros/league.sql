{#- The corpus this run builds. Refuses to compile without one: a run that silently defaulted
    to a league is how a college query ends up reading NFL team ids. -#}
{% macro league() -%}
    {%- set lg = var('league') -%}
    {%- if lg not in ('cfb', 'nfl') -%}
        {{ exceptions.raise_compiler_error("Name the corpus: dbt build --vars '{league: cfb}' (or nfl). Got: " ~ lg) }}
    {%- endif -%}
    {{- lg -}}
{%- endmacro %}
