select {{ dbt_utils.star(from=source('pbp', 'dim_team_season'), except=['league']) }}
from {{ source('pbp', 'dim_team_season') }}
where league = '{{ league() }}'
