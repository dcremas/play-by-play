# dbt: the per-league serving layer

This dbt project builds what `sql/split_leagues.sql` builds: one schema per league, with no
`league` column anywhere, holding the dimensions, the two facts, the athlete bridges and the
wide tables the apps read. It reads the system of record (`pbp.*`) and never writes it.

```
ESPN → scripts/ → CSVs → sql/load_league.sql ────────→ pbp.*  ─┐
                       → sql/load_raw_athletes.sql ──→ raw.*  ─┴→ dbt → <league>_dbt.*
       (extract and load: Python + psql)                         (transform: this project)
```

**Status: serving.** On the EC2 box, `update_season.py --transform dbt` builds `cfb_next` /
`nfl_next` with this project every morning and swaps them into place (`deploy/README.md`). Before
cutover every table matched what `split_leagues.sql` built, row for row (`tests/parity/`). A plain
`dbt build` on the laptop lands in `cfb_dbt` / `nfl_dbt` and touches nothing live.

## Run it

```bash
# once: Python 3.13, because dbt's dependencies do not import on 3.14 yet
~/.pyenv/versions/3.13.9/bin/python -m venv dbt/.venv
dbt/.venv/bin/pip install -r dbt/requirements.txt

cd dbt && export DBT_PROFILES_DIR=.
.venv/bin/dbt deps
psql -d pbp -v root=$PWD/.. -f ../sql/load_raw_athletes.sql   # after build_dims.py athlete --leagues cfb / nfl

.venv/bin/dbt build --vars '{league: cfb}'      # ~55 s: 14 tables, 11 views, ~75 tests
.venv/bin/dbt build --vars '{league: nfl}'      # ~25 s
.venv/bin/dbt source freshness --vars '{league: cfb}'
```

Every run names its league; the project refuses to compile without one. The connection comes
from `profiles.yml` (local socket, database `pbp`), overridable with `PBP_PG*` environment variables.

## Layout

| layer | materialized | schema | what it does |
|---|---|---|---|
| `models/staging/` | view | `<league>_dbt_staging` | one view per source table: filter to the league, drop `league`, cast `raw` text |
| `models/marts/core/` | table | `<league>_dbt` | the dimensions, both facts and both bridges, with primary keys |
| `models/marts/serving/` | table | `<league>_dbt` | `play_wide`, `scrimmage_wide`, `season_status`, with the same indexes as today |

`models/exposures.yml` names what reads the serving schema: the Play Explorer and Ask Football.

## Tests

| where | what it holds |
|---|---|
| `models/staging/_sources.yml` | keys of the system of record, scoped by league where ids collide; source freshness on `loaded_at` |
| `models/marts/*/_*.yml` | primary keys, accepted values, relationships from the facts to their games and teams, and row-count equality between each wide table and its fact (a join that fans out or drops rows fails) |
| `tests/invariants/` | the "must be 0" checks from `sql/verify.sql`, now asserted instead of printed, plus: no `league` column in the serving schema, every play's team resolves to a (team, season) row, every season's kicks carry a kicker id at 95%+ (blocks a build from incomplete raw files), and field goal accuracy falls with distance (warn only) |
| `tests/parity/` | off by default. Diffs every mart against an existing schema with `EXCEPT ALL` in both directions |

```bash
.venv/bin/dbt test --vars '{league: cfb, parity_schema: cfb}' --select tag:parity   # ~75 s
```

**One known exception.** Four college two-point conversions have no kicking team (standalone
conversion rows where ESPN's `start.team.id` is NULL). `README.md` says that count is zero; these
tests found the four. `not_null` on `special_teams_play.kicking_team_id` warns at 4 and fails at 5.

## How production runs it

`scripts/update_season.py --transform dbt`, per league:

```bash
dbt build --vars '{league: cfb, serving_schema: cfb_next}'   # tests run before the swap
psql -f mcp_server/comment_tables.sql -v schema=cfb_next
psql -f sql/swap_schema.sql -v live=cfb -v stage=cfb_next
```

dbt grants `SELECT` to `pbp_ro` on every mart and `USAGE` on the schema, so the stage arrives readable.
A test at `error` severity fails the build, and the live schema keeps serving the previous day.
`sql/split_leagues.sql` is still the default for `update_season.py` and can be retired once the box
has run on dbt for a full week.
