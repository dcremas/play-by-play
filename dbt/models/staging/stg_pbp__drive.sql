select {{ dbt_utils.star(from=source('pbp', 'drive'), except=['league']) }}
from {{ source('pbp', 'drive') }}
where league = '{{ league() }}'
