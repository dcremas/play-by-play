-- ESPN files the AFC/NFC all-star squads (team ids 31, 32) as postseason games. They are not
-- real rosters; fetch_espn.py drops them at fetch time.
select play_uid, offense_team_id, defense_team_id
from {{ source('pbp', 'scrimmage_play') }}
where league = 'nfl' and (offense_team_id in (31, 32) or defense_team_id in (31, 32))
