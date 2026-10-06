"""The page's look: PromptPace's, carried into Streamlit.

The palette, the type, the cards and the footer are lifted from
~/projects/typing/app/static/styles.css so the two apps read as one estate. Colours that
Streamlit itself draws -- buttons, inputs, the segmented control, charts -- come from
.streamlit/config.toml, and the values here must stay equal to those.

HOW THE CSS FINDS THINGS
------------------------
Streamlit's generated class names change between releases; these two hooks do not:
  * data-testid attributes, which Streamlit keeps stable for its own tests;
  * `st-key-<key>` classes, which any container given `key=` carries. Every card on the
    page is `st.container(key="card-...")`, so `[class*="st-key-card"]` is "a card".

DARK MODE follows the visitor's OS, as PromptPace's does. config.toml defines both themes
and toolbarMode="minimal" hides the menu that could override the OS, so the media query
below and Streamlit's own choice cannot disagree.
"""
from __future__ import annotations

import html

import streamlit as st

_CSS = """
<style>
:root {
  --pp-surface: #fcfcfb; --pp-surface-2: #f0efec; --pp-border: #e2e0da;
  --pp-text: #0b0b0b; --pp-text-2: #52514e; --pp-muted: #74726c;
  --pp-accent: #2a78d6; --pp-accent-ink: #1c5cab; --pp-accent-soft: rgb(42 120 214 / .10);
  --pp-series-2: #eb6834; --pp-dot: rgb(0 0 0 / .055);
  --pp-live: #16a34a; --pp-live-halo: rgb(22 163 74 / .20);
  --pp-warn: #9a6700; --pp-warn-bg: rgb(250 178 25 / .14);
  --pp-danger: #d03b3b; --pp-danger-bg: rgb(208 59 59 / .08);
  --pp-mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;
  --pp-shadow: 0 1px 2px rgb(0 0 0 / .04), 0 4px 16px rgb(0 0 0 / .04);
}
@media (prefers-color-scheme: dark) {
  :root {
    --pp-surface: #1a1a19; --pp-surface-2: #232321; --pp-border: #2e2e2b;
    --pp-text: #ffffff; --pp-text-2: #c3c2b7; --pp-muted: #93928a;
    --pp-accent: #3987e5; --pp-accent-ink: #86b6ef; --pp-accent-soft: rgb(57 135 229 / .16);
    --pp-series-2: #d95926; --pp-dot: rgb(255 255 255 / .045);
    --pp-live: #34d399; --pp-live-halo: rgb(52 211 153 / .22);
    --pp-warn: #fab219; --pp-warn-bg: rgb(250 178 25 / .12);
    --pp-danger: #e66767; --pp-danger-bg: rgb(230 103 103 / .12);
    --pp-shadow: none;
  }
}

/* ---- the frame: no Streamlit chrome, a dotted page, a 960px column ---- */
header[data-testid="stHeader"], [data-testid="stDecoration"], [data-testid="stToolbar"],
[data-testid="stStatusWidget"] { display: none !important; }
[data-testid="stApp"] {
  background-image: radial-gradient(var(--pp-dot) 1px, transparent 1.2px);
  background-size: 22px 22px;
}
[data-testid="stApp"]::before {
  content: ""; position: fixed; inset: 0 0 auto 0; height: 4px; z-index: 999990;
  background: linear-gradient(90deg, var(--pp-accent) 0 60%, var(--pp-series-2));
}
[data-testid="stMainBlockContainer"] { max-width: 960px; padding: 2.25rem 1rem 0; }

/* ---- header ---- */
.pp-brand { display: flex; align-items: center; gap: 10px; margin: 0; padding: 0; }
.pp-brand::before { content: ""; width: 40px; height: 40px; flex: none;
  background: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%232a78d6'/%3E%3Cg transform='rotate%28-35 16 16%29' fill='none' stroke='%23fff' stroke-linecap='round'%3E%3Cellipse cx='16' cy='16' rx='10.5' ry='6.4' stroke-width='2.2'/%3E%3Cpath d='M11.5 16h9M13.5 14.4v3.2M16 14.4v3.2M18.5 14.4v3.2' stroke-width='1.6'/%3E%3C/g%3E%3C/svg%3E") center / contain no-repeat; }
.pp-brand-text { display: flex; flex-direction: column; line-height: 1.1; }
.pp-wordmark { font-size: 1.75rem; font-weight: 750; letter-spacing: -.02em; color: var(--pp-text); }
.pp-wordmark span { color: var(--pp-accent); }
.pp-brand-text small { font-size: .8rem; font-weight: 500; color: var(--pp-muted); margin-top: 3px; }
.pp-tagline { margin: 14px 0 6px; color: var(--pp-text-2); font-size: 1rem; }

/* ---- cards ---- */
[class*="st-key-card"] {
  background: var(--pp-surface); border: 1px solid var(--pp-border); border-radius: 14px;
  box-shadow: var(--pp-shadow); padding: 20px;
}
.pp-eyebrow {
  margin: 0; font-size: .8rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .06em; color: var(--pp-muted); display: flex; align-items: center; gap: 10px;
}
.pp-chip {
  text-transform: none; letter-spacing: 0; font-weight: 600; font-size: .8rem;
  color: var(--pp-accent-ink); background: var(--pp-accent-soft); border-radius: 999px;
  padding: 2px 10px;
}
.pp-scope { margin: 2px 0 0; color: var(--pp-text-2); font-size: .9rem; }
.pp-scope b { color: var(--pp-text); font-weight: 600; }
.pp-question { margin: 6px 0 0; font-size: 1.3rem; font-weight: 550; line-height: 1.4;
  color: var(--pp-text); text-wrap: balance; }
.pp-meta { margin: 0; color: var(--pp-muted); font-size: .8rem; }
.pp-meta b { font-family: var(--pp-mono); font-weight: 600; color: var(--pp-text-2);
  font-variant-numeric: tabular-nums; }
.pp-status { display: inline-block; color: var(--pp-text-2); font-size: .875rem; line-height: 1.5; }
.pp-status::before { content: ""; display: inline-block; vertical-align: 1px; margin-right: 8px;
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--pp-live); box-shadow: 0 0 0 3px var(--pp-live-halo); }
.pp-status[data-state="paused"]::before { background: var(--pp-warn); box-shadow: none; }
.pp-status b { font-family: var(--pp-mono); font-weight: 600; color: var(--pp-text);
  font-variant-numeric: tabular-nums; }
.pp-label { margin: 18px 0 6px; font-size: .75rem; font-weight: 650; letter-spacing: .07em;
  text-transform: uppercase; color: var(--pp-muted); }

/* ---- notices (in place of Streamlit's coloured alert boxes) ---- */
.pp-notice { margin: 0; padding: 12px 16px; border-radius: 10px; font-size: .925rem;
  line-height: 1.5; color: var(--pp-text-2); background: var(--pp-surface-2);
  border: 1px solid var(--pp-border); }
.pp-notice b { color: var(--pp-text); }
.pp-notice[data-tone="warn"] { background: var(--pp-warn-bg); border-color: transparent; }
.pp-notice[data-tone="error"] { background: var(--pp-danger-bg); border-color: transparent; }

/* ---- the ask box ---- */
.st-key-card-ask [data-testid="stTextInput"] input { font-size: 1.1rem; padding: 12px 14px; }
.st-key-card-ask [data-testid="stForm"] { border: 0; padding: 0; }
.st-key-card-ask [data-testid="stFormSubmitButton"] button { padding: 9px 20px; font-weight: 600; }

/* Example chips: a long question wraps inside its chip rather than ending in an ellipsis,
   which on a phone cut every one of them off mid-word. */
[class*="st-key-ex-"] button { height: auto; max-width: 100%; text-align: left; }
[class*="st-key-ex-"] button * { white-space: normal !important; overflow: visible !important;
  text-overflow: clip !important; }

/* ---- answers and reference ---- */
[class*="st-key-card"] [data-testid="stExpander"] details { border-radius: 10px; }
[class*="st-key-card"] [data-testid="stCode"] pre { border-radius: 10px; }
.pp-table-name { font-family: var(--pp-mono); font-weight: 600; color: var(--pp-text); }
.pp-limit { padding: 12px 0; border-top: 1px solid var(--pp-border); }
.pp-limit:first-child { border-top: 0; padding-top: 4px; }
.pp-limit h4 { margin: 0 0 2px; font-size: .95rem; font-weight: 600; color: var(--pp-text); padding: 0; }
.pp-limit h4::first-letter { text-transform: uppercase; }
.pp-limit code, .pp-scope code { font: .82em var(--pp-mono); background: var(--pp-surface-2);
  padding: 1px 5px; border-radius: 5px; }
.pp-limit p { margin: 4px 0 0; font-size: .9rem; color: var(--pp-text-2); line-height: 1.5; }
.pp-limit .pp-applies { color: var(--pp-muted); font-size: .8rem; margin: 0; }
.pp-limit .pp-todo { color: var(--pp-text); }
.pp-limit .pp-todo::before { content: "→ "; color: var(--pp-accent); font-weight: 700; }

@media (max-width: 640px) {
  [class*="st-key-card"] { padding: 16px; }
  .pp-wordmark { font-size: 1.5rem; }
  .pp-question { font-size: 1.15rem; }
}
</style>
"""

# THE BRAND MARK IS CSS, NOT MARKUP. st.html sanitises its input and drops <svg> entirely,
# so an inline mark rendered as nothing. A data-URI background survives because the
# sanitiser never sees the stylesheet's contents. (A football on PromptPace's blue square.)


def inject() -> None:
    """The stylesheet. Call once, first. st.html with only a <style> renders no element."""
    st.html(_CSS)


def header(tagline: str) -> None:
    st.html(
        '<h1 class="pp-brand">'
        + '<span class="pp-brand-text"><span class="pp-wordmark">Football <span>SQL</span></span>'
        + "<small>by Dustin Cremascoli</small></span></h1>"
        + f'<p class="pp-tagline">{html.escape(tagline)}</p>'
    )


def notice(text_html: str, tone: str = "info") -> None:
    """A quiet message box. `text_html` is trusted markup -- escape anything dynamic."""
    st.html(f'<p class="pp-notice" data-tone="{tone}">{text_html}</p>')
