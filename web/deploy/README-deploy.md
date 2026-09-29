# Deploying the Dash instance explorer

The explorer runs at **<https://pbp.dustincremascoli.com/plays/>**, mounted as a
path on a hostname that already existed rather than on a subdomain of its own.

```bash
bash web/deploy/push.sh              # code only -- the usual deploy
bash web/deploy/push.sh --data       # code AND the 331 MB snapshot
```

Both are safe to re-run. Neither touches the warehouse, and neither restarts the
Streamlit agent that shares this hostname.

---

## Why a path and not a subdomain

Every other application on this estate gets its own subdomain — `recipes.`,
`api.`, `sql.`, `analytics.`. This one does not, and the reason is worth keeping:

**`pbp.dustincremascoli.com` already existed, with a working certificate.** It was
provisioned on 2026-09-25 for the Streamlit text-to-SQL agent. A new subdomain
would have meant a new certbot lineage, a seventh vhost, and a DNS record — all
for a second window onto *the same warehouse*. The two apps are two lenses on one
corpus, so one hostname is the honest arrangement:

| | serves |
|---|---|
| `pbp.dustincremascoli.com/` | Streamlit agent — ask the corpus a question in English |
| `pbp.dustincremascoli.com/plays/` | Dash explorer — read the corpus one play at a time |

It also sidestepped a deadline. The registration expires **2026-10-04**; issuing a
fresh lineage in the days before that is the worst possible week to do it, because
a lapsed registration breaks issuance and renewal both.

---

## What ends up where

| Path | What | Owner |
|---|---|---|
| `/opt/pbp-web/web/` | the application code | `pbpweb` |
| `/opt/pbp-web/.venv/` | Python 3.11 virtualenv | `pbpweb` |
| `/var/lib/pbp-web/pbp_{cfb,nfl}.duckdb` | the snapshot, 331 MB | `root:pbpweb`, mode 640 |
| `/etc/systemd/system/pbp-web.service` | the unit | root |
| `/etc/nginx/conf.d/pbp-web.conf` | upstream + rate-limit zone | root |
| `/etc/nginx/pbp-plays.inc` | the `location` blocks | root |
| `/etc/nginx/proxy_params_pbp_web.inc` | proxy settings | root |
| `/etc/nginx/conf.d/pbp.conf` | **one include line spliced in** | root |

**Code and data are deliberately on different paths and different cadences.** The
code changes when the app changes; the snapshot changes when
`scripts/build_snapshot.py` runs after a warehouse update. Shipping 331 MB to
deploy a one-line CSS fix is the kind of friction that stops people deploying, so
the data is opt-in behind `--data`. `push.sh` overrides that on the first run,
when it detects the box has no snapshot and the app therefore cannot start.

This is the same split `mcp_server/deploy/push.sh` makes against
`scripts/sync_ec2.py`, for the same reason.

---

## The one edit to someone else's file

`provision.sh` splices exactly one thing into `conf.d/pbp.conf`, which belongs to
`mcp_server/deploy/`:

```nginx
    # The Dash instance explorer at /plays/. Owned by web/deploy/ in the pbp repo.
    include /etc/nginx/pbp-plays.inc;
```

It is inserted before the `error_page` line at the end of the 443 server block,
it is guarded so a re-run is a no-op, and it fails loudly rather than guessing if
that anchor is missing or ambiguous. Everything else this app asks of nginx is in
a file it owns.

Location matching in nginx is by **specificity, not file order**, so where in the
block the include sits does not affect routing. What does affect routing is the
`^~` on the `/plays/` locations — see the note in `pbp-plays.inc`.

`provision.sh` follows `NGINX-RUNBOOK.md` §9 around the edit: it fingerprints the
resolved config before and after, and archives both plus the original `pbp.conf`
to `/home/ec2-user/nginx-conf-archive/` — **not** into `conf.d/`, where stale
`.bak` files are the trap that runbook opens with.

---

## The prefix is the thing that breaks

The app is mounted at `/plays/` and **nginx does not strip the prefix**.
`proxy_pass http://pbp_web;` has no trailing URI, so gunicorn sees the path
exactly as the browser sent it, which is what `url_base_pathname="/plays/"`
expects.

Three parts have to agree, and `web/routes.py` is the module that documents why:

| | set in | value |
|---|---|---|
| where Dash registers routes and generates URLs | `PBP_WEB_PREFIX` in `pbp-web.service` | `/plays` |
| where nginx routes from | `pbp-plays.inc` | `/plays/` |
| whether nginx strips it | `proxy_params_pbp_web.inc` | no |

Change one without the others and the failure mode depends on which:

- **nginx strips but Dash does not expect it** → every request 404s. Total and
  immediate, which is the *good* case.
- **Dash serves at `/` while assets are requested from `/plays/`** → the page
  half-loads, the HTML arrives, the JavaScript does not, and nothing in either log
  says why. This is the one to avoid, and it is what "fixing" the first case by
  setting `routes_pathname_prefix="/"` produces.

`provision.sh` asserts against both: it requires a 200 from
`http://127.0.0.1:8061/plays/` (gunicorn agrees with the prefix) *and* a 200 from
`https://pbp.dustincremascoli.com/plays/` (nginx routes to it).

It also asserts the Streamlit agent at `/` still returns 200. That is the
assertion that makes sharing a hostname safe — a bad location block would break
the *other* app, and it would break silently, because nobody testing the new one
would think to load the old one.

---

## Things that fail silently, and the assertions that catch them

| Failure | Why it is silent | Assertion |
|---|---|---|
| gunicorn binds `0.0.0.0:8061` | the app works perfectly — it is just also published on the public Elastic IP, past nginx, past the rate limits, past every security header | `ss -ltn` check for a non-loopback bind, after start |
| the `/plays/` location loses to the Streamlit `location /` | the new app 404s or serves the wrong app, and only on some paths | 200 from `/plays/` through nginx |
| the `/plays/` block shadows the Streamlit app | the *old* app breaks, and nothing in the new app's testing would notice | 200 from `/` through nginx |
| a partial `.duckdb` lands under the name the app opens | DuckDB opens it and reports a corrupt or short table rather than a missing file | staged to `/tmp`, moved into place with a single `mv` |
| `Type=notify` with a gunicorn that cannot notify | the unit hangs until systemd's start timeout, then is killed — looks like the app failing to boot | `import gunicorn.systemd` checked at provision time |
| stale `__pycache__` after the move to `/opt` | tracebacks name a directory that does not exist | excluded on the way in, cleared on the way out |

---

## Pins

`requirements-deploy.txt` is separate from the repo's `requirements.txt` and the
difference is two lines. The pins that shape a number — `duckdb` and `pandas` —
are identical to local, because those do the arithmetic. `numpy` is **2.4.6 on the
box against 2.5.2 locally**, because numpy 2.5 publishes no wheel for Python 3.11.

That divergence is free only because **nothing in `web/` imports numpy** — it is
there solely as a pandas dependency. The file says so, and says what to do if that
stops being true. Re-check with:

```bash
grep -rn --include='*.py' -e 'import numpy' -e 'np\.' web/
```

### The interpreter trap

Three `python3`s exist on this box and two are wrong. This is the same table
`mcp_server/deploy/README-deploy.md` carries, and it catches people twice:

| Path | Version | Why not |
|---|---|---|
| `ec2-user`'s `python3` | 3.13 | a **pyenv shim** under `/home/ec2-user`. `ProtectHome=true` makes it unreadable to the unit |
| root's `python3` | 3.9 | too old for these wheels |
| `/usr/bin/python3.11` | 3.11.16 | **this one.** What every other service here runs on |

`provision.sh` runs under `sudo`, so it gets root's 3.9 unless told otherwise.
`PYTHON=` overrides it.

---

## Resource budget

This box runs a dozen services on 3.8 GB and sits well into swap before this
service starts. The caps exist so a runaway query kills *this* service rather than
letting the kernel's OOM killer choose — and on this box its choice would probably
be the website.

| | pbp-mcp (each) | pbp-explorer | **pbp-web** |
|---|---|---|---|
| `MemoryHigh` / `MemoryMax` | 240M / 360M | 420M / — | **560M / 768M** |
| `CPUQuota` | 60% | — | **80%** |

Higher than the others because **each gunicorn worker attaches both DuckDB files
and builds its own view set**: `web/data.py`'s connection is process-wide, so the
cost is per worker and does not amortise. That is also why the unit runs **2
workers × 4 threads** rather than 4 workers — the app hands every caller its own
cursor off one shared connection precisely so that threads are the cheap axis.

`MemoryHigh` throttles and reclaims; `MemoryMax` kills. The gap between them is
deliberate: a heavy sort should be slowed down, not killed.

Re-measure after real traffic:

```bash
ssh awsvm 'systemctl show pbp-web -p MemoryCurrent'
```

(`MemoryPeak` returns nothing on this box's systemd — do not rely on it.)

---

## The service user

A fourth user on this stack, and for a simpler reason than the others:

| user | holds |
|---|---|
| `pbpmcp` | the Postgres credential |
| `pbpapp` | the LLM API key, and no database credential |
| **`pbpweb`** | **neither** |

This app reads two read-only files off local disk and talks to nothing else — no
database, no model, no network egress, no per-request cost. It is the least
privileged process on the hostname, and the unit says so: `IPAddressDeny=any` with
only localhost allowed, `ProtectSystem=strict`, and the snapshot mounted
`ReadOnlyPaths`.

That last one is why there is no `StateDirectory`, unlike `pbp-explorer.service`
which needs one for its token ledger. This app has no state to keep. `HOME` points
at `/var/lib/pbp-web` only so a library probing `$HOME` finds a real directory —
nothing writes there.

---

## The work-in-progress banner is gone

The app used to carry a yellow alert saying it was still being built, switched on by
`PBP_WEB_WIP` in the unit. It was removed on 2026-09-29, along with the environment
variable and `ui.wip_banner()`. In its place is a plain title and one line above the
season band, in the page source rather than in the unit.

**If you are updating an existing box, drop the `Environment=PBP_WEB_WIP=` line** —
from the unit here, and from any `systemctl edit pbp-web` drop-in that set it. Nothing
reads it any more, so leaving it does no harm beyond being a lie about what the app
does. Check for one with:

```bash
ssh awsvm 'systemctl cat pbp-web | grep -n PBP_WEB_WIP'
```

The partial-season caveat it also carried did not live only there: the `2026 partial`
badge in the header is still on the page, still per league, and still carries the full
sentence in its tooltip.

---

## Refreshing the snapshot

The corpus on the box is a copy, and it goes stale the moment
`scripts/update_season.py` runs. There is no automation for this; it is the same
open follow-up the mirror has.

```bash
.venv/bin/python scripts/update_season.py 2026               # college
.venv/bin/python scripts/update_season.py 2026 --league nfl  # NFL
.venv/bin/python scripts/build_snapshot.py                   # rebuild both files
bash web/deploy/push.sh --data                               # ship them
```

The app reports the build time of the snapshot it is reading — the `snapshot`
badge in the header — so the page itself says how stale it is.

---

## Rolling back

```bash
ssh awsvm 'sudo systemctl stop pbp-web'
```

The `/plays/` location then returns the maintenance page via `error_page 502`,
and **the Streamlit agent at `/` is unaffected**. To remove the mount entirely,
delete the include line from `conf.d/pbp.conf` and `nginx -t && systemctl reload
nginx`; archived copies of the file from every provision run are in
`/home/ec2-user/nginx-conf-archive/`.
