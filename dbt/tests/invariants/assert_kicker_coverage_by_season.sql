-- Every season's kicks must mostly carry a kicker id. Real coverage is 97.5-100% in every
-- season of both leagues (measured 2026-11-05); a season under 95% means the athlete links
-- were built from incomplete inputs. That is exactly what happened on the box's first run,
-- when the NFL's raw files were missing and every pre-2026 NFL kick lost its kicker -- and
-- the check that caught it ran after the swap. This one runs before it.
select season,
       count(*)                                         as kicks,
       count(kicker_athlete_id)                         as with_kicker,
       round(100.0 * count(kicker_athlete_id) / count(*), 2) as pct
from {{ ref('play_wide') }}
where play_kind in ('field_goal', 'punt', 'kickoff')
group by season
having count(kicker_athlete_id) < 0.95 * count(*)
