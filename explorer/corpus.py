"""The two corpora this app can be pointed at, and what is true of one and not the other.

ONE MCP SERVER PER LEAGUE, so choosing a corpus means choosing an ENDPOINT, not adding a
filter. That is the whole shape of this app and it comes straight from the warehouse: the
leagues are separate schemas served by separate processes, no tool takes a `league`
argument, and there is no query that can reach across them. A visitor switching corpora is
switching backends.

WHY THE PER-CORPUS NUMBERS LIVE HERE RATHER THAN IN PROSE
---------------------------------------------------------
Every scalar below differs between the leagues and several are the kind that read as
plausible when wrong. A sentence promising "9.9% of punts and kickoffs state no outcome" is
the COLLEGE figure; the NFL's is 3.4%, and the blended number describes neither. Keeping
them in one table per corpus means the UI can state the right one, and means changing a
number is an edit in a single place rather than a hunt through copy.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Corpus:
    key: str
    label: str
    short: str
    mcp_url: str
    blurb: str
    examples: list[str] = field(default_factory=list)
    # Per-corpus facts the system prompt and the UI both need. See the module docstring
    # for why these are not written into prose.
    unstated_pct: str = ""
    notes: str = ""


CFB = Corpus(
    key="cfb",
    label="College football",
    short="CFB",
    mcp_url=os.environ.get("PBPX_MCP_URL_CFB", "http://127.0.0.1:8771/mcp"),
    blurb="FBS, 2014 to last weekend. 1.53M scrimmage plays, 321k kicks, 10,631 games.",
    unstated_pct="9.9%",
    notes=(
        "The corpus deliberately includes FBS-vs-FCS games, so any question about player "
        "or kicker quality should restrict to both_top_division. Conference membership is "
        "grained (team, season): 83 of 275 teams changed conference inside the window."
    ),
    examples=[
        "Which kickers were best from 50+ yards since 2020?",
        "How has fourth-down conversion rate changed by season?",
        "Which teams gained the most yards per rush in 2024?",
        "Did touchback rate really jump in 2018?",
    ],
)

NFL = Corpus(
    key="nfl",
    label="NFL",
    short="NFL",
    mcp_url=os.environ.get("PBPX_MCP_URL_NFL", "http://127.0.0.1:8772/mcp"),
    blurb="2014 to last weekend. 452k scrimmage plays, 92k kicks, 3,327 games.",
    unstated_pct="3.4%",
    notes=(
        "both_top_division is constant true here -- the NFL has no second division -- so "
        "it is never a useful filter. The kickoff rule changed across 2024 and 2025 and "
        "touchback rate moves sharply between them; treat those seasons separately."
    ),
    examples=[
        "Which kickers were best from 50+ yards since 2020?",
        "How did touchback rate change after the 2024 kickoff rule?",
        "Which teams ran most often on third and short in 2024?",
        "Who forced the most turnovers last season?",
    ],
)

ALL: tuple[Corpus, ...] = (CFB, NFL)
BY_KEY = {c.key: c for c in ALL}
DEFAULT = CFB.key


def resolve(key: str | None) -> Corpus:
    """A corpus from a key, falling back to the default rather than raising.

    The key reaches this from a Streamlit widget and from the URL, so it is caller input.
    Returning the DEFAULT for anything unrecognised keeps a mangled query string from
    turning into a stack trace on a public page.
    """
    return BY_KEY.get((key or "").strip().lower(), BY_KEY[DEFAULT])
