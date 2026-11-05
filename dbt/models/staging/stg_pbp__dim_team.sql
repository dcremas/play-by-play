select {{ dbt_utils.star(from=source('pbp', 'dim_team'), except=['league']) }}
from {{ source('pbp', 'dim_team') }}
where league = '{{ league() }}'
