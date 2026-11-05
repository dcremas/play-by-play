select {{ dbt_utils.star(from=source('pbp', 'fact_game'), except=['league']) }}
from {{ source('pbp', 'fact_game') }}
where league = '{{ league() }}'
