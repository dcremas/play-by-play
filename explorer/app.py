"""Play-by-Play Explorer: ask a football question, watch it become SQL.

    ./run.sh                        # local, needs both MCP servers running
    streamlit run explorer/app.py   # same thing without the checks

WHAT THIS IS FOR
----------------
Showing a question turn into a query. The MCP servers offer twenty-five tools and most of
them would answer these questions more reliably than generated SQL does -- `leaderboard`
returns the kicker ranking directly. They are deliberately not exposed: a page that
displays a tool name instead of a SELECT has nothing to look at. See EXPOSED_TOOLS in
agent.py.

TWO CORPORA, TWO BACKENDS
-------------------------
College football and the NFL are separate schemas behind separate MCP servers, so the
league selector switches which server the agent talks to -- it is not a filter. The page
holds one agent per corpus, built lazily, so a visitor who only looks at college never
opens a connection to the NFL server.

WHAT THIS PAGE DOES NOT DO
--------------------------
It never touches Postgres and never builds SQL itself. Every byte of data on it arrived
through an MCP tool call, and the only tool that runs SQL runs it through a parser and a
SELECT-only role. Compromising this process yields no database credential, because it
never had one.
"""
from __future__ import annotations

import time

import pandas as pd
import streamlit as st

# Flat imports, not `from . import`: Streamlit executes app.py as a SCRIPT, so
# this directory is sys.path[0] and there is no parent package to be relative to.
# `from . import agent` fails with "attempted relative import with no known parent
# package" -- and it fails in the browser, not the terminal, so the symptom is a
# blank page and a clean log. The weather explorer is laid out the same way.
import agent
import budget
import corpus
import estate

st.set_page_config(page_title="Play-by-Play Explorer", page_icon="🏈", layout="wide")


# --------------------------------------------------------------------------- #
# Cached resources
# --------------------------------------------------------------------------- #

@st.cache_resource(show_spinner=False)
def _agent_for(key: str):
    """One agent per corpus, cached for the process.

    cache_resource, not cache_data: this is a live object holding MCP sessions tied to the
    background loop, and a copy per session would open a connection per visitor.
    """
    return agent.build_agent(corpus.resolve(key))


@st.cache_data(ttl=300, show_spinner=False)
def _schema_for(key: str) -> list[dict]:
    """The readable tables, straight from the server. No model in the path, so free."""
    c = corpus.resolve(key)
    payload = agent.call_tool(c, "list_schema", {})
    if isinstance(payload, dict):
        return payload.get("tables", []) or payload.get("schema", [])
    return []


@st.cache_data(ttl=300, show_spinner=False)
def _limits_for(key: str) -> list[dict]:
    payload = agent.call_tool(corpus.resolve(key), "known_limits", {})
    return (payload or {}).get("limits", []) if isinstance(payload, dict) else []


# --------------------------------------------------------------------------- #
# Charting
# --------------------------------------------------------------------------- #

# Column names that mean "this is the x axis, and it is ordered". The system prompt tells
# the model to alias its bucket clearly -- `AS season`, `AS week` -- and this is the other
# half of that bargain: a vague alias costs the reader a chart, so the instruction is only
# honest if something here actually looks for the name.
_ORDERED_X = ("season", "year", "week", "month", "day", "date", "bucket",
              "fg_dist_bucket", "down", "distance", "yards", "quarter", "period")


def _maybe_chart(df: pd.DataFrame) -> None:
    """Draw a chart when the shape obviously supports one, and otherwise draw nothing.

    Deliberately conservative. A wrong chart is worse than no chart here: the table is
    already on screen and correct, so an invented axis only adds a way to misread it. Two
    shapes qualify and nothing else does.
    """
    if df.empty or len(df.columns) < 2 or len(df) < 2 or len(df) > 500:
        return

    numeric = [col for col in df.columns
               if pd.api.types.is_numeric_dtype(df[col]) and df[col].notna().any()]
    if not numeric:
        return

    lowered = {str(col).lower(): col for col in df.columns}
    x = next((lowered[name] for name in _ORDERED_X if name in lowered), None)

    if x is not None:
        # The axis is usually numeric itself -- `season` is an integer -- so it must be
        # removed from the measures rather than used to disqualify the chart. An earlier
        # version tested `x not in numeric[:1]`, which silently refused to chart every
        # by-season result, i.e. most of them.
        measures = [col for col in numeric if col != x]
        if not measures:
            return
        # RATES AND COUNTS DO NOT SHARE AN AXIS. A frame with total_kickoffs (9,000) and
        # touchback_rate (51.5) plots the rate as a flat line along the bottom -- the
        # chart is technically correct and shows nothing. When the result has both, chart
        # the rates: the question was almost always about the rate, which is why the
        # model computed one.
        rates = [c for c in measures
                 if any(k in str(c).lower() for k in ("pct", "rate", "percent", "share", "avg"))]
        measures = rates or measures
        plot = df[[x] + measures[:3]].copy()
        # A year is a LABEL, not a quantity. Left as an integer, Streamlit formats the
        # axis with a thousands separator and the chart reads "2,014" -- which is not a
        # year anyone writes. Casting to string makes it an ordered category; the frame
        # is already sorted by the query's ORDER BY.
        if str(x).lower() in ("season", "year", "game_month", "month", "week"):
            plot[x] = plot[x].astype("Int64").astype(str)
        st.line_chart(plot.set_index(x), height=260)
        return

    # A labelled ranking: one text column, one measure, few enough rows to read. Anything
    # wider than this is a table, not a chart.
    labels = [col for col in df.columns if not pd.api.types.is_numeric_dtype(df[col])]
    if len(labels) == 1 and len(df) <= 30:
        st.bar_chart(df.set_index(labels[0])[numeric[0]], height=260)


# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #

if "asked" not in st.session_state:
    st.session_state.asked = 0
if "history" not in st.session_state:
    st.session_state.history = []
if "corpus" not in st.session_state:
    st.session_state.corpus = corpus.DEFAULT


# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #

left, right = st.columns([3, 2], vertical_alignment="bottom")
with left:
    st.title("🏈 Play-by-Play Explorer")
    st.caption(
        "Ask in English. A language model writes PostgreSQL, runs it through a read-only "
        "guard, and shows you the query."
    )
with right:
    picked = st.segmented_control(
        "Corpus",
        options=[c.key for c in corpus.ALL],
        format_func=lambda k: corpus.BY_KEY[k].label,
        default=st.session_state.corpus,
        key="corpus_pick",
    )
    # The selector is the backend. Switching it is switching MCP servers, so the history
    # from the other corpus is left behind rather than re-rendered under the wrong league.
    if picked and picked != st.session_state.corpus:
        st.session_state.corpus = picked
        st.session_state.history = []
        st.rerun()

c = corpus.resolve(st.session_state.corpus)
st.caption(f"**{c.label}** — {c.blurb}")


# --------------------------------------------------------------------------- #
# Budget banner
# --------------------------------------------------------------------------- #

status = budget.status()
if not status.get("healthy"):
    st.error(
        "The usage ledger is unreadable, so questions are paused. This fails closed on "
        f"purpose. ({status.get('error', 'unknown')})"
    )
remaining_pct = (status["remaining"] / max(status["tokens_limit"], 1)) * 100
left_q = budget.MAX_QUESTIONS_PER_SESSION - st.session_state.asked

bar, meta = st.columns([3, 1])
with bar:
    st.progress(min(1.0, max(0.0, remaining_pct / 100)),
                text=f"Daily model budget: {remaining_pct:.0f}% left "
                     f"({status['tokens_used']:,} of {status['tokens_limit']:,} tokens used)")
with meta:
    st.metric("Questions left this session", max(0, left_q))


# --------------------------------------------------------------------------- #
# Ask
# --------------------------------------------------------------------------- #

st.divider()
with st.form("ask", clear_on_submit=False):
    question = st.text_input(
        "Your question",
        placeholder=c.examples[0] if c.examples else "Ask about this corpus…",
        label_visibility="collapsed",
    )
    submitted = st.form_submit_button("Ask", type="primary", use_container_width=False)

st.caption("Try one of these:")
cols = st.columns(len(c.examples) or 1)
for col, example in zip(cols, c.examples):
    if col.button(example, use_container_width=True, key=f"ex-{c.key}-{example[:20]}"):
        question, submitted = example, True

if submitted and question and question.strip():
    try:
        with st.spinner(f"Asking the {c.short} warehouse…"):
            t0 = time.time()
            result = agent.ask(c, question.strip(), st.session_state.asked)
            result["elapsed"] = time.time() - t0
            result["question"] = question.strip()
        st.session_state.asked += 1
        st.session_state.history.insert(0, result)
        # The budget banner and the questions-left metric are rendered ABOVE this block,
        # so without a rerun they show the state from before the question that just ran --
        # a counter that reads 12 after you have asked one. Rerunning redraws the header
        # with the ledger's real numbers.
        st.rerun()
    except budget.BudgetExceeded as exc:
        st.warning(str(exc))
    except agent.SetupError as exc:
        st.error(str(exc))
    except Exception as exc:  # noqa: BLE001 - the page must survive a bad question
        # The turn was still charged; see agent.ask. Saying so keeps the counter honest
        # rather than looking like a free failure.
        st.error(
            f"That question could not be answered: {type(exc).__name__}: {exc}\n\n"
            "It was still charged against the daily budget, because it still spent tokens."
        )


# --------------------------------------------------------------------------- #
# Answers
# --------------------------------------------------------------------------- #

for i, item in enumerate(st.session_state.history):
    st.divider()
    st.markdown(f"**{item['question']}**")
    if item.get("answer"):
        st.markdown(item["answer"])

    for n, q in enumerate(item.get("queries") or []):
        with st.expander(f"SQL {n + 1} of {len(item['queries'])}", expanded=(n == 0)):
            st.code(q.get("asked") or "", language="sql")
            payload = q.get("result")
            if isinstance(payload, dict) and payload.get("rows") is not None:
                df = pd.DataFrame(payload["rows"], columns=payload.get("columns"))
                st.dataframe(df, use_container_width=True, hide_index=True)
                bits = [f"{payload.get('row_count', len(df)):,} rows"]
                if payload.get("truncated"):
                    bits.append(f"**truncated at {payload.get('row_limit')}** — "
                                "this is a partial answer")
                if payload.get("tables"):
                    bits.append("read " + ", ".join(payload["tables"]))
                st.caption(" · ".join(bits))
                _maybe_chart(df)
            elif isinstance(payload, dict) and payload.get("error"):
                # Shown rather than hidden: a refused query is the guard working, and
                # watching it refuse is more informative than a generic failure.
                st.warning(payload["error"])
            elif payload is not None:
                st.write(payload)

    charged = f"{item['tokens']:,} tokens"
    if not item.get("measured"):
        charged += " (estimated — the provider reported none)"
    st.caption(f"{item.get('elapsed', 0):.1f}s · {charged} · {c.short}")


# --------------------------------------------------------------------------- #
# Reference
# --------------------------------------------------------------------------- #

st.divider()
tab_schema, tab_limits, tab_how = st.tabs(["Tables", "Known limits", "How this works"])

with tab_schema:
    st.caption(
        "Straight from the database — these are the table COMMENTs, which is the same "
        "documentation the model reads before it writes a query. No model was involved in "
        "rendering this tab and it costs nothing."
    )
    try:
        for t in _schema_for(c.key):
            with st.expander(f"`{t.get('table_name')}` — {t.get('approx_rows') or '?':,} rows"
                             if isinstance(t.get("approx_rows"), int)
                             else f"`{t.get('table_name')}`"):
                st.write(t.get("description") or "_no description_")
    except agent.SetupError as exc:
        st.error(str(exc))

with tab_limits:
    st.caption(
        "The model is told to read these before answering. They are the cases that return "
        "a plausible wrong number rather than an error."
    )
    try:
        for lim in _limits_for(c.key):
            st.markdown(f"**{lim.get('topic')}** — _{lim.get('applies_to')}_")
            st.write(lim.get("limit"))
            st.caption("→ " + (lim.get("what_to_do") or ""))
    except agent.SetupError as exc:
        st.error(str(exc))

with tab_how:
    st.markdown(f"""
**The chain.** Your question goes to {agent.MODEL}, which can only reach data by calling
MCP tools. The tool that runs SQL parses it first, forces a `LIMIT`, refuses anything but a
single read-only `SELECT` over an allow-listed table, and executes it as a role that holds
`SELECT` and nothing else.

**This page has no database credential.** It cannot build SQL or open a connection. If it
were fully compromised the warehouse would be unaffected, because the web tier was never
trusted with it.

**One server per league.** {c.label} is served by its own MCP process against its own
schema. There is no `league` column and no tool takes one, so a cross-league query is not
something the model can write by accident — the other corpus is not in its tool set.

**Why it can still be wrong.** It writes the query; nothing checks the query answers *your*
question. Read the SQL. That is what it is there for.
    """)


# The cross-site footer, last on the page -- see estate.py.
estate.footer()
