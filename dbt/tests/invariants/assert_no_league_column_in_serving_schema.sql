-- A `league` column in the serving schema means a combined corpus leaked through, which is
-- the bug the per-league split exists to remove. scripts/build_snapshot.py refuses the same.
select table_name, column_name
from information_schema.columns
where table_schema = '{{ ref("play_wide").schema }}'
  and column_name ilike '%league%'
