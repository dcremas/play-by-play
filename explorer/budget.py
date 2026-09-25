"""Spend caps for a public, unauthenticated endpoint that costs money per question.

ADAPTED FROM ../../ec2-nginx/weather-sql-explorer/sql_explorer/budget.py, deliberately and
nearly verbatim. That module is concurrency-correct in a way that is easy to get subtly
wrong -- the flock is held across the whole read-modify-write, which is what keeps the
counter right when two Streamlit workers answer at the same instant -- and rewriting it
from memory to look original would be a worse engineering decision than reusing it. The
differences here are the env var prefix, the defaults, and one extra dimension: this app
serves TWO corpora from two backends, and the ledger is shared across both because the
money is.

WHY NGINX CANNOT DO THIS
------------------------
Streamlit sends every user action over ONE long-lived WebSocket. A visitor loads the page
-- a burst of HTTP requests, which `limit_req` sees -- and then asks twenty questions over
that already-established connection, generating ZERO further HTTP requests to count. So
`limit_conn` bounds how many sessions one IP can hold, and everything else has to live
here. mcp_server/deploy/pbp.conf says the same thing from the other side.

THE LEDGER FAILS CLOSED. An unreadable file reads as fully spent, and `status()` reports it
that way too, so the banner and the gate agree rather than the banner promising budget that
the next question is refused for.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from pathlib import Path

# On the box this is a StateDirectory the service user owns. Locally it falls next to the
# app. It must be on disk and survive a restart, because Streamlit restarts often and a
# ledger held in memory is a budget that resets whenever it is inconvenient.
STATE_PATH = Path(
    os.environ.get("PBPX_STATE_PATH",
                   str(Path(__file__).resolve().parent / ".budget.json"))
)

MAX_QUESTIONS_PER_SESSION = int(os.environ.get("PBPX_MAX_SESSION_QUESTIONS", "12"))
MAX_TOKENS_PER_DAY = int(os.environ.get("PBPX_MAX_DAILY_TOKENS", "2000000"))

# Hard stop on the agent's tool-calling loop. An agent retrying a failing query is the
# expensive failure mode and a question COUNT cannot see it -- one question can be twenty
# model calls. This corpus makes that likelier than the weather one did: there are more
# tables, the NULL semantics invite a second attempt, and describe_table returns a lot of
# prose the model may re-read.
MAX_AGENT_STEPS = int(os.environ.get("PBPX_MAX_AGENT_STEPS", "28"))

# Ceiling on what a single question may be charged, so one pathological run cannot empty
# the day in one go and so an absurd usage report cannot corrupt the ledger.
MAX_TOKENS_PER_QUESTION = int(os.environ.get("PBPX_MAX_QUESTION_TOKENS", "140000"))


class BudgetExceeded(RuntimeError):
    """Raised when a question must not be answered. The message is shown to the user."""


def _today() -> str:
    # UTC, not local: the box runs UTC and the reset must not move twice a year.
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def _read_locked(handle) -> dict:
    handle.seek(0)
    raw = handle.read()
    if not raw.strip():
        return {"date": _today(), "tokens": 0, "questions": 0}
    return json.loads(raw)


def _write_locked(handle, state: dict) -> None:
    handle.seek(0)
    handle.truncate()
    json.dump(state, handle)
    handle.flush()
    os.fsync(handle.fileno())


def _with_ledger(mutate):
    """Run `mutate(state) -> state` under an exclusive lock on the ledger.

    Opened "a+" so the file is created if absent without truncating it if present. The
    lock is held across read-modify-write: read-then-write without it loses one of two
    simultaneous increments, which is a budget that quietly undercounts under exactly the
    load it exists to bound.
    """
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            state = _read_locked(handle)
            if state.get("date") != _today():          # roll over on first access
                state = {"date": _today(), "tokens": 0, "questions": 0}
            state = mutate(state)
            _write_locked(handle, state)
            return state
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def status() -> dict:
    """Current spend, for display. Never raises -- a broken ledger reads as spent."""
    try:
        state = _with_ledger(lambda s: s)
        used = int(state.get("tokens", 0))
        return {"date": state.get("date"), "tokens_used": used,
                "tokens_limit": MAX_TOKENS_PER_DAY,
                "remaining": max(0, MAX_TOKENS_PER_DAY - used),
                "questions_today": int(state.get("questions", 0)), "healthy": True}
    except Exception as exc:  # noqa: BLE001 - must not take the page down
        return {"date": _today(), "tokens_used": MAX_TOKENS_PER_DAY,
                "tokens_limit": MAX_TOKENS_PER_DAY, "remaining": 0,
                "questions_today": 0, "healthy": False, "error": str(exc)}


def check_can_spend(session_questions: int) -> None:
    """Raise BudgetExceeded unless another question may be answered now.

    Called BEFORE the model runs. The daily check uses tokens already recorded, so the cap
    can be overshot by at most one question -- bounded by MAX_TOKENS_PER_QUESTION.
    Reserving up front instead would need a refund path for every failure, and a crashed
    request would leak reservations until midnight.
    """
    if session_questions >= MAX_QUESTIONS_PER_SESSION:
        raise BudgetExceeded(
            f"This session has reached its limit of {MAX_QUESTIONS_PER_SESSION} questions. "
            "Reload the page to start a new one."
        )
    try:
        state = _with_ledger(lambda s: s)
    except Exception as exc:  # noqa: BLE001
        raise BudgetExceeded(
            f"The usage ledger could not be read, so no request will be sent. ({exc})"
        ) from exc

    if int(state.get("tokens", 0)) >= MAX_TOKENS_PER_DAY:
        raise BudgetExceeded(
            "This demo has reached its daily usage budget and will reset at 00:00 UTC. "
            "The warehouse and both MCP servers are unaffected -- only the language model "
            "is paused."
        )


def record(tokens: int) -> dict:
    """Charge a completed question. Called even when it FAILED.

    A failed agent run still burns tokens, and exempting failures is the hole a retry loop
    escapes through.
    """
    charged = max(0, min(int(tokens or 0), MAX_TOKENS_PER_QUESTION))

    def mutate(state: dict) -> dict:
        state["tokens"] = int(state.get("tokens", 0)) + charged
        state["questions"] = int(state.get("questions", 0)) + 1
        return state

    try:
        return _with_ledger(mutate)
    except Exception:  # noqa: BLE001
        # Losing an increment is bad but must not lose the user's answer. The next
        # check_can_spend fails closed if the file is still unreadable.
        return status()
