# pbp — ESPN play-by-play warehouse (FBS + NFL, 2014–present)

Start with `README.md` (Conventions section is binding) and `PLAN.md` (design record).

## Layout and data flow (one direction only)

ESPN → `scripts/` → CSVs in `data/out` → `sql/load_league.sql` → local Postgres `pbp` (schema `pbp`, PG 18,
system of record) → `scripts/build_snapshot.py` → `data/out/pbp_{cfb,nfl}.duckdb`.
The EC2 box (PG 16) holds a mirror split into `cfb.*` / `nfl.*` by `sql/split_leagues.sql`.

- `web/` — Dash explorer; reads the DuckDB snapshot only, never Postgres
- `reports/` — Excel/ERD builders; also snapshot-only
- `mcp_server/` — the `pbp-cfb` / `pbp-nfl` MCP servers (25 read-only tools over the **EC2 mirror**)
- `explorer/` — "Ask Football" Streamlit text-to-SQL app over both MCP servers
- `data/` — gitignored, never hand-edited. `data/espn/` is the only source of truth and is NOT backed up.

## Environment

Three venvs, all pyenv Python 3.14.7 (3.12/3.13 builds lack `blake2`): root `.venv`, `mcp_server/.venv`,
`explorer/.venv`, each with its own `requirements.txt`. No `pyproject.toml`.

- **Root `.venv/bin/pip` (and other entry-point scripts) are broken** — their shebangs point at the old
  `projects/cfb-special-teams` path. Use `.venv/bin/python -m pip ...`.
- `explorer/requirements.txt` pins `mcp<2` on purpose (langchain-mcp-adapters breaks on 2.x).

Run everything from the repo root — the SQL files resolve CSV paths relative to it.

## Commands

```bash
.venv/bin/python scripts/build_snapshot.py && .venv/bin/python -m web.app   # Dash explorer, :8060
bash scripts/refresh.sh [--check | --force nfl]                             # ETL refresh (locks in data/state/)
bash scripts/weekly_cfb.sh  /  bash scripts/weekly_nfl.sh
.venv/bin/python scripts/update_season.py <season> --league ... [--dry-run]
.venv/bin/python scripts/sync_ec2.py [--season 2026 | --league nfl | --dry-run]   # data → EC2 mirror
```

MCP server (from `mcp_server/`, needs the tunnel `ssh -f -N -T -L 15432:127.0.0.1:5432 awsvm`):
```bash
MCP_DB_SCHEMA=cfb ./.venv/bin/python -m pbp_mcp.server                       # stdio
MCP_DB_SCHEMA=nfl MCP_HTTP_PORT=8772 ./.venv/bin/python -m pbp_mcp.server --http
```
Config is `mcp_server/.env` (template `.env.example`); `MCP_DB_SCHEMA` is required, `cfb` or `nfl`.
`explorer/run.sh` needs both servers up on 8771/8772 and serves :8504.

## Verifying a change

There are no unit tests, CI, or ruff config. The checks that exist:
- `MCP_DB_SCHEMA=cfb ./.venv/bin/python -m pbp_mcp.selftest` (from `mcp_server/`) — run for any MCP change
- `scripts/verify_split.py`, `scripts/verify_mirror.py` — after split/sync work
- `psql -d pbp -f sql/verify.sql` / `sql/verify_phase4.sql` — print results, assert nothing; read the output
- `ruff check <files you touched>` — the repo is not ruff-format clean, so never bulk-format
- `explorer/eval/run_eval.py` costs API money with `--yes`; ask before running it

## Deploy (approved paths only)

- `bash mcp_server/deploy/push.sh [--tls]` — ships `mcp_server/` + `explorer/` code to `awsvm`, runs
  `provision.sh` under sudo. Code only; data goes via `sync_ec2.py`.
- `bash web/deploy/push.sh [--data]` — Dash app; `--data` also ships the ~331 MB snapshot.

## Data invariants (do not "fix" these)

- `fg_made IS NULL` = kick negated by penalty; `returned IS NULL` = outcome never stated. Never coalesce.
- 109 athlete names are shared by different people: group by `athlete_id`, label with `known_name`.
- Conference resolves on `(team_id, season)` — use `play_wide` / `scrimmage_wide` rather than hand joins.
- Team and conference ids collide across leagues; venue and athlete ids are shared.
- `SEASONS` is defined in four places that must agree (README Conventions #8).
- `play_text` is never cleaned. `pytz` must stay pinned even though nothing imports it.
- The old `sql/load_{1_stage,...}.sql` loaders are superseded and refuse to run — don't revive them.
- `run_sql` safety: sqlglot AST guard (refuses to run without sqlglot), read-only connections, 60 s timeout,
  2,000-row cap.
