# Play-by-Play Warehouse MCP Server

**Read-only conversational access to 2.4M plays of college football and NFL
play-by-play.** Twenty-five tools — twenty-two typed, plus schema introspection
and a guarded SQL passthrough — over a `SELECT`-only Postgres role on the EC2
box.

| | |
|---|---|
| **Corpus** | 2,397,512 plays · 13,958 games · 13 seasons (2014–2026) · two leagues |
| **Database** | `pbp` on the EC2 instance, PostgreSQL 16.15, schema `pbp`, 14 tables |
| **Role** | `mcp_ro` — the same role the weather warehouse MCP uses, granted per database |
| **Route from the Mac** | SSH tunnel on `127.0.0.1:15432`; public 5432 is closed |
| **Sibling** | `../../ec2-nginx/weather-sql-explorer/mcp_server` — this server is built on its pattern, deliberately |

The two wide tables are the intended read path:

| Table | Rows | Contents |
|---|---|---|
| `pbp.play_wide` | 412,761 | kicks fact — kickoffs, punts, field goals, conversions, **dimensions pre-joined** |
| `pbp.scrimmage_wide` | 1,984,751 | scrimmage fact — rushes, passes, sacks, penalties, **dimensions pre-joined** |
| `pbp.drive` | 336,818 | one row per drive; spans both facts |
| `pbp.dim_athlete` | 68,389 | career grain, shared across both leagues |
| + 10 more | | the normalised facts, bridges and dimensions |

Counts are as of the 2026-09-25 sync and grow weekly while a season is in
progress — `data_coverage` is the live answer, this table is orientation.

---

## 1. Why this exists

The warehouse lives in local Postgres on the Mac and is read by two local apps
through a DuckDB snapshot. Neither is reachable from Claude Desktop, claude.ai or
a phone. This server puts the same corpus behind an MCP interface against the EC2
mirror, so the data is available anywhere.

In Claude Code on this machine it is *partly* redundant — the assistant already
has a shell, `psql` and the tunnel. What it still buys, even there, is that **the
domain's traps are encoded in the tools** rather than having to be remembered:

- Conference membership is grained `(team, season)` and 83 of 275 teams changed
  conference inside the window. Every tool reads the wide tables, where that join
  is already resolved.
- `fg_made IS NULL` is a kick negated by penalty, not a miss. `returned IS NULL`
  is an outcome the feed never stated — 9.9% of punts and kickoffs. No tool
  coalesces either away, and the rate tools return the unstated share beside the
  rate.
- 109 athlete names are shared by more than one person. Everything player-grain
  groups by `athlete_id`.
- Team and conference ids **collide** between leagues; venue and athlete ids are
  **deliberately shared**. Getting either backwards is silent.

---

## 2. Prerequisites — the SSH tunnel

Public 5432 is closed to the internet, so from the Mac the tunnel is the **only**
route. Nothing here works without it:

```bash
ssh -f -N -T -L 15432:127.0.0.1:5432 awsvm
```

Check it:

```bash
nc -z 127.0.0.1 15432 && echo "tunnel up" || echo "tunnel DOWN"
```

The server opens connections lazily, so it starts fine with the tunnel down —
only the first query fails, and it fails with a message naming the tunnel rather
than a bare "connection refused".

> The `awsvm` host alias is in `~/.ssh/config`. The weather MCP's README spells
> the same command out longhand with the `.pem` path.

---

## 3. Setup

```bash
cd ~/projects/pbp/mcp_server

# 3a. Dependencies
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# 3b. The read-only grants (idempotent). Runs on the box: it needs to own the
#     objects, and mcp_ro's session defaults are set per-database.
ssh awsvm 'sudo -u postgres psql -d pbp -f /var/lib/pgsql/staging/mcp_setup/setup_role_pbp.sql'

# 3c. The caveats, as COMMENTs. Also on the box — COMMENT requires ownership.
ssh awsvm 'sudo -u postgres psql -d pbp -f /var/lib/pgsql/staging/mcp_setup/comment_tables.sql'

# 3d. The credential. mcp_ro ALREADY HAS a password — the weather MCP uses it.
#     Reuse it rather than rotating, or you break that server too.
cp .env.example .env && chmod 600 .env
grep MCP_DB_PASSWORD ../../ec2-nginx/weather-sql-explorer/mcp_server/.env   # copy this in

# 3e. Verify
./.venv/bin/python -m pbp_mcp.selftest
```

`scripts/sync_ec2.py` ships `setup_role_pbp.sql` and `comment_tables.sql` to
`/var/lib/pgsql/staging/mcp_setup/` and re-runs both on every sync, so steps 3b
and 3c are only manual on a first install.

### One credential, two servers

`mcp_ro` reads `weatherdata`, `apple_weatherkit` **and** `pbp`. That is the
convention already established on this box (`setup_role.sql` and
`setup_role_apple_weatherkit.sql` grant the same role per database), and keeping
it means one password and a straightforward path to the cross-warehouse join that
`README.md` "Not built" describes — plays against weather, on the same server.

**Rotating the password requires updating both `.env` files**, and
`pg_user_mapping` for the weather FDW as well. Change only one and the other
server starts failing authentication while this one keeps working, which is a
confusing way to find out.

---

## 4. Connecting a client

### Claude Code

```bash
claude mcp add pbp-warehouse \
  --scope user \
  -e PYTHONPATH=/Users/dustincremascoli/projects/pbp/mcp_server \
  -- /Users/dustincremascoli/projects/pbp/mcp_server/.venv/bin/python \
     -m pbp_mcp.server
```

**`PYTHONPATH` is not optional.** The server is launched from whatever directory
the client happens to be in, and `python -m pbp_mcp.server` cannot find the
package from outside `mcp_server/`. Without it the server exits immediately and
the client reports `CONNECTION_CLOSED`, which does not point at the cause.

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pbp-warehouse": {
      "command": "/Users/dustincremascoli/projects/pbp/mcp_server/.venv/bin/python",
      "args": ["-m", "pbp_mcp.server"],
      "env": {
        "PYTHONPATH": "/Users/dustincremascoli/projects/pbp/mcp_server"
      }
    }
  }
}
```

Restart Desktop — it reads that file only at launch. **Use the absolute path to
`.venv/bin/python`**; Desktop does not inherit your shell `PATH`, so a bare
`python` resolves to nothing and the server appears to fail silently.

### On the EC2 box — NOT DONE

Both modes above run the server **on the Mac**, reaching the box through the
tunnel. The database is on EC2; the server is not. Running it there, the way
`weather-mcp.service` runs, is unbuilt and would need:

1. A service user (`pbpmcp`), separate from `weathermcp`, so neither can read the
   other's credential.
2. The code and a venv at `/opt/pbp-mcp` — the box has Python but not this package.
3. `/etc/pbp-mcp/mcp.env`, mode `640 root:pbpmcp`, holding `MCP_DB_PASSWORD`.
   On-box the connection is local, so `MCP_DB_HOST=127.0.0.1` and `MCP_DB_PORT=5432`
   — no tunnel.
4. A systemd unit running `python -m pbp_mcp.server --http`, binding
   **`127.0.0.1:8771` only** (8770 is weather, 8000 is in use). It must not be
   proxied by nginx: there is no authentication on that endpoint.
5. A sync story for the code, which `scripts/sync_ec2.py` does not currently handle
   — it ships SQL, not the server.

Until then, the answer to "is it usable on the box?" is no; the answer to "is it
usable against the box?" is yes.

---

## 5. The tools

**Start here**

| Tool | Purpose |
|---|---|
| `data_coverage` | Seasons, games, and which season is still being played. Call first for anything recent. |
| `known_limits` | The caveats that change an answer. Call before quoting a number. |
| `list_schema` / `describe_table` | Tables and columns, with the NULL semantics as stored `COMMENT`s. |
| `run_sql` | One read-only SELECT. `explain_only=true` to cost it without returning rows. |

**Discovery — turn a name into an id**

| Tool | Purpose |
|---|---|
| `find_team` | Name → `(league, team_id)` plus that season's conference. Ids collide between leagues. |
| `find_player` | Name → `athlete_id`. `leagues` shows a cross-league career. |
| `find_venue` | Name or city → `venue_id`. Venue ids are shared across leagues. |
| `list_conferences` | Conferences and member counts, per season. |

**Games**

| Tool | Purpose |
|---|---|
| `list_games` | Filter by league, season, week, type, team. |
| `game_summary` | Header, play counts, and a **derived** final score. |
| `drive_chart` | Every drive in one game, in order. |

**Plays**

| Tool | Purpose |
|---|---|
| `query_plays` | The workhorse. Either fact, ~20 filters, raw `play_text` on every row. |
| `play_detail` | One play in full, with every athlete ESPN reported on it. |

**People and teams**

| Tool | Purpose |
|---|---|
| `player_profile` | Identity plus career production, split by league and season. |
| `player_game_log` | Per-game scrimmage production. |
| `leaderboard` | `fg_pct`, `punt_gross`, `rush_yards`, `pass_yards`, `receiving_yards`, `tackles` — always with `min_attempts`. |
| `team_season` | One team's offence, defence and special teams for a season. |

**Analysis**

| Tool | Purpose |
|---|---|
| `fg_by_distance` | FG% by 5-yard band. Monotonic in both leagues. |
| `kick_outcomes` | Touchback / return / fair catch rates, on the right denominator, with `pct_unstated`. |
| `drive_outcomes` | Drive result by starting field position. |
| `situational_splits` | Efficiency by down, distance bucket and field zone. |
| `league_trend` | One measure by season — eight of them, including the two that show rule changes. |

**Data quality**

| Tool | Purpose |
|---|---|
| `parse_quality` | Parser agreement and athlete-id coverage on the kicks fact. |
| `audit_plays` | Kicks the parser could not read exactly, with their raw text. |

### `min_attempts` on `leaderboard`

It defaults to 20 and exists because an unqualified rate leaderboard is the most
reliable way to get a confidently wrong answer here — a 100% field-goal kicker
with two attempts leads nothing.

### `top_division_only`

The college corpus deliberately includes FBS-vs-FCS games. Set this for any
player- or kicker-quality question. It is **constant true for the NFL**, so it is
safe on a cross-league query — and it had to be made explicitly true, because left
as the college expression it evaluated NULL and would have silently dropped the
entire NFL corpus.

---

## 6. Security model

Read-only is enforced in **four independent layers**, and the SQL guard is not the
important one:

1. **The role.** `mcp_ro` holds `SELECT` on fourteen tables and nothing else — no
   superuser, no createdb, no createrole, `NOINHERIT`. In this database it carries
   `default_transaction_read_only=on`, `statement_timeout=60s` and
   `idle_in_transaction_session_timeout=60s` as role-level defaults, so they apply
   even if a client forgets to ask. **This is the layer that matters.**
2. **The connection.** Every transaction sets `read_only=True`.
3. **The statement timeout**, set again per connection in `db.py`, so a server
   pointed at a laxer role is still bounded.
4. **The parser** (`guard.py`). Model-generated SQL is parsed with `sqlglot` and
   rejected unless it is a *single* read-only statement over an *allow-listed*
   table, with a `LIMIT` forced onto it. Decisions are made on the AST, never on
   keyword matching — comment injection and case tricks defeat blocklists, so none
   is used. It also refuses `pg_read_file`, `dblink`, `pg_sleep` and friends, the
   system catalogs, `information_schema`, and locking clauses.

What makes a passthrough acceptable is not persuading the model to behave, it is
that **the database cannot do the thing you are afraid of**. A `DELETE` arriving
from a poisoned prompt is not refused because the parser was clever; it is refused
because the role has no `DELETE` privilege. The guard turns safe-but-confusing
failures into clear ones. `selftest.py` proves this directly: three checks
deliberately **bypass the guard** and assert the database refuses the write on its
own.

**Why `information_schema` is blocked but `describe_table` works.** The catalogs
are read by *fixed* queries in `queries.py`, filtered through
`has_table_privilege`, so a client sees structure for the tables it may read and
nothing else. `run_sql` cannot reach the catalogs at all.

**On that point the layering above is not symmetric, and it is worth being exact.**
Audited 2026-09-25 by bypassing the guard and querying directly as `mcp_ro`:

- **Writes are refused by the role**, every one -- `CREATE TABLE`, `CREATE SCHEMA`,
  `CREATE EXTENSION` all fail on `ReadOnlySqlTransaction`, and `pg_read_file` and
  `pg_authid` on `InsufficientPrivilege`. Layer 1 genuinely is the one that matters
  here.
- **Catalog reads are not.** `pg_stat_activity`, `pg_settings` and
  `information_schema` are readable by PUBLIC in Postgres and therefore by
  `mcp_ro`. For those, **the guard is the only thing standing in the way, not the
  role.** The exposure if it were bypassed is small -- Postgres redacts other
  sessions' query text to `<insufficient privilege>` for a non-superuser, and the
  superuser-only GUCs stay hidden (`ssl_key_file`'s *path* is visible; the key is
  not) -- but "the database cannot do the thing you are afraid of" is true of
  writes and only of writes.

**Cross-database reach of the shared credential**, same audit: `mcp_ro` may
`CONNECT` to six databases (the PUBLIC default) but can read tables in only two --
`pbp` (14) and `weatherdata` (5), plus `apple_weatherkit`. `recipes`,
`data_visualization_logging` and `postgres` expose **0 readable tables**. So the
shared role grants exactly what was intended and nothing more.

Confirm the grants are still what they should be:

```bash
psql -h 127.0.0.1 -p 15432 -U dustincremascoli -d pbp -tA -c "
select table_name, string_agg(privilege_type,',') from information_schema.table_privileges
where grantee='mcp_ro' and table_schema='pbp' group by 1 order by 1;"
```

**The grants do not stay granted by themselves.** `sql/wide_tables.sql` does
`DROP TABLE` + `CREATE TABLE AS` on `play_wide`, `scrimmage_wide` and
`season_status`, and privileges and comments go with a dropped table.
`scripts/sync_ec2.py` therefore re-runs `setup_role_pbp.sql` and
`comment_tables.sql` after it, every time. If someone reorders those steps, the
next sync silently revokes access to the three tables the server depends on most —
and the selftest's "guard allow-list matches the grants" check is what catches it.

**Network exposure: none added.** The server runs on the Mac and reaches Postgres
through the existing tunnel; nothing is opened on the EC2 side.

**It is NOT deployed on the EC2 box**, unlike the weather warehouse, which runs
there as `weather-mcp.service` on `127.0.0.1:8770`. `main_http()` is provided and
smoke-tested -- it binds `127.0.0.1:8771`, serves `/mcp` and answers `initialize` --
but there is no systemd unit, no service user and no `/etc/pbp-mcp/mcp.env`. What
on-box deployment would need is in section 4.

**The credential** lives only in `.env` (mode 600, gitignored). It is not in any
client config, not in the repo, and not on any command line.

---

## 7. Troubleshooting

| Symptom | Cause |
|---|---|
| "Could not reach Postgres … tunnel is almost certainly down" | Restart the tunnel (§2). The message includes the command. |
| "Database authentication failed for role mcp_ro" | `MCP_DB_PASSWORD` does not match the role. Remember it is shared with the weather MCP — check whether that one still works. |
| Tools missing in Claude Desktop | `command` is not the absolute venv python path, or Desktop was not restarted. |
| `run_sql` says a table is "not readable" that `list_schema` lists | `guard.ALLOWED_TABLES` and the grants have drifted. They are two separate lists on purpose; the selftest compares them. |
| A query times out at 60s | Usually a join across both facts before aggregating. Aggregate each side in a CTE, then join the CTEs — 2M scrimmage rows against 400k kick rows is a large fan-out on a 2-vCPU box. |
| Counts differ from README.md | The mirror is only as fresh as the last `scripts/sync_ec2.py`. Check `data_coverage` against the local warehouse. |
| `ModuleNotFoundError: mcp.server.mcpserver` | You are on MCP SDK v1, where the class was `FastMCP` in `mcp.server.fastmcp`. This server uses v2. |

Reproduce any tool outside MCP for debugging:

```bash
./.venv/bin/python -c "
from pbp_mcp import server
import json; print(json.dumps(server.fg_by_distance(league='nfl'), indent=2))"
```

---

## 8. Layout

```
mcp_server/
  README.md                     this file
  requirements.txt
  setup_role_pbp.sql            mcp_ro grants + session defaults  (run on the box)
  comment_tables.sql            the NULL semantics, as COMMENTs   (needs ownership)
  .env.example                  copy to .env, chmod 600
  pbp_mcp/
    server.py      the 25 tools; MCPServer wiring
    queries.py     every fixed SQL statement + the identifier allow-lists
    guard.py       the gate model-generated SQL passes before the planner
    db.py          pooled read-only connections; `query` (fixed SQL) vs
                   `query_guarded` (validated dynamic SQL) are separate on
                   purpose, so dynamic SQL has exactly one entry point
    selftest.py    live checks -- run after any change
```

Elsewhere in the repo, load-bearing for this server:

```
sql/wide_tables.sql      builds play_wide / scrimmage_wide / season_status.
                         DROPS them first, so it revokes grants and comments --
                         which is why sync_ec2.py re-applies both afterwards.
scripts/sync_ec2.py      pushes local Postgres -> the EC2 mirror and rebuilds
                         the serving layer. The only supported way to refresh.
```

---

## 9. What the selftest proves

It is not a smoke test. Beyond checking that every tool returns plausible shapes,
it asserts **the football**, because those are the cheapest available proof that
the load landed correctly:

- College FG% 74–80, NFL 80–90. **NFL yards-per-carry *below* college** — the
  correct direction, and the opposite of what a copied pipeline produces.
- FG% falls monotonically from 20 to 50 yards **in both leagues**. A silently
  wrong distance column does not produce that curve.
- College kickoff touchback% **steps up at 2018** (the fair-catch rule); NFL
  touchback% **collapses by 2025** (the dynamic kickoff, then the touchback spot
  moving to the 35). Two different shapes, in a column parsed by two different
  modules.
- Drive TD% falls monotonically with starting field position, in both leagues; a
  reversed field-position column produces it backwards.
- Unstated kick outcomes are **NULL, not false** — the defect fixed 2026-08-31 —
  and the unstated share is ~9.9%.
- Sacks carry `is_complete IS NULL`, matching NCAA accounting.
- Five referential-integrity checks return 0, the Pro Bowl is absent, and both
  wide tables match the row counts of the facts they project.
- Twenty-four guard rejections, five acceptances, three limit behaviours, and
  three write-protection checks that bypass the guard entirely.

If it does not pass, fix that before wiring up a client.
