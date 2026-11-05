select {{ dbt_utils.star(from=source('pbp', 'dim_conference'), except=['league']) }}
from {{ source('pbp', 'dim_conference') }}
where league = '{{ league() }}'
