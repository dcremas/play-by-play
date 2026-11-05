{{ config(severity='warn') }}

-- A reasonableness check, not an integrity one: across the 20-54 yard range, field goal
-- accuracy should fall as distance grows. A bucket beating the one before it by more than
-- two points suggests a parser regression on distance or outcome.
with by_bucket as (
    select fg_dist_bucket,
           avg(case when fg_made then 1.0 else 0.0 end) as pct_made
    from {{ ref('play_wide') }}
    where play_kind = 'field_goal'
      and fg_made is not null
      and fg_dist_bucket between 20 and 50
    group by fg_dist_bucket
)
select fg_dist_bucket, pct_made, prev_pct
from (
    select fg_dist_bucket, pct_made,
           lag(pct_made) over (order by fg_dist_bucket) as prev_pct
    from by_bucket
) b
where pct_made > prev_pct + 0.02
