"""The text-to-SQL agent: Gemini, reached through MCP tools, driven from Streamlit.

SHAPE OF THE THING
------------------
    Streamlit (sync)
      -> one background asyncio loop, for the app's whole life
        -> LangChain agent (Gemini), ONE PER CORPUS
          -> MCP tools over streamable HTTP to 127.0.0.1:8771 (cfb) or :8772 (nfl)
            -> pbp_ro on Postgres, SELECT-only, search_path pinned to that corpus

The app holds NO database credentials and builds NO SQL. It cannot: the only way it reaches
data is by calling MCP tools, and the only tool that runs SQL runs it through guard.py as a
read-only role. That is the reason for the extra hop -- the web tier being compromised does
not put the warehouse at risk, because the web tier was never trusted with it.

ONE AGENT PER CORPUS, NOT ONE AGENT WITH A LEAGUE ARGUMENT
-----------------------------------------------------------
College football and the NFL are separate schemas behind separate servers, and no tool
takes a `league` argument. So the corpus is chosen by which MCP endpoint the agent was
built against, and switching leagues in the UI switches agents. Two consequences worth
stating because they are the point:

  * A model cannot write a cross-league query even by accident. There is nothing in its
    tool set that reaches the other corpus, and the guard refuses the other schema by name.
  * The per-turn prompt is smaller. `league` used to appear in 14 of 25 tool schemas and
    18 times in their prose; none of that is sent now.

Agents are cached per corpus and built lazily, so a visitor who only ever looks at college
never opens a connection to the NFL server.

TWO VERSION TRAPS, BOTH INHERITED FROM THE WEATHER EXPLORER AND BOTH REAL
--------------------------------------------------------------------------
1. `langchain-mcp-adapters` does not work with the `mcp` 2.x SDK -- it imports
   `RequestContext` from `mcp.shared.context`, which v2 moved:

       ImportError: cannot import name 'RequestContext' from 'mcp.shared.context'

   THIS APP THEREFORE PINS `mcp<2`, while the SERVERS run `mcp>=2` in their own venv.
   That is not an oversight and the two must not be unified. They are separate processes
   sharing a wire protocol, not a library.

2. The MCP server must be reachable BEFORE the agent is built, because tool discovery is a
   live call. A dead server surfaces as an empty tool list and the model then answers with
   no data, which looks like a model problem and is not. `_build` checks explicitly and
   fails loudly instead.

WHY A BACKGROUND EVENT LOOP
---------------------------
Streamlit re-executes the script top to bottom on every interaction, from a thread it owns.
The MCP client and the agent are async and their sessions are tied to the loop that created
them, so `asyncio.run(...)` per interaction would build a fresh loop each time, strand the
previous connections, and eventually raise "attached to a different loop". One long-lived
loop on a daemon thread, cached for the process, keeps the clients valid across reruns.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_mcp_adapters.client import MultiServerMCPClient

import budget
import corpus


def _load_env() -> None:
    """Read explorer/.env without adding a dependency.

    Values already in the environment win, so the systemd unit and a shell export both
    override the file rather than fighting it.
    """
    path = Path(__file__).resolve().parent / ".env"
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# Read at import, BEFORE the constants below -- both read the environment, so a .env
# loaded any later than this is a .env that silently does nothing.
_load_env()

MODEL = os.environ.get("PBPX_MODEL", "gemini-3.8-flash")

# SEVEN TOOLS OF THE SERVERS' TWENTY-FIVE, and the choice is not about cost.
#
# Measured with Gemini's own tokeniser: all 25 schemas cost 4,345 prompt tokens per TURN,
# these seven cost 1,049, and a question runs several turns. Real, but small beside the
# ~60k a reasoning turn spends -- tuning reasoning_effort saves more than every tool schema
# combined. So this set is chosen for what it does to the ANSWERS.
#
#   list_schema, describe_table, run_sql   the demo's whole point: a question becoming SQL.
#                                          The typed tools would answer many questions more
#                                          reliably, and that is exactly why they are out --
#                                          the visible query is the thing worth seeing.
#   known_limits                           95 tokens. The cheapest insurance here: this
#                                          corpus has traps that return a plausible wrong
#                                          number rather than an error.
#   data_coverage                          138 tokens. Stops a season still being played
#                                          from being quoted as a finished one.
#   find_team, find_player                 name -> id. Some athlete names are shared by
#                                          different people, and grouping by name merges
#                                          them into one career.
EXPOSED_TOOLS = ("list_schema", "describe_table", "run_sql",
                 "known_limits", "data_coverage", "find_team", "find_player")


SYSTEM_PROMPT = """\
You are a data analyst answering questions about {label} play-by-play by writing \
PostgreSQL.

THIS SERVER HOLDS ONE LEAGUE: {label}, and nothing else. There is no league column and no \
tool takes one. If the question is about the other league, say so and stop -- do not \
answer it from this data.

HOW TO WORK
1. Call known_limits() FIRST, before any SQL. This corpus has traps that return a \
plausible wrong number rather than an error, and the list is short.
2. Call list_schema once to see the readable tables.
3. Call describe_table for every table you intend to query, BEFORE writing SQL. The column \
comments carry the NULL semantics and they are authoritative.
4. Write ONE SELECT and run it with run_sql.

ONE QUERY, NOT A SERIES. Run a second statement only if the first errored, was refused, or
came back truncated -- not to refine a result you already have. Every extra turn resends the whole conversation, and describe_table alone is several thousand tokens, so polishing a good answer into a slightly better one costs more than the answer was worth. If a threshold is arguable (a minimum number of attempts, say), pick a sensible one, state it, and move on rather than trying several.

TABLE NAMES ARE UNQUALIFIED OR PREFIXED `{schema}.` -- for example `{schema}.play_wide`. \
There is no `pbp.` schema to read; a query naming one is refused and costs you a turn.

PREFER THE WIDE TABLES. play_wide (kicks: kickoffs, punts, field goals, conversions) and \
scrimmage_wide (rushes, passes, sacks, penalties) already have the dimensions joined on, \
including the (team_id, season) conference resolution that a hand-written join gets wrong. \
The two facts are disjoint and play_uid is unique across both.

NULL IS MEANINGFUL AND MUST NOT BE COALESCED AWAY
- fg_made IS NULL is a kick wiped out by a penalty, NOT a miss. Exclude those rows from \
the denominator; counting them as misses understates every field goal percentage.
- returned IS NULL means the feed never stated an outcome -- {unstated_pct} of punts and \
kickoffs here. Exclude them from rate denominators and say what share was unstated. On \
field goals and conversions the same NULL means "does not apply", so scope any \
returned/touchback question to play_kind IN ('punt','kickoff').
- yards_to_goal = 0 is a null sentinel, not the goal line. Exclude it from field position.
- yards_gained on a turnover is the DEFENCE's return, not the offence's gain. Exclude \
turnovers from any mean-yards measure.

PEOPLE AND TEAMS
- Group by athlete_id and LABEL with known_name, never the reverse: some names are shared \
by more than one person and they are different people. Use find_player to resolve a name.
- Careers here are scoped to this league. A player who also played in the other one has a \
separate, unlinked record there; never present these numbers as a whole career.

{notes}

BEFORE YOU QUOTE A NUMBER
- Call data_coverage if the question touches a recent season. A season still being played \
is incomplete and weighted toward early-season games; say so rather than trending it.
- If run_sql returns truncated: true, say so -- the result is a partial answer.
- If a query is rejected or errors, read the message and fix it. Do not retry the same \
statement.

STYLE
Answer in a few sentences. Lead with the finding, not with a description of what you did. \
Give numbers with their units and to one decimal place. Do not paste the SQL into your \
answer -- the page shows it already. When you bucket by time, alias the bucket clearly \
-- `AS season`, `AS week` -- because the chart picks its axis from the column name.
"""


# --------------------------------------------------------------------------- #
# The background loop
# --------------------------------------------------------------------------- #

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, daemon=True,
                             name="pbpx-agent-loop").start()
            _loop = loop
        return _loop


def run_sync(coro, timeout: float = 180.0) -> Any:
    """Run a coroutine on the background loop and block until it finishes.

    The timeout is the backstop for a whole agent turn. Each individual statement is
    already capped at 60s by pbp_ro's statement_timeout, but an agent can issue several in
    sequence, so the turn needs its own ceiling or a pathological question holds a
    Streamlit worker indefinitely.
    """
    future: Future = asyncio.run_coroutine_threadsafe(coro, _get_loop())
    return future.result(timeout=timeout)


# --------------------------------------------------------------------------- #
# Building agents, one per corpus
# --------------------------------------------------------------------------- #

class SetupError(RuntimeError):
    """Configuration or connectivity problem, phrased for the operator."""


_tools_by_corpus: dict[str, dict[str, Any]] = {}
_agents: dict[str, Any] = {}
_clients: dict[str, Any] = {}
_build_lock = threading.Lock()


async def _discover(c: corpus.Corpus) -> dict[str, Any]:
    """The exposed tools for one corpus, by name. Reaches that server on first call.

    Split out of `_build` because the schema page needs list_schema and describe_table
    WITHOUT an LLM. Building the model to read a table's columns would make the schema
    reference depend on PBPX_API_KEY being set and on the provider being up, neither of
    which has anything to do with reading pg_catalog. Nothing here spends a token.
    """
    if c.key in _tools_by_corpus:
        return _tools_by_corpus[c.key]

    client = MultiServerMCPClient(
        {c.key: {"transport": "streamable_http", "url": c.mcp_url}}
    )
    try:
        discovered = await client.get_tools()
    except Exception as exc:  # noqa: BLE001 - becomes an on-page message
        raise SetupError(
            f"Could not reach the {c.label} MCP server at {c.mcp_url} ({exc}).\n\n"
            f"Locally:  MCP_DB_SCHEMA={c.key} ../mcp_server/.venv/bin/python "
            f"-m pbp_mcp.server --http\n"
            f"On the box it is the `pbp-mcp@{c.key}` systemd service."
        ) from exc

    tools = {t.name: t for t in discovered if t.name in EXPOSED_TOOLS}
    missing = set(EXPOSED_TOOLS) - set(tools)
    if missing:
        # An older server build, or a partial start. Better to say so than to run with a
        # crippled tool set and let the model improvise around the gap.
        raise SetupError(
            f"The {c.label} MCP server is running but does not expose {sorted(missing)}. "
            f"It offered: {sorted(t.name for t in discovered)}. Update the server."
        )

    _clients[c.key] = client
    _tools_by_corpus[c.key] = tools
    return tools


def call_tool(c: corpus.Corpus, name: str, arguments: dict | None = None,
              timeout: float = 45.0) -> Any:
    """Call one MCP tool directly, with no model involved.

    This is how the schema page reads structure: list_schema and describe_table return the
    table and column COMMENTs straight out of pg_catalog, so what the page shows is the
    database's own documentation and cannot drift from it. No LLM is in the path, so it is
    free and is not charged to the ledger.
    """
    async def _run() -> Any:
        tools = await _discover(c)
        tool = tools.get(name)
        if tool is None:
            raise SetupError(f"The {c.label} server does not expose {name!r}.")
        return await tool.ainvoke(arguments or {})

    return run_sync(_run(), timeout=timeout)


async def _build(c: corpus.Corpus) -> Any:
    key = os.environ.get("PBPX_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise SetupError(
            "No API key. Put PBPX_API_KEY in explorer/.env (see .env.example) or export "
            "it before starting Streamlit. On the box it is /etc/pbp-explorer/app.env."
        )
    os.environ.setdefault("GOOGLE_API_KEY", key)

    tools = await _discover(c)
    llm = ChatGoogleGenerativeAI(
        model=MODEL,
        temperature=0,        # SQL generation: same question, same query.
        max_retries=2,
        # Flash reasons by default and cannot be told not to -- the API accepts
        # low/medium/high and rejects "minimal". `low` is what the weather explorer
        # measured as most of the saving without changing its answers, and it matters more
        # here: this prompt is longer and describe_table returns a lot of prose, so a turn
        # that reasons freely over it is expensive. See MAX_AGENT_STEPS in budget.py for
        # the other half of the same problem.
        reasoning_effort="low",
    )
    prompt = SYSTEM_PROMPT.format(label=c.label, schema=c.key,
                                  unstated_pct=c.unstated_pct, notes=c.notes)
    return create_agent(llm, list(tools.values()), system_prompt=prompt)


def build_agent(c: corpus.Corpus) -> tuple[Any, list[str]]:
    """The agent for one corpus, built once and cached. Blocking -- does network I/O."""
    with _build_lock:
        if c.key not in _agents:
            _agents[c.key] = run_sync(_build(c), timeout=60.0)
        return _agents[c.key], list(_tools_by_corpus[c.key])


# --------------------------------------------------------------------------- #
# Asking a question
# --------------------------------------------------------------------------- #

def _tokens_from(messages: list) -> int:
    """Total token usage across the turn, for the ledger.

    Read from usage_metadata on each AI message. Providers have moved this field more than
    once, so a missing value is treated as unknown rather than zero -- an unknown that
    reads as zero is a question that costs nothing according to the ledger, which is how a
    budget silently stops working.
    """
    total = 0
    for message in messages:
        usage = getattr(message, "usage_metadata", None)
        if isinstance(usage, dict):
            total += int(usage.get("total_tokens") or 0)
    return total


# Charged when the provider reports no usage at all, so an unmeasurable question still
# costs something. Deliberately pessimistic.
UNKNOWN_QUESTION_TOKENS = 20_000


def _text(content: Any) -> str:
    """Flatten a message's content to text.

    LangChain messages carry EITHER a plain string OR a list of typed content blocks
    ([{"type": "text", "text": ...}]), and which one depends on the provider and the
    version. Assuming the string form is a silent failure, not a crash: the answer comes
    back empty and the page renders a question with no reply under it, which is exactly
    what the first build of this file did.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return "" if content is None else str(content)


def _decode(content: Any) -> Any:
    """Decode one MCP tool result into a dict.

    The payload is JSON inside whatever shape the message uses, so it is flattened first.
    Returned as the raw string when it will not parse, rather than raising: a tool that
    starts answering in prose should degrade to showing the prose, not take the page down.
    """
    if isinstance(content, dict):
        return content
    text = _text(content)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def ask(c: corpus.Corpus, question: str, session_questions: int) -> dict:
    """Answer one question against one corpus.

    Raises budget.BudgetExceeded BEFORE spending anything. Returns the answer text, every
    SQL statement the agent ran with its result, and the tokens charged.
    """
    budget.check_can_spend(session_questions)
    agent, _ = build_agent(c)

    charged = UNKNOWN_QUESTION_TOKENS
    try:
        result = run_sync(
            agent.ainvoke(
                {"messages": [{"role": "user", "content": question}]},
                # Hard stop on the tool-call loop. An agent retrying a failing query is
                # the expensive failure mode and a question count cannot see it.
                {"recursion_limit": budget.MAX_AGENT_STEPS},
            )
        )
    except Exception:
        # Charge the estimate even on failure: a crashed or looping run still spent
        # tokens, and exempting failures is a hole in the daily cap.
        budget.record(charged)
        raise

    messages = result.get("messages", [])
    measured = _tokens_from(messages)
    charged = measured or UNKNOWN_QUESTION_TOKENS
    budget.record(charged)

    # Every run_sql the agent issued, paired with what came back.
    #
    # PAIRED BY tool_call_id, not by order. A turn can issue several calls in ONE message
    # -- this agent routinely opens with known_limits + data_coverage + list_schema
    # together -- so "the next ToolMessage" is not reliably the answer to "the last
    # tool_call". The id is what the protocol provides for exactly this.
    asked_by_id: dict[str, str] = {}
    order: list[str] = []
    for message in messages:
        for call in (getattr(message, "tool_calls", None) or []):
            if call.get("name") == "run_sql":
                call_id = call.get("id") or f"anon-{len(order)}"
                asked_by_id[call_id] = (call.get("args") or {}).get("sql", "")
                order.append(call_id)

    results_by_id: dict[str, Any] = {}
    for message in messages:
        if getattr(message, "name", None) == "run_sql":
            call_id = getattr(message, "tool_call_id", None)
            if call_id in asked_by_id:
                results_by_id[call_id] = _decode(getattr(message, "content", None))

    queries = [{"asked": asked_by_id[i], "result": results_by_id.get(i)} for i in order]

    # The final assistant turn: the last message that carries text and asked for nothing.
    answer = ""
    for message in reversed(messages):
        if getattr(message, "tool_calls", None):
            continue
        if getattr(message, "name", None):      # a ToolMessage, not the reply
            continue
        text = _text(getattr(message, "content", None)).strip()
        if text:
            answer = text
            break

    return {"answer": answer, "queries": queries, "tokens": charged,
            "measured": bool(measured), "corpus": c.key}
