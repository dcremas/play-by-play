select {{ dbt_utils.star(from=source('pbp', 'scrimmage_play'), except=['league']) }}
from {{ source('pbp', 'scrimmage_play') }}
where league = '{{ league() }}'
