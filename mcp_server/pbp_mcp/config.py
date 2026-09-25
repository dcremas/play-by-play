"""Which corpus this server instance serves.

ONE SERVER, ONE LEAGUE. College football and the NFL are answered separately --
the way ESPN segregates them -- and no question crosses the two. Each is a schema
of its own (`cfb.*`, `nfl.*`) holding identical table names, so the same code
serves either and `search_path` is what selects the corpus.

    MCP_DB_SCHEMA=cfb  python -m pbp_mcp.server --http   # 127.0.0.1:8771
    MCP_DB_SCHEMA=nfl  python -m pbp_mcp.server --http   # 127.0.0.1:8772

WHY A SERVER-LEVEL SETTING RATHER THAN A TOOL ARGUMENT
------------------------------------------------------
Every tool used to take `league`, and 14 of the 25 carried it in their schema.
Binding it here instead removes it from the model's job entirely: it cannot be
forgotten on a join, cannot be passed inconsistently between two calls in one
answer, and costs nothing per turn. Team ids COLLIDE between the corpora -- team 2
is Auburn and also the Buffalo Bills -- so a league-blind join used to be the most
likely way to get a confidently wrong answer out of this warehouse. Inside one
schema there is nothing to get wrong.

THE SCHEMA NAME IS NOT USER INPUT AND MUST NEVER BECOME IT. It is interpolated
into `search_path` and into the allow-list `run_sql` is checked against, so it is
validated against a fixed tuple here and the TUPLE'S OWN STRING is what is used --
the environment variable is never the thing that reaches SQL.
"""
from __future__ import annotations

import os

# The only corpora that exist. A schema name outside this set is a configuration
# error, not a request to be honoured.
LEAGUES: tuple[str, ...] = ("cfb", "nfl")

# Kept for messages and for the handful of places a human-readable corpus name
# reads better than the schema name.
LEAGUE_LABEL = {"cfb": "FBS college football", "nfl": "the NFL"}


class ConfigError(RuntimeError):
    """Raised at import time -- a misconfigured server must not start and serve."""


def _resolve() -> str:
    raw = (os.environ.get("MCP_DB_SCHEMA") or "").strip().lower()
    if not raw:
        raise ConfigError(
            "MCP_DB_SCHEMA is not set. This server serves ONE league; set it to "
            f"one of {', '.join(LEAGUES)}.\n"
            "  local:  MCP_DB_SCHEMA=cfb ./.venv/bin/python -m pbp_mcp.server\n"
            "  on the box: it is an Environment= line in pbp-mcp@.service."
        )
    if raw not in LEAGUES:
        raise ConfigError(
            f"MCP_DB_SCHEMA={raw!r} is not a corpus. Valid values: {', '.join(LEAGUES)}."
        )
    # Return the tuple's own string, never the caller's.
    return LEAGUES[LEAGUES.index(raw)]


SCHEMA: str = _resolve()
LEAGUE: str = SCHEMA          # they are the same thing; both names read naturally
LABEL: str = LEAGUE_LABEL[SCHEMA]
