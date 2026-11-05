"""Ask Football: ask a football question, watch it become SQL.

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

THE LOOK is PromptPace's (~/projects/typing), carried over in style.py and
.streamlit/config.toml: one 960px column of cards on a dotted page, light or dark by the
visitor's OS. Named "Ask Football" (was "Football SQL" until 2026-10-12: "SQL" means
nothing to most visitors, so the name says what you do and the tagline says how), paired
with the weather app's "Ask Weather". "Play-by-Play Explorer" is
the Dash app's name on the main site, and two apps under one name was a coin toss.
"""
from __future__ import annotations

import html
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

# Flat imports, not `from . import`: Streamlit executes app.py as a SCRIPT, so
# this directory is sys.path[0] and there is no parent package to be relative to.
# `from . import agent` fails with "attempted relative import with no known parent
# package" -- and it fails in the browser, not the terminal, so the symptom is a
# blank page and a clean log. The weather explorer is laid out the same way.
import agent
import budget
import charts
import corpus
import estate
import style

st.set_page_config(page_title="Ask Football — Dustin Cremascoli", page_icon="🏈",
                   layout="wide")
style.inject()


# --------------------------------------------------------------------------- #
# Reads that need no model -- free, cached, and still working when the model is not
# --------------------------------------------------------------------------- #

@st.cache_data(ttl=300, show_spinner=False)
def _schema_for(key: str) -> list[dict]:
    """The readable tables, straight from the server."""
    payload = agent.call_tool(corpus.resolve(key), "list_schema", {})
    return payload.get("tables", []) if isinstance(payload, dict) else []


@st.cache_data(ttl=300, show_spinner=False)
def _limits_for(key: str) -> list[dict]:
    payload = agent.call_tool(corpus.resolve(key), "known_limits", {})
    return payload.get("limits", []) if isinstance(payload, dict) else []


@st.cache_data(ttl=600, show_spinner=False)
def _coverage_for(key: str) -> dict | None:
    """Games, plays and the last game loaded -- the numbers the scope line states.

    Read live because a hand-typed count is a count that goes stale: the old header said
    "to last weekend" for an NFL corpus three weeks behind. None when the server is down,
    and the page then states the scope in words only.
    """
    try:
        payload = agent.call_tool(corpus.resolve(key), "data_coverage", {})
    except Exception:  # noqa: BLE001 - the scope line is decoration, not the page
        return None
    if not isinstance(payload, dict) or "totals" not in payload:
        return None
    t = payload["totals"]
    last = max((s.get("last_kickoff") or "" for s in payload.get("seasons") or []),
               default="")
    live = (payload.get("in_progress") or [None])[0]
    return {"games": t.get("games") or 0,
            "plays": (t.get("scrimmage_plays") or 0) + (t.get("kick_plays") or 0),
            "first": t.get("first_season"), "last": last,
            "week": live.get("last_regular_week") if live else None}


def _plays(n: int) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"


def _scope_html(c: corpus.Corpus) -> str:
    cov = _coverage_for(c.key)
    bits = [f"<b>{html.escape(c.label)}</b>", html.escape(c.blurb.rstrip("."))]
    if cov:
        bits += [f"{cov['games']:,} games", f"{_plays(cov['plays'])} plays"]
        if cov["last"]:
            # Kickoffs are stored in UTC; a Saturday night game is Sunday in UTC, and
            # "through Sunday" for a slate that ended Saturday reads as a missing day.
            day = datetime.fromisoformat(cov["last"]).astimezone(ZoneInfo("America/New_York"))
            through = f"{day:%b} {day.day}, {day.year}"
            bits.append(f"through week {cov['week']} ({through})" if cov["week"]
                        else f"through {through}")
    return f'<p class="pp-scope">{" · ".join(bits)}</p>'


# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #

st.session_state.setdefault("asked", 0)
st.session_state.setdefault("history", [])
st.session_state.setdefault("corpus", corpus.DEFAULT)
st.session_state.setdefault("next_id", 0)


def _switch_league() -> None:
    # The selector is the backend. Switching it is switching MCP servers, so the history
    # from the other corpus is left behind rather than re-rendered under the wrong league.
    picked = st.session_state.get("league")
    if picked and picked != st.session_state.corpus:
        st.session_state.corpus = picked
        st.session_state.history = []


def _pick_example(key: str) -> None:
    """A chip click asks its question. The chip is then cleared so it can be asked again."""
    chosen = st.session_state.get(f"ex-{key}")
    if chosen:
        st.session_state.pending = chosen
    st.session_state[f"ex-{key}"] = None


c = corpus.resolve(st.session_state.corpus)
status = budget.status()
left_q = max(0, budget.MAX_QUESTIONS_PER_SESSION - st.session_state.asked)
remaining_pct = status["remaining"] / max(status["tokens_limit"], 1) * 100
paused = agent.model_status()
can_ask = status.get("healthy") and status["remaining"] > 0 and left_q > 0 and not paused

_UNAVAILABLE = {
    "billing": ("<b>Answers are paused.</b> The language model behind this page is out of "
                "credit. The data notes below still work, and the warehouse "
                "itself is unaffected."),
    "rate": ("<b>The language model is busy.</b> It refused the last request as too many "
             "at once. Try again in a minute."),
}


# --------------------------------------------------------------------------- #
# Header and the ask card
# --------------------------------------------------------------------------- #

style.header("Ask about any college football or NFL play since 2014. A language model "
             "writes the SQL, runs it read-only, and shows you the query.")

with st.container(key="card-ask"):
    with st.container(horizontal=True, vertical_alignment="center", key="ask-top"):
        # Stretch, not content: the eyebrow's leader line fills the gap to the control.
        st.html('<p class="pp-eyebrow">Ask a question</p>', width="stretch")
        st.segmented_control(
            "League", options=[x.key for x in corpus.ALL],
            format_func=lambda k: corpus.BY_KEY[k].label,
            default=st.session_state.corpus, required=True, key="league",
            on_change=_switch_league, label_visibility="collapsed",
        )
    st.html(_scope_html(c))

    with st.form("ask", border=False, clear_on_submit=False):
        question = st.text_area(
            "Your question", max_chars=400, label_visibility="collapsed", height="content",
            placeholder=c.examples[0] if c.examples else "Ask about this league…",
        )
        with st.container(horizontal=True, vertical_alignment="center"):
            if paused:
                st.html('<span class="pp-status" data-state="paused">Answers paused</span>',
                        width="content")
            elif not status.get("healthy"):
                st.html('<span class="pp-status" data-state="paused">Usage ledger '
                        'unreadable — questions paused</span>', width="content")
            else:
                st.html(f'<span class="pp-status"><b>{left_q}</b> questions left · '
                        f'<b>{remaining_pct:.0f}%</b> of today\'s model budget</span>',
                        width="content")
            st.space("stretch")
            submitted = st.form_submit_button("Ask", type="primary", disabled=not can_ask)

if submitted and question and question.strip():
    st.session_state.pending = question.strip()

if c.examples:
    st.html('<p class="pp-label">Try one of these</p>')
    st.pills("Try one of these", c.examples, key=f"ex-{c.key}", disabled=not can_ask,
             on_change=_pick_example, args=(c.key,), label_visibility="collapsed")


# --------------------------------------------------------------------------- #
# Asking
# --------------------------------------------------------------------------- #

if paused:
    style.notice(_UNAVAILABLE[paused], "warn")
elif not status.get("healthy"):
    style.notice("<b>Questions are paused.</b> The usage ledger is unreadable, and the page "
                 "fails closed on purpose rather than spend without counting. "
                 f"({html.escape(str(status.get('error', 'unknown')))})", "error")

pending = st.session_state.pop("pending", None)
if pending:
    try:
        with st.spinner(f"Asking the {c.label} warehouse… this usually takes 10–40 seconds"):
            t0 = time.time()
            result = agent.ask(c, pending, st.session_state.asked,
                               history=st.session_state.history)
        result.update(elapsed=time.time() - t0, question=pending, id=st.session_state.next_id)
        st.session_state.next_id += 1
        st.session_state.asked += 1
        st.session_state.history.insert(0, result)
        # The status line is rendered ABOVE this block, so without a rerun it shows the
        # state from before the question that just ran -- a counter that reads 10 after
        # you have asked one. Rerunning redraws it with the ledger's real numbers.
        st.rerun()
    except agent.ModelUnavailable:
        # agent.model_status() now reports the outage, so the rerun draws the paused
        # status line and the notice from the top of the page.
        st.rerun()
    except budget.BudgetExceeded as exc:
        style.notice(html.escape(str(exc)), "warn")
    except agent.SetupError as exc:
        style.notice("<b>The warehouse is unreachable right now.</b> Please try again "
                     "shortly.", "error")
        with st.expander("Details for the operator"):
            st.code(str(exc), language=None)
    except Exception as exc:  # noqa: BLE001 - the page must survive a bad question
        # The turn was still charged; see agent.ask. Saying so keeps the counter honest
        # rather than looking like a free failure.
        style.notice("<b>That question could not be answered.</b> Try rephrasing it. It was "
                     "still counted against the daily budget, because it still spent tokens.",
                     "error")
        with st.expander("Details"):
            st.code(f"{type(exc).__name__}: {exc}", language=None)


# --------------------------------------------------------------------------- #
# Answers
# --------------------------------------------------------------------------- #

def _md(text: str) -> str:
    # A bare `$` opens LaTeX in st.markdown, so "$5 tickets ... $10" renders as math.
    return text.replace("$", r"\$")


def _frame(payload) -> pd.DataFrame | None:
    if isinstance(payload, dict) and payload.get("rows") is not None:
        return pd.DataFrame(payload["rows"], columns=payload.get("columns"))
    return None


# Integers that are labels, not quantities: "2,014" is not a season.
_PLAIN_INT = ("season", "year", "week", "down", "quarter", "period", "game_month", "month",
              "fg_dist_bucket", "jersey")


def _column_config(df: pd.DataFrame) -> dict:
    """Readable headers and number formats for a result table.

    The table is the evidence under every answer, so it should read like one: "Conversion
    rate (%)" over 51.9%, not conversion_rate_pct over 51.9; 2,598 attempts, not 2598;
    seasons without a thousands separator. The data itself is untouched -- sorting and the
    CSV download still see the raw values.
    """
    if df.columns.duplicated().any():
        return {}
    cfg = {}
    for col in df.columns:
        name, s = str(col).lower(), df[col]
        title = charts.label(col)
        if not pd.api.types.is_numeric_dtype(s) or pd.api.types.is_bool_dtype(s):
            cfg[col] = st.column_config.Column(title)
            continue
        values = s.dropna()
        whole = values.empty or bool((values == values.round()).all())
        if name in _PLAIN_INT or name.endswith(("_id", "_uid")):
            fmt = "%d"
        elif name.endswith("_pct") or "_pct_" in name or name == "pct":
            fmt = "%.1f%%"
        elif whole:
            fmt = "localized"
        else:
            fmt = "%.2f" if values.abs().max() < 10 else "%.1f"
        cfg[col] = st.column_config.NumberColumn(title, format=fmt)
    return cfg


def _result(q: dict, primary: bool) -> None:
    """One run_sql call: its SQL, then its rows or the reason it has none."""
    payload = q.get("result")
    df = _frame(payload)
    if primary and df is not None:
        chart = charts.pick(df)
        if chart is not None:
            st.altair_chart(chart, width="stretch")
    st.html('<p class="pp-label">SQL</p>')
    st.code(q.get("asked") or "", language="sql", wrap_lines=True)
    if df is not None:
        bits = [f"{payload.get('row_count', len(df)):,} row"
                + ("" if payload.get("row_count", len(df)) == 1 else "s")]
        if payload.get("tables"):
            bits.append("read " + ", ".join(payload["tables"]))
        st.html(f'<p class="pp-label">Result · {html.escape(" · ".join(bits))}</p>')
        # `truncated` is true whenever the page is full, including a top-10 that asked for
        # LIMIT 10 itself; limit_source == "caller" is the server saying so. Warning on
        # those put "partial answer" under every ranking on the page.
        if payload.get("truncated") and payload.get("limit_source") != "caller":
            style.notice(f"<b>Truncated at {payload.get('row_limit')} rows</b> — this is a "
                         "partial answer.", "warn")
        # Every row of a ranking or a season trend on screen at once -- "auto" stopped at
        # about ten, so a top fifteen scrolled. 35 px is the grid's row height; past twenty
        # rows the table scrolls, as a long result should.
        st.dataframe(df, hide_index=True, height=35 * (min(len(df), 20) + 1) + 3,
                     column_config=_column_config(df))
    elif isinstance(payload, dict) and payload.get("error"):
        # Shown rather than hidden: a refused query is the guard working, and watching it
        # refuse is more informative than a generic failure.
        style.notice("<b>Refused or failed:</b> " + html.escape(str(payload["error"])), "warn")
    elif payload is not None:
        st.write(payload)


# The prompt ends every answer with "Next: q1 | q2 | q3". Markdown emphasis around the label
# is tolerated, because models add it.
_NEXT = re.compile(r"^[ \t>*_-]*next[*_]*\s*[:：][*_]*\s*(.+?)\s*$", re.I | re.M)


def _split_next(answer: str) -> tuple[str, list[str]]:
    """The answer without its "Next:" line, and the follow-up questions that line offered.

    An answer without one comes back unchanged with no follow-ups: they are an offer, not
    part of the finding, so a model that skips them costs the reader nothing.
    """
    found = list(_NEXT.finditer(answer or ""))
    if not found:
        return answer, []
    last = found[-1]
    asks = [q.strip(" *_`\"'“”") for q in last.group(1).split("|")]
    asks = list(dict.fromkeys(q for q in asks if 8 <= len(q) <= 160))[:3]
    return (answer[:last.start()] + answer[last.end():]).rstrip(), asks


def _pick_next(answer_id: int) -> None:
    """A follow-up click asks it, carrying the conversation as any typed follow-up does."""
    key = f"next-{answer_id}"
    if st.session_state.get(key):
        st.session_state.pending = st.session_state[key]
    st.session_state[key] = None


def _new_conversation() -> None:
    # Clears the answers AND the follow-up context, which are the same list -- see
    # agent._prior_turns. `asked` is deliberately left alone: starting over is not a way
    # to earn more questions.
    st.session_state.history = []


if st.session_state.history:
    with st.container(horizontal=True, vertical_alignment="center"):
        st.html('<p class="pp-scope">Follow-ups can refer back: each question carries '
                f'your last {agent.CONTEXT_EXCHANGES} as context.</p>', width="stretch")
        st.button("New conversation", on_click=_new_conversation)

for item in st.session_state.history:
    with st.container(key=f"card-answer-{item['id']}"):
        charged = f"<b>{item['tokens']:,}</b> tokens"
        if not item.get("measured"):
            charged += " (estimated)"
        st.html(
            '<div style="display:flex;justify-content:space-between;align-items:center;'
            'gap:12px;flex-wrap:wrap">'
            f'<p class="pp-eyebrow">Question <span class="pp-chip">'
            f'{html.escape(corpus.resolve(item.get("corpus")).label)}</span></p>'
            f'<p class="pp-meta"><b>{item.get("elapsed", 0):.1f}s</b> · {charged}</p></div>'
            f'<p class="pp-question">{html.escape(item["question"])}</p>'
        )
        body, follow_ups = _split_next(item.get("answer") or "")
        if body:
            st.markdown(_md(body))
        else:
            style.notice("The model returned no written answer. Its queries are below.")

        # The answer rests on the LAST query: the prompt allows a second only when the
        # first errored, was refused or came back truncated. Earlier ones are attempts.
        queries = item.get("queries") or []
        if queries:
            _result(queries[-1], primary=True)
        if len(queries) > 1:
            with st.expander(f"Earlier attempts ({len(queries) - 1})"):
                for q in queries[:-1]:
                    _result(q, primary=False)

        if follow_ups:
            st.html('<p class="pp-label">Ask next</p>')
            st.pills("Ask next", follow_ups, key=f"next-{item['id']}", disabled=not can_ask,
                     on_change=_pick_next, args=(item["id"],), label_visibility="collapsed")


# --------------------------------------------------------------------------- #
# Reference -- no model involved, so it costs nothing and works when the model is down
# --------------------------------------------------------------------------- #

def _prose(text: str) -> str:
    """Escape, then let `backticks` read as code. The comments carry a literal `%%` from
    the psql script that wrote them; it is meant as one percent sign."""
    text = html.escape((text or "").replace("%%", "%"))
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


# What a visitor can ask about, one tile per subject, in the order people ask. Each names
# the table whose size it shows; the descriptions are for a reader, not for SQL.
_SUBJECTS = (
    ("scrimmage_wide", "Plays", "Rushes, passes, sacks and penalties: down, distance, field "
     "position, yards gained and who was involved."),
    ("play_wide", "Kicks", "Kickoffs, punts, field goals, extra points and two-point tries, "
     "with distances and outcomes."),
    ("drive", "Drives", "Every possession: where it started, how many plays and yards, and "
     "how it ended."),
    ("fact_game", "Games", "Date, week, home and away teams, venue and attendance."),
    ("dim_athlete", "Players", "Everyone named on a play, by ESPN ID, with position and the "
     "seasons they appear in."),
    ("dim_team", "Teams", "Each team, with its conference season by season."),
)
# The serving tables first: they are the ones a query should start from.
_TABLE_ORDER = ("play_wide", "scrimmage_wide", "drive", "fact_game")


def _approx(n) -> str:
    """A table size as a reader wants it. Rounded on purpose: these are the planner's
    estimates, and an exact-looking 1,585,215 beside 1,585,270 reads as a discrepancy."""
    if not isinstance(n, int) or n < 0:
        return ""
    if n >= 1_000_000:
        return f"{n / 1e6:.2f}M"
    if n >= 10_000:
        return f"{n / 1e3:.0f}k"
    return f"{n:,}"


def _first_sentence(text: str | None, limit: int = 170) -> str:
    text = " ".join((text or "").split())
    cut = re.search(r"(?<=[.!?])\s", text)
    text = text[:cut.start()] if cut else text
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _subjects_html(c: corpus.Corpus, tables: list[dict]) -> str:
    rows = {t.get("table_name"): t.get("approx_rows") for t in tables}
    tiles = []
    for table, title, text in _SUBJECTS:
        if c.key == "nfl" and table == "dim_team":
            text = "Each team, with its conference and division season by season."
        size = _approx(rows.get(table))
        tiles.append(
            '<div class="pp-subject"><p class="pp-subject-head">'
            f"<b>{html.escape(title)}</b>"
            + (f"<span>{size}</span>" if size else "")
            + f"</p><p>{html.escape(text)}</p></div>")
    return f'<div class="pp-subjects">{"".join(tiles)}</div>'


def _tables_html(tables: list[dict]) -> str:
    rank = {name: i for i, name in enumerate(_TABLE_ORDER)}
    ordered = sorted(tables, key=lambda t: (rank.get(t.get("table_name"), 99),
                                            t.get("table_name") or ""))
    return '<div class="pp-tables">' + "".join(
        f'<div class="pp-trow"><code>{html.escape(t.get("table_name") or "")}</code>'
        f'<span class="pp-trows">{_approx(t.get("approx_rows"))}</span>'
        f'<span>{_prose(_first_sentence(t.get("description")))}</span></div>'
        for t in ordered) + "</div>"


def _limits_html(limits: list[dict]) -> str:
    return "".join(
        '<div class="pp-limit">'
        f'<h4>{_prose(lim.get("topic"))}</h4>'
        f'<p class="pp-applies">{_prose(lim.get("applies_to"))}</p>'
        f'<p>{_prose(lim.get("limit"))}</p>'
        + (f'<p class="pp-todo">{_prose(lim["what_to_do"])}</p>'
           if lim.get("what_to_do") else "")
        + "</div>"
        for lim in limits)


# ONE CARD, NO TABS (2026-11-05). It used to be three tabs, the first a fourteen-row list of
# database tables, which put the warehouse's internals between a visitor and the footer.
# Now: what you can ask about, at a glance; then the caveats in plain words; then how it
# works; and the tables and the model's own caveat list, collapsed, for the reader who
# checks the SQL.
with st.container(key="card-reference"):
    st.html('<p class="pp-eyebrow">About the data</p>')
    try:
        tables = _schema_for(c.key)
    except Exception:  # noqa: BLE001 - the card is reference, not the page
        tables = []
    st.html(_subjects_html(c, tables))

    with st.expander("Good to know before you read a number"):
        st.html('<ul class="pp-know">'
                + "".join(f"<li>{line}</li>" for line in c.good_to_know) + "</ul>")

    with st.expander("How answers are made"):
        st.markdown(f"""
**The chain.** Your question goes to `{agent.MODEL}`, which can reach data only by calling
MCP tools. The tool that runs SQL parses it first, forces a `LIMIT`, refuses anything but a
single read-only `SELECT` over an allow-listed table, and runs it as a role that holds
`SELECT` and nothing else.

**This page has no database credential.** It cannot build SQL or open a connection, so if
it were compromised the warehouse would be unaffected.

**One server per league.** {c.label} is served by its own MCP process against its own
schema, so a question about one league cannot reach the other's data.

**Why it can still be wrong.** It writes the query; nothing checks that the query answers
*your* question. Read the SQL and the *Basis* line under each answer. That is what they are
there for.
""")

    with st.expander("For SQL readers: tables and the model's caveats"):
        if tables:
            st.html('<p class="pp-label">Tables · straight from the database</p>'
                    + _tables_html(tables))
        else:
            style.notice("The table list is unavailable right now.", "warn")
        try:
            limits = _limits_for(c.key)
        except Exception:  # noqa: BLE001
            limits = []
        if limits:
            st.html('<p class="pp-label">Known limits · the model reads these first</p>'
                    + _limits_html(limits))


# The cross-site footer, last on the page -- see estate.py.
estate.footer(
    'Play-by-play data from <a href="https://www.espn.com" target="_blank" '
    f'rel="noopener noreferrer">ESPN{estate.NEW_TAB_SR}</a>. '
    "Answers are written by a language model and can "
    "be wrong even when the query runs; the SQL is shown under every answer so you can "
    "check it. Each visit gets "
    f"{budget.MAX_QUESTIONS_PER_SESSION} questions, and the page shares a daily model "
    "budget that resets at 00:00 UTC.",
    model=agent.MODEL_FAMILY,
)
# Last of all: shows the page, which static/first-paint.css hides until the script is done.
style.reveal()
