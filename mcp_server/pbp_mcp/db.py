"""Pooled read-only connections to the `pbp` warehouse on the EC2 box.

Two entry points, separate on purpose:

  `query`          the SQL is a module-level constant from queries.py, and every
                   caller-supplied value is bound by the driver.
  `query_guarded`  the SQL is untrusted text that guard.check() has already
                   parsed, validated and rewritten.

Keeping them apart means a reviewer grepping for where dynamic SQL enters the
database finds exactly one function, and it is named for what it does.

This is a straight adaptation of
`ec2-nginx/weather-sql-explorer/mcp_server/weather_mcp/db.py`, which has been in
service against the same Postgres instance since 2026-08-20. The one structural
difference is that this server talks to a single database rather than two, so
`database` is a module constant instead of a parameter.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

import psycopg
import psycopg.conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from . import config

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# Statement timeout applied per connection. The pbp_ro role carries its own 60s
# default in this database; this is here so the server stays bounded even if it
# is ever pointed at a role that does not.
#
# It is deliberately higher than the weather server's 30s. That warehouse's
# largest table is 9.2M narrow observation rows; this one has a 2.4 GB scrimmage
# fact on a 2-vCPU box with 640 MB of shared_buffers, so a legitimate full-corpus
# aggregate can genuinely take longer than 30 seconds. The wide tables and the
# indexes in split_leagues.sql are what keep the common cases far below this.
_STATEMENT_TIMEOUT_MS = 60_000

_pool_singleton: ConnectionPool | None = None


class ConfigError(RuntimeError):
    """Raised when the server is not configured well enough to run."""


def database() -> str:
    """The database this server reads. Never taken from a tool argument."""
    _load_env()
    return os.environ.get("MCP_DB_NAME", "pbp")


def _load_env() -> None:
    """Read mcp_server/.env into os.environ without adding a dependency.

    Values already present in the environment win, so a shell export or a Claude
    Desktop `env` block overrides the file rather than fighting it.
    """
    if not _ENV_PATH.exists():
        return
    for raw in _ENV_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _conninfo() -> str:
    _load_env()
    password = os.environ.get("MCP_DB_PASSWORD")
    if not password:
        raise ConfigError(
            "MCP_DB_PASSWORD is not set. Put it in mcp_server/.env (see "
            ".env.example), or export it before starting the server. It is the "
            f"password for the {os.environ.get('MCP_DB_USER', 'pbp_ro')} role, "
            "which belongs to THIS server alone -- it is not the shared mcp_ro "
            "the weather MCP uses, so there is no copy of it in that server's "
            ".env. On the box the value comes from /etc/pbp-mcp/mcp.env instead."
        )
    return psycopg.conninfo.make_conninfo(
        host=os.environ.get("MCP_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_DB_PORT", "15432")),
        user=os.environ.get("MCP_DB_USER", "pbp_ro"),
        password=password,
        dbname=database(),
        connect_timeout=10,
        application_name=f"pbp-mcp-{config.SCHEMA}",
        # search_path is what selects the corpus, and it is set to EXACTLY ONE schema.
        # Listing both would make every unqualified name resolve to whichever comes
        # first -- a college query silently answered from NFL tables, or the reverse,
        # with no error anywhere. The role also carries a search_path default; this
        # overrides it per connection so the server is correct even against a role
        # that was set up for the other league.
        options=(f"-c statement_timeout={_STATEMENT_TIMEOUT_MS} "
                 f"-c search_path={config.SCHEMA}"),
    )


def _pool() -> ConnectionPool:
    global _pool_singleton
    if _pool_singleton is None:
        _pool_singleton = ConnectionPool(
            _conninfo(),
            min_size=0,      # lazy: the tunnel may legitimately be down at boot
            max_size=4,
            open=True,
            timeout=15,
            kwargs={"row_factory": dict_row},
        )
    return _pool_singleton


def query(sql: str, params: Iterable[Any] | None = None) -> list[dict]:
    """Run one read-only SELECT and return its rows as dicts.

    `sql` is always a module-level constant from queries.py. `params` carries
    every caller-supplied value, bound by the driver -- never interpolated.
    """
    try:
        with _pool().connection() as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                return cur.fetchall()
    except ConfigError:
        raise
    except _QUERY_FAULTS:
        # Not a connection problem -- see _QUERY_FAULTS. Let it through.
        raise
    except psycopg.OperationalError as exc:
        raise RuntimeError(_operational_hint(exc)) from exc


def query_guarded(sql: str) -> tuple[list[str], list[list]]:
    """Run one already-validated, model-generated statement for `run_sql`.

    THE CALLER MUST HAVE CALLED guard.check() FIRST. Nothing here re-validates;
    passing raw model output straight to this function defeats the guard. The
    only caller is server.run_sql.

    Returns (column_names, rows) rather than dicts, because a text-to-SQL result
    is a table for display: duplicate column names (`SELECT a.season, b.season`)
    are legal in SQL and would silently collapse into one key in a dict.
    """
    try:
        with _pool().connection() as conn:
            conn.read_only = True
            with conn.cursor(row_factory=psycopg.rows.tuple_row) as cur:
                cur.execute(sql)
                if cur.description is None:
                    # The guard only admits SELECTs, so reaching this means the
                    # guard was bypassed.
                    raise RuntimeError(
                        "Query returned no result set. Only SELECT is supported."
                    )
                columns = [d.name for d in cur.description]
                return columns, [list(r) for r in cur.fetchall()]
    except ConfigError:
        raise
    except _QUERY_FAULTS:
        raise
    except psycopg.OperationalError as exc:
        raise RuntimeError(_operational_hint(exc)) from exc


# Errors that are SUBCLASSES of psycopg.OperationalError but have nothing to do
# with the connection. Carried over from the weather server, where treating these
# as connection failures was a live bug: QueryCanceled (SQLSTATE 57014,
# statement_timeout) inherits from OperationalError, so every timed-out query
# reported "the SSH tunnel is almost certainly down" on a perfectly healthy
# tunnel. That matters more here, not less -- this warehouse is big enough on this
# box that hitting the timeout is an ordinary event, not an exotic one.
_QUERY_FAULTS = (
    psycopg.errors.QueryCanceled,
    psycopg.errors.IdleInTransactionSessionTimeout,
)


def _probe() -> str:
    """Connect once, directly, to find out what is actually wrong.

    THE POOL HIDES THE CAUSE. With min_size=0 the pool connects on demand, and
    when that fails every attempt is swallowed and the caller gets a bare
    `PoolTimeout: couldn\'t get a connection after 15.00 sec` -- which says nothing
    about refused connections, authentication or a missing database. This reaches
    past the pool for the real error so the hint below can be specific.

    Returns "" if the probe unexpectedly succeeds or cannot be made.
    """
    try:
        conninfo = psycopg.conninfo.make_conninfo(_conninfo(), connect_timeout=5)
        with psycopg.connect(conninfo):
            return ""
    except Exception as exc:  # noqa: BLE001 -- any failure here is the diagnosis
        return str(exc).strip()


def _operational_hint(exc: Exception) -> str:
    """Turn a connection failure into something actionable.

    The overwhelmingly common cause is the SSH tunnel being down, and the raw
    psycopg message ("connection refused") does not point there.
    """
    text = str(exc).strip()
    lowered = text.lower()

    # A pool timeout carries no diagnosis of its own -- see _probe. Replace it with
    # the underlying error before classifying, or a dead tunnel gets reported as
    # "the connection itself looks fine", which is exactly backwards and sends
    # someone to debug their SQL.
    if "couldn\'t get a connection" in lowered or isinstance(exc, PoolTimeout):
        probed = _probe()
        if probed:
            text = f"{probed}\n\n(surfaced via the connection pool: {text})"
            lowered = text.lower()
    if "authentication" in lowered or "password" in lowered:
        return (
            f"Database authentication failed for role "
            f"{os.environ.get('MCP_DB_USER', 'pbp_ro')}. Check MCP_DB_PASSWORD in "
            "mcp_server/.env matches the password the role actually has. That "
            "role is shared with the weather warehouse MCP, so if this server "
            "started failing after a rotation, both .env files need the new "
            f"value.\n\n{text}"
        )
    if "does not exist" in lowered:
        return (
            "Database or role missing -- has setup_role_pbp.sql been applied to "
            f"the pbp database?\n\n{text}"
        )
    connect_failure = any(
        marker in lowered
        for marker in (
            "could not connect", "connection refused", "timeout expired",
            # the pool's own wording, in case the probe above could not run
            "couldn\'t get a connection",
            "no route to host", "network is unreachable", "connection reset",
            "server closed the connection unexpectedly", "terminating connection",
        )
    )
    if connect_failure:
        return (
            "Could not reach Postgres on "
            f"{os.environ.get('MCP_DB_HOST', '127.0.0.1')}:"
            f"{os.environ.get('MCP_DB_PORT', '15432')}. The SSH tunnel is almost "
            "certainly down -- public 5432 is closed, so the tunnel is the only "
            "route. Restart it with:\n\n"
            "  ssh -f -N -T -L 15432:127.0.0.1:5432 awsvm\n\n"
            f"{text}"
        )
    return (
        "Postgres returned an operational error. The connection itself looks "
        f"fine, so this is most likely the query or the server's state:\n\n{text}"
    )


def close_all() -> None:
    """Close the pool. Called on server shutdown."""
    global _pool_singleton
    if _pool_singleton is not None:
        try:
            _pool_singleton.close()
        except Exception:  # noqa: BLE001 -- shutdown must not raise
            pass
        _pool_singleton = None
