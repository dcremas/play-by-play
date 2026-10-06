# Football SQL — the play-by-play SQL explorer

**Ask a football question in English; watch it become SQL.** Streamlit in front of Gemini,
which reaches data only by calling MCP tools against a `SELECT`-only Postgres role.

    https://pbp.dustincremascoli.com        (the box)
    ./run.sh                                (local, needs both MCP servers)

| | |
|---|---|
| **Corpora** | two — college football and the NFL, **one MCP server each** |
| **Endpoints** | `127.0.0.1:8771` (cfb), `:8772` (nfl); the app itself on `:8504` |
| **Tools exposed to the model** | 7 of the servers' 25 |
| **Credentials held** | the LLM API key. **No database credential** |
| **On the box** | `pbp-explorer.service`, user `pbpapp`, behind nginx |

---

## The chain, and why it has an extra hop

```
browser -> nginx (TLS, rate limits) -> Streamlit :8504
             -> Gemini
               -> MCP tools over loopback HTTP -> :8771 / :8772
                 -> pbp_ro on Postgres, SELECT-only, search_path pinned to one schema
```

**This page never touches Postgres and never builds SQL.** It cannot: the only way it
reaches data is a tool call, and the only tool that runs SQL parses it first, forces a
`LIMIT`, refuses anything but a single read-only `SELECT` over an allow-listed table, and
executes it as a role holding `SELECT` and nothing else. Compromising this process yields
no database credential because it never had one — `pbpapp` cannot read
`/etc/pbp-mcp/mcp.env`, and `provision.sh` asserts that every deploy.

## One server per league, not one server with a filter

The corpora are separate schemas behind separate processes. **No tool takes a `league`
argument.** So the selector switches *backends*, and two things follow:

* A cross-league query is not something the model can write by accident — the other
  corpus is not in its tool set, and the guard refuses the other schema by name. Asked
  about Mahomes' Chiefs career, the college server declines and says why, in under 2,000
  tokens, without running SQL.
* The prompt is smaller. `league` used to appear in 14 of 25 tool schemas.

## Seven tools of twenty-five, and it is not about cost

Measured with Gemini's tokeniser: all 25 schemas cost **4,345** prompt tokens per turn,
these seven cost **1,049**. Real, but small beside the ~45k a question spends — tuning
`reasoning_effort` saves more than every tool schema combined. The set is chosen for what
it does to the *answers*:

| Tool | Why |
|---|---|
| `list_schema`, `describe_table`, `run_sql` | the point: a question becoming visible SQL |
| `known_limits` | 95 tokens. This corpus has traps that return a plausible wrong number rather than an error |
| `data_coverage` | 138 tokens. Stops a season still being played being quoted as finished |
| `find_team`, `find_player` | name → id. Some athlete names are shared by different people |

`leaderboard` and the other typed tools would answer many of these questions *more
reliably*, which is exactly why they are out: a page that displays a tool name instead of
a `SELECT` has nothing to look at.

## Spend caps, and why nginx cannot provide them

Streamlit sends every user action over **one long-lived WebSocket**. A visitor loads the
page — a burst of HTTP requests, which `limit_req` sees — then asks twenty questions over
that established connection, generating **zero** further HTTP requests to count. So
`limit_conn` bounds parallel sessions and everything else lives in `budget.py`: a
per-session question limit and a daily token ledger that **fails closed**. An unreadable
ledger reads as fully spent, and `status()` reports it that way too, so the banner and the
gate agree.

A failed question is still charged. A crashed or looping run spent tokens, and exempting
failures is the hole a retry loop escapes through.

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # fill in PBPX_API_KEY
# both servers, one per corpus:
(cd ../mcp_server && MCP_DB_SCHEMA=cfb MCP_HTTP_PORT=8771 ./.venv/bin/python -m pbp_mcp.server --http &)
(cd ../mcp_server && MCP_DB_SCHEMA=nfl MCP_HTTP_PORT=8772 ./.venv/bin/python -m pbp_mcp.server --http &)
./run.sh
```

`run.sh` checks both servers answer before starting, because a dead one surfaces as an
empty tool list and the model then answers with no data — which looks like a model problem
and is not.

## Six things that cost time here

1. **`mcp<2` is pinned, and the servers run `mcp>=2`.** `langchain-mcp-adapters` imports
   `RequestContext` from a module v2 moved. Two processes sharing a wire protocol, not a
   library. Do not unify them.
2. **Flat imports, not `from . import`.** Streamlit executes `app.py` as a *script*, so
   this directory is `sys.path[0]` and there is no parent package. The relative form fails
   in the browser rather than the terminal, so the symptom is a blank page and a clean log.
3. **Message content is a list of typed blocks, not a string.** Assuming the string form
   returns an empty answer and renders a question with no reply under it. `_text()` exists
   for this; tool results are paired by `tool_call_id`, not by order, because one turn can
   issue several calls at once.
4. **`HOME` must be set in the unit and must not be under `/home`.** Streamlit probes
   `$HOME/.streamlit/secrets.toml` before serving; with `ProtectHome=true` that raises
   `PermissionError`, which Streamlit does not handle, and the service crash-loops.

5. **`st.html` sanitises, and it is not obvious how.** Inline `<svg>` is dropped
   entirely, and a `<style>` whose text contains `<` followed by a letter is deleted
   whole -- DOMPurify's mXSS guard -- so one CSS comment naming an element unstyled the
   footer. Icons here are CSS data-URIs (`style.py`, `estate.py`) for that reason.
6. **MCP tool results arrive as text blocks, not dicts.** `call_tool` decodes them. Before
   it did, the Tables and Known limits tabs rendered empty in production with no error.

## The look

PromptPace's (`~/projects/typing/app/static/styles.css`): the palette lives twice, in
`.streamlit/config.toml` for what Streamlit draws and in `style.py` for the cards, header
and footer, and the two must stay equal. Light or dark follows the visitor's OS.
`charts.py` picks at most one chart per answer and documents each rule with the wrong
chart that motivated it.

## Deploying

`../mcp_server/deploy/push.sh` ships this directory alongside the servers and
`provision.sh` installs it. See `../mcp_server/deploy/README-deploy.md`.
