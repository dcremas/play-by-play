select {{ dbt_utils.star(from=source('pbp', 'special_teams_play'), except=['league']) }}
from {{ source('pbp', 'special_teams_play') }}
where league = '{{ league() }}'
