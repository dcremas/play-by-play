-- ncaa_division (FBS/FCS) is college-only and nfl_division (AFC East) is NFL-only. Either
-- populated for the other league means the two meanings have started to merge.
select league, team_id, season, ncaa_division, nfl_division
from {{ source('pbp', 'dim_team_season') }}
where (league = 'nfl' and ncaa_division is not null)
   or (league = 'cfb' and nfl_division is not null)
