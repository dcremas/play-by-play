{#- Rows in the dbt-built model and NOT in the same table of var('parity_schema'), and the
    reverse. Columns are selected by name from both sides, so a column missing from either
    fails the query rather than shifting silently. EXCEPT ALL, so duplicate rows count. -#}
{% macro parity_check(model_name) %}
{%- set built = ref(model_name) -%}
{%- set legacy = var('parity_schema') ~ '.' ~ model_name -%}
{%- set cols = [] -%}
{%- if execute -%}
    {%- for c in adapter.get_columns_in_relation(built) -%}
        {%- do cols.append(adapter.quote(c.name)) -%}
    {%- endfor -%}
{%- endif -%}
{%- set col_list = cols | join(', ') if cols else '*' -%}
with only_built as (
    select {{ col_list }} from {{ built }}
    except all
    select {{ col_list }} from {{ legacy }}
),
only_legacy as (
    select {{ col_list }} from {{ legacy }}
    except all
    select {{ col_list }} from {{ built }}
)
select 'only in dbt' as side, * from only_built
union all
select 'only in {{ var("parity_schema") }}' as side, * from only_legacy
{% endmacro %}
