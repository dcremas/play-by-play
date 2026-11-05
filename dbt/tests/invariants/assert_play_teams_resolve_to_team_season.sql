-- Every team on a play must have a (team_id, season) row, or its conference comes out NULL in
-- the wide tables -- a plausible-looking gap rather than an error.
{% set checks = [
    ('special_teams_play', 'kicking_team_id'),
    ('special_teams_play', 'receiving_team_id'),
    ('scrimmage_play', 'offense_team_id'),
    ('scrimmage_play', 'defense_team_id'),
] %}
{% for fact, col in checks %}
select '{{ fact }}.{{ col }}' as team_column, p.play_uid, p.{{ col }} as team_id, p.season
from {{ ref(fact) }} p
where p.{{ col }} is not null
  and not exists (
      select 1 from {{ ref('dim_team_season') }} ts
      where ts.team_id = p.{{ col }} and ts.season = p.season
  )
{% if not loop.last %}union all{% endif %}
{% endfor %}
