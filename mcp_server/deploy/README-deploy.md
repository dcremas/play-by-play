# Deploying the pbp MCP server on the EC2 box

The database has been on the box since 2026-09-25. **The server now is too.**
Before that it ran on the Mac against the box through the SSH tunnel, which meant
nothing else on that host could reach it — including a web app living there.
That was the blocker for building a text-to-SQL explorer, and this directory is
what removed it.

```bash
bash deploy/push.sh              # from the Mac. Idempotent. Deploys BOTH instances.
bash deploy/push.sh --tls        # …and issue the certificate, once DNS resolves
```

**Two instances, one template.** `pbp-mcp@cfb` on `127.0.0.1:8771` and
`pbp-mcp@nfl` on `8772`, from a single `pbp-mcp@.service`. The instance name IS
the schema name (`MCP_DB_SCHEMA=%i`), so `pbp-mcp@cfb` cannot serve the NFL
corpus, and `config.py` refuses to start on any name that is not a real corpus.

---

## What ends up where

| Path | Owner | What |
|---|---|---|
| `/opt/pbp-mcp` | `pbpmcp` | the code and its venv. `ReadOnlyPaths` in the unit; **no `.env`** |
| `/etc/pbp-mcp/mcp.env` | `root:pbpmcp` `640` | `MCP_DB_PASSWORD`, and nothing else. Shared by both instances: one role reads both schemas |
| `/etc/systemd/system/pbp-mcp@.service` | root | the template both instances run from |
| `/etc/pbp-mcp/port-{cfb,nfl}.env` | `root:pbpmcp` `640` | `MCP_HTTP_PORT` per instance — systemd cannot derive a port from `%i` |
| `/etc/nginx/conf.d/pbp.conf` | root | the vhost for the *future* app on 8504 |
| `/etc/nginx/proxy_params_pbp.inc` | root | WebSocket-aware proxy settings |
| `/usr/share/nginx/html/maintenance.html` | root | the 502 page, shared with `sqlx.conf` |

---

## The order, and why it is that order

1. **Build, grant, comment, swap — per league, in that order.**
   `split_leagues.sql` builds `<league>_next`; `setup_role_pbp.sql -v schemas=<league>_next`
   grants it; `comment_tables.sql -v schema=<league>_next` documents it;
   `swap_schema.sql` renames it into place in one transaction.
   **The order is not negotiable.** Grants and comments live on table oids and survive a
   rename, so a staging schema armed first arrives live already readable. Grant after the
   swap instead and the servers briefly cannot read a schema that exists.
   `scripts/sync_ec2.py` drives exactly this.

   Why staging at all: the rebuild used to `DROP SCHEMA cfb CASCADE` and recreate in
   place, which is **1m39s measured on this box** during which `pbp-mcp@cfb` has nothing
   to read — and worse than that, because `DROP SCHEMA` takes `ACCESS EXCLUSIVE` and every
   new query queues behind it. The rename is **0.76 ms** on the 2.5 GB schema. Verified
   under load: 231 consecutive live queries through a full rebuild-and-swap, **0 failures**.
2. **`push.sh` → `provision.sh`** — user, code, venv, credential file, unit,
   nginx vhost (HTTP only), start, verify.
3. **DNS** — a manual A record at GoDaddy. There is no Route53 zone and no API
   credential on the Mac, so this cannot be scripted.
4. **`enable-tls.sh`** — certbot, then swap the HTTP vhost for the TLS one.

**Steps 3 and 4 cannot be reordered.** certbot validates over HTTP against a host
that must already resolve, and a failed attempt counts against Let's Encrypt rate
limits. Step 2 installs the HTTP-only vhost precisely so there is a webroot at
`/.well-known/acme-challenge/` for that validation — installing the TLS vhost
first fails `nginx -t`, because it references certificate files that do not exist
yet.

---

## Things that fail silently, and the assertions that catch them

Each of these is in `provision.sh` because the failure is invisible otherwise.

| Assertion | What it catches |
|---|---|
| 8771 and 8772 are not on a public address | a wrong `MCP_HTTP_HOST`. **The endpoint has no authentication** — loopback-only is the entire security model for it |
| no nginx config proxies 8771 | someone "helpfully" exposing the MCP endpoint, i.e. publishing a read-any-table SQL interface |
| `weathermcp` cannot read `/etc/pbp-mcp/mcp.env` | the credential separation being cosmetic rather than real |
| `/opt/pbp-mcp/.env` does not exist | a second copy of the password on the box, and a second place to rotate it |
| `pbp.conf` does not sort first in `conf.d` | **stealing `default_server`.** nginx gives it to the first `listen` it parses, which is the alphabetically-first `.conf` holding a server block. Take it, and every unknown-Host request on the box is answered by this vhost with this certificate |
| the venv's Python matches the chosen interpreter | a venv built by root's 3.9, which cannot install `mcp` and cannot be upgraded in place |
| the selftest runs **per instance, with that unit's environment** | the deploy's own false negative: invoked without it, `db.py` falls back to its development default of port 15432 — the laptop's tunnel — and every check fails with "connection refused" against a perfectly healthy local database |

A note on that last one. `provision.sh` reads the connection settings back off the
unit with `systemctl show pbp-mcp -p Environment --value` rather than repeating
them, so there is one source of truth. The `env $UNIT_ENV` is deliberately
unquoted: systemctl returns space-separated `KEY=VALUE` pairs that must word-split
into separate arguments.

---

## The interpreter trap

Three different `python3`s exist on this box and two of them are wrong:

| Path | Version | Why not |
|---|---|---|
| `ec2-user`'s `python3` | 3.13 | a **pyenv shim** under `/home/ec2-user`. `ProtectHome=true` makes it unreadable to the unit, and the service user has no access at all |
| root's `python3` | 3.9 | too old. Every release of `mcp` needs ≥3.10, and pip fails with the deeply unhelpful `Could not find a version that satisfies the requirement mcp>=1.2 (from versions: none)` |
| `/usr/bin/python3.11` | 3.11.16 | **this one.** What `weather-mcp` already runs on |

`provision.sh` runs under `sudo`, so it gets root's 3.9 unless told otherwise.
`PYTHON=` overrides it.

---

## Resource budget

This box runs a dozen services on 3.8 GB and sits ~1.1 GB into swap before this
service starts. The caps are there so a runaway query kills *this* service rather
than letting the kernel's OOM killer choose — and on this box its choice would
probably be the website.

| | weather-mcp | pbp-mcp |
|---|---|---|
| measured | 25 MB | **63 MB each**, two instances (~126 MB total) |
| `MemoryHigh` / `MemoryMax` | 160M / 240M | 240M / 360M |
| `CPUQuota` | none | 60% |

Higher than weather's because this corpus is a 2.4 GB fact table and
`guard.MAX_LIMIT` allows 2,000 rows of `play_text`, which is megabytes of Python
`str` per result. `CPUQuota` is here because CPU is the scarcer resource: 2 vCPU
shared with Postgres, nginx, two Streamlit apps and the weather MCP.

Re-measure after real traffic:

```bash
ssh awsvm 'systemctl show pbp-mcp -p MemoryCurrent'
```

(`MemoryPeak` returns nothing on this box's systemd — do not rely on it.)

---

## The app that does not exist yet

`pbp.conf` proxies `127.0.0.1:8504` and **nothing listens there.** Until something
does, the host serves `maintenance.html`. That is the intended state: DNS, TLS and
the rate limits are settled and proven before there is an application to break.

When you build it, the three things the vhost assumes:

1. **It binds `127.0.0.1:8504` only.** Streamlit otherwise binds `0.0.0.0`, which
   publishes the port on the public EIP — past nginx, past its rate limits and
   past every security header. Add the same assertion `provision.sh` makes for
   8771.
2. **It runs as its own user** (`pbpapp`), holding the LLM API key and **no
   database credential**. `pbp_ro`'s password belongs to `pbpmcp` alone; the
   public-facing process is the one an attacker reaches first.
3. **It carries its own spend caps.** nginx cannot provide them — see the long
   note in `pbp.conf`. Streamlit sends every user action over one long-lived
   WebSocket, so a visitor can ask twenty questions generating zero further HTTP
   requests for `limit_req` to count. `limit_conn` bounds parallel sessions and is
   the useful lever; the actual cap must be a per-session question limit and a
   daily token budget that fails closed.

The weather stack's `sql_explorer/` is the working example of all three.
