"""D-I FBS Special Teams -- data validation and pre-analysis console.

One page, five tabs, one local DuckDB snapshot (`data/out/pbp.duckdb`, built by
`scripts/build_snapshot.py`). Nothing here talks to Postgres at runtime, so the app starts
cold in under a second and keeps working when the database does not.

Two jobs, deliberately kept in the same page:

  VALIDATE   -- does the table say what it claims? Completeness, rule assertions,
                season-over-season continuity across the 2021/2022 source boundary,
                parse confidence, athlete-link coverage.
  PRE-ANALYSE-- what is worth modelling? Distance-adjusted kicking, field-position-adjusted
                punting, the 2018 fair-catch rule in the kickoff data, and a SQL scratchpad.

Both baselines (FG make probability, punt net yards) are fit on the FULL corpus, never on
the filtered subset -- "above expected" has to be measured against a fixed league yardstick
or a filter would move the goalposts with the players.

    .venv/bin/streamlit run app.py
"""
from __future__ import annotations

import os
import textwrap

import duckdb
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import SplineTransformer

HOME = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HOME, "data", "out", "pbp.duckdb")

st.set_page_config(page_title="CFB Special Teams Console", page_icon="🏈", layout="wide")

# Plotly's default template is too loud against Streamlit's chrome.
PT = "plotly_white"
OK, WARN, BAD = "#2f9e44", "#e8a33d", "#d6455b"
KIND_COLOR = {"field_goal": "#3b7dd8", "punt": "#2f9e44", "kickoff": "#8b5cf6",
              "pat": "#e8a33d", "two_point": "#d6455b"}


# --------------------------------------------------------------------------- data access
@st.cache_resource
def connect() -> duckdb.DuckDBPyConnection:
    if not os.path.exists(DB):
        st.error(f"No snapshot at {DB}. Build it first:\n\n"
                 f"    .venv/bin/python scripts/build_snapshot.py")
        st.stop()
    return duckdb.connect(DB, read_only=True)


@st.cache_data(show_spinner=False)
def q(sql: str) -> pd.DataFrame:
    return connect().execute(sql).df()


def lit(v) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def in_list(col: str, vals) -> str:
    return f"{col} IN ({', '.join(lit(v) for v in vals)})"


@st.cache_data(show_spinner=False)
def in_progress_seasons() -> list[int]:
    """Seasons still being played, per the snapshot's season_status table.

    build_snapshot.py marks a season in progress while its most recent game kicked off
    inside the last 30 days, so this needs nothing unset in January. Snapshots built before
    season_status existed simply report none.
    """
    try:
        return q("SELECT season FROM season_status WHERE is_in_progress ORDER BY 1")["season"].tolist()
    except Exception:
        return []


def settled() -> str:
    """A WHERE fragment excluding any season that is not finished yet.

    Both baselines are fit on the whole corpus on purpose -- "above expected" needs a fixed
    yardstick -- but a season three games deep is not a point on that yardstick. fg_exp
    enters season LINEARLY, so a 2026 with 21 attempts would tug the whole decade-long
    quality trend, and every kicker in every season would be measured against it.
    """
    live = in_progress_seasons()
    return "TRUE" if not live else f"season NOT IN ({', '.join(str(x) for x in live)})"


# --------------------------------------------------------------------------- baselines
@st.cache_resource(show_spinner="Fitting field goal baseline...")
def fg_baseline() -> tuple[pd.DataFrame, dict]:
    """P(make | distance, season) over every attempt in the corpus.

    Distance gets a cubic spline because the make curve is sigmoid in log-odds but not
    linear in yards; season enters linearly to absorb the decade-long drift in kicking
    quality (PAT% went 96.6 -> 98.5 over the same window). Returns one row per attempt so
    the leaderboards can join on play_uid.
    """
    df = q("""
        SELECT play_uid, fg_distance_yds AS d, season AS s, fg_made::int AS y
        FROM play
        WHERE play_kind = 'field_goal' AND fg_made IS NOT NULL
          AND fg_distance_yds BETWEEN 15 AND 70
          AND """ + settled() + """
    """)
    X = np.array(df[["d", "s"]], dtype=float, copy=True)
    X[:, 1] -= 2020                                    # centre so the intercept is readable
    y = np.asarray(df["y"])
    model = make_pipeline(
        ColumnTransformer([("dist", SplineTransformer(n_knots=5, degree=3), [0]),
                           ("season", "passthrough", [1])]),
        LogisticRegression(max_iter=2000))
    model.fit(X, y)
    p = model.predict_proba(X)[:, 1]
    fit = {"n": len(df), "auc": roc_auc_score(y, p), "brier": brier_score_loss(y, p),
           "logloss": log_loss(y, p), "model": model}
    return df.assign(p_hat=p)[["play_uid", "p_hat"]], fit


@st.cache_resource(show_spinner="Fitting punt baseline...")
def punt_baseline() -> tuple[pd.DataFrame, dict]:
    """E[net yards | field position] over every clean punt.

    Net is dominated by where the punt starts -- a punt from the opponent's 35 is a pinning
    kick that cannot travel 45 yards without a touchback. Comparing punters on raw net
    without this adjustment mostly ranks their offenses.
    """
    df = q("""
        SELECT play_uid, yards_to_goal AS ytg, punt_net_yds::double AS net
        FROM play
        WHERE play_kind = 'punt' AND NOT kick_blocked
          AND punt_net_yds IS NOT NULL AND yards_to_goal BETWEEN 20 AND 100
          AND """ + settled() + """
    """)
    X = np.array(df[["ytg"]], dtype=float, copy=True)
    y = np.asarray(df["net"], dtype=float)
    model = make_pipeline(SplineTransformer(n_knots=6, degree=3), LinearRegression())
    model.fit(X, y)
    pred = model.predict(X)
    fit = {"n": len(df), "r2": model.score(X, y),
           "rmse": float(np.sqrt(np.mean((y - pred) ** 2))), "model": model}
    return df.assign(exp_net=pred)[["play_uid", "exp_net"]], fit


def register_baselines() -> None:
    """Expose both baselines to SQL so leaderboards stay one query, not a pandas pipeline."""
    con = connect()
    if "_baselines_registered" not in st.session_state:
        fg, _ = fg_baseline()
        pt, _ = punt_baseline()
        con.register("fg_exp", fg)
        con.register("punt_exp", pt)
        st.session_state["_baselines_registered"] = True


# --------------------------------------------------------------------------- filters
def sidebar() -> tuple[str, dict]:
    meta = q("SELECT * FROM snapshot_meta").iloc[0]
    seasons = q("SELECT DISTINCT season FROM play ORDER BY 1")["season"].tolist()
    confs = q("""SELECT DISTINCT kicking_conference c FROM play
                 WHERE kicking_conference IS NOT NULL ORDER BY 1""")["c"].tolist()

    st.sidebar.title("Filters")
    st.sidebar.caption(
        f"snapshot {pd.Timestamp(meta['built_at']):%Y-%m-%d %H:%M} · "
        f"{int(meta['rows']):,} rows · db `{meta['source_db']}`")

    live = in_progress_seasons()
    if live:
        ss = q(f"""SELECT season, games, plays, last_regular_week AS wk
                   FROM season_status WHERE season IN ({','.join(str(x) for x in live)})""")
        for r in ss.itertuples():
            st.sidebar.warning(
                f"**{r.season} is in progress** — {r.games:,} games, {r.plays:,} plays, "
                f"through week {r.wk}. It is filterable like any other season, but it is "
                f"held out of the fitted baselines and the season-continuity chart: a "
                f"part-season would move the yardstick every kicker is measured against. "
                f"Refresh it with `scripts/update_season.py {r.season}`.")

    lo, hi = st.sidebar.select_slider("Seasons", options=seasons,
                                      value=(seasons[0], seasons[-1]))
    kinds = st.sidebar.multiselect(
        "Play kind", ["field_goal", "punt", "kickoff", "pat", "two_point"],
        default=["field_goal", "punt", "kickoff", "pat", "two_point"])
    scope = st.sidebar.radio(
        "Competition scope", ["Everything ingested", "FBS vs FBS only"], index=1,
        help="The build ingests every game with at least one FBS team (PLAN.md §8). "
             "Kicker-quality work almost always wants the FBS-only cut.")
    conf = st.sidebar.multiselect("Kicking conference", confs, default=[],
                                  help="Joined on (team_id, season), so realignment is honoured.")
    surface = st.sidebar.multiselect("Surface", ["grass", "turf"], default=[])
    parse = st.sidebar.multiselect("Parse confidence",
                                   q("""SELECT DISTINCT parse_confidence p FROM play
                                        WHERE parse_confidence IS NOT NULL
                                        ORDER BY 1""")["p"].tolist(), default=[])
    season_type = st.sidebar.multiselect(
        "Season type", q("""SELECT DISTINCT season_type t FROM play
                            WHERE season_type IS NOT NULL ORDER BY 1""")["t"].tolist(),
        default=[])
    conf_only = st.sidebar.checkbox("Conference games only", value=False)
    drop_neutral = st.sidebar.checkbox("Exclude neutral-site games", value=False)

    w = [f"season BETWEEN {lo} AND {hi}"]
    if kinds:
        w.append(in_list("play_kind", kinds))
    else:
        w.append("FALSE")                              # empty multiselect means "nothing"
    if scope == "FBS vs FBS only":
        w.append("fbs_vs_fbs")
    if conf:
        w.append(in_list("kicking_conference", conf))
    if surface:
        w.append(in_list("surface", surface))
    if parse:
        w.append(in_list("parse_confidence", parse))
    if season_type:
        w.append(in_list("season_type", season_type))
    if conf_only:
        w.append("conference_game")
    if drop_neutral:
        w.append("NOT neutral_site")

    st.sidebar.divider()
    st.sidebar.caption("Rebuild the snapshot from Postgres:\n\n"
                       "`.venv/bin/python scripts/build_snapshot.py`")
    return " AND ".join(w), {"lo": lo, "hi": hi, "kinds": kinds, "scope": scope}


# --------------------------------------------------------------------------- helpers
def wilson(k: np.ndarray, n: np.ndarray, z: float = 1.96):
    """Wilson score interval -- the normal approximation is wrong at 90%+ make rates."""
    k, n = np.asarray(k, float), np.asarray(n, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        p = k / n
        d = 1 + z ** 2 / n
        c = (p + z ** 2 / (2 * n)) / d
        h = z * np.sqrt(p * (1 - p) / n + z ** 2 / (4 * n ** 2)) / d
    return c - h, c + h


def kpi(col, label, value, help_text=None):
    col.metric(label, value, help=help_text)


def pct(x, digits=1):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:.{digits}f}%"


# --------------------------------------------------------------------------- tab 1
# A punt or kickoff should always land in exactly one outcome bucket.
#
# FIXED 2026-08-31. These flags used to be booleans with no NULL state, so an unparsed
# outcome was indistinguishable from a negative one and every rate computed over them was
# silently deflated by however many rows the parser could not classify. st_parser.py now
# writes NULL on the five "how did it end" flags when the text states no outcome, which
# means `avg(touchback::int)` and friends drop those rows from BOTH the numerator and the
# denominator on their own -- the rates on this page are now taken on the classified
# subset, which is what they always should have been.
#
# The panel and the rule check stay: the unreadable share is still worth watching, it is
# still worst in 2023-2025, and it is still the thing that decides whether a per-season
# outcome rate is worth quoting. Only the deflation is gone, not the gap.
#
# `returned IS NULL` is the whole test now. The long all-false predicates it replaces were
# proven equivalent on the rebuilt snapshot -- 9,686 punts and 10,030 kickoffs either way,
# zero rows differing.
PUNT_UNCLASSIFIED = "play_kind = 'punt' AND returned IS NULL"
KO_UNCLASSIFIED = "play_kind = 'kickoff' AND returned IS NULL"


def classification_frame(where: str, kind: str) -> pd.DataFrame:
    pred = PUNT_UNCLASSIFIED if kind == "punt" else KO_UNCLASSIFIED
    return q(f"""SELECT season, count(*) AS plays,
                        count(*) FILTER (WHERE {pred}) AS unclassified,
                        100.0 * avg(({pred})::int) AS pct_unclassified
                 FROM play WHERE {where} AND play_kind = '{kind}'
                 GROUP BY 1 ORDER BY 1""")


def classification_chart(df: pd.DataFrame, kind: str):
    fig = go.Figure()
    if len(df):
        fig.add_bar(x=df["season"], y=df["pct_unclassified"],
                    marker_color=np.where(df["pct_unclassified"] > 12, BAD,
                                 np.where(df["pct_unclassified"] > 6, WARN, OK)),
                    hovertemplate="%{x}<br>%{y:.1f}% unclassified<extra></extra>")
    fig.add_hline(y=5, line_dash="dot", line_color="#999",
                  annotation_text="clean-season floor (~5%)", annotation_position="top left")
    fig.update_layout(template=PT, height=300, xaxis_title=None,
                      yaxis_title=f"% of {kind}s with no outcome flag",
                      margin=dict(l=0, r=0, t=10, b=0))
    return fig


# Tolerances are shares, not counts, so a check means the same thing whether the filter is
# one conference-season or the whole decade. `tol = 0` means the condition is impossible by
# construction and any row is a defect; a non-zero tol is a documented source quirk whose
# rate has been measured, and the check exists to catch it drifting.
CHECKS = [
    dict(name="punt with no outcome flag set", tol=0.05, pred=PUNT_UNCLASSIFIED,
         scope="play_kind = 'punt'",
         note="The share of punts whose text states no outcome at all -- ESPN's terse form "
              "(\"Joshua Brown punt for 34 yds\"). A clean season runs ~5%. These rows now "
              "carry NULL rather than false on every outcome flag, so they no longer deflate "
              "the rates built on them; this check tracks how much of each season is "
              "unreadable, which is what decides whether a per-season rate is quotable"),
    dict(name="kickoff with no outcome flag set", tol=0.05, pred=KO_UNCLASSIFIED,
         scope="play_kind = 'kickoff'",
         note="same gap on kickoffs, and it bites hardest on touchback rate, the headline "
              "kickoff metric. 2023 is the worst season and is almost entirely the terse "
              "form. The rate itself is now computed on classified kicks only"),
    dict(name="duplicate play_uid", tol=0.0,
         pred="play_uid IN (SELECT play_uid FROM play GROUP BY 1 HAVING count(*) > 1)",
         note="primary key in Postgres, so this can only break in the snapshot"),
    dict(name="kicking team = receiving team", tol=0.0,
         pred="kicking_team_id = receiving_team_id",
         note="the 2016/2018 dtype bug from Phase 3 that hit 24,676 rows; must stay at zero"),
    dict(name="null kicking team", tol=0.0, pred="kicking_team_id IS NULL",
         note="fixed by moving the spine to ESPN in Phase 3"),
    dict(name="play with no fact_game row", tol=0.0,
         pred="NOT EXISTS (SELECT 1 FROM fact_game g WHERE g.game_id = play.game_id)",
         note="referential integrity against the game dimension"),
    dict(name="kicker_athlete_id not in dim_athlete", tol=0.0,
         scope="kicker_athlete_id IS NOT NULL",
         pred="kicker_athlete_id IS NOT NULL AND NOT EXISTS "
              "(SELECT 1 FROM dim_athlete a WHERE a.athlete_id = play.kicker_athlete_id)",
         note="referential integrity against the athlete dimension"),
    dict(name="no conference for the kicking team-season", tol=0.0,
         pred="kicking_conference IS NULL",
         note="a null here would silently drop the row from every conference grouping"),
    dict(name="kick negated by penalty still has a result", tol=0.0,
         pred="negated_by_penalty AND (fg_made IS NOT NULL OR converted IS NOT NULL)",
         note="a wiped-out kick must record NULL, not made/missed — 161 FGs rely on this"),
    dict(name="touchback and returned both true", tol=0.0002,
         scope="play_kind IN ('punt', 'kickoff')",
         pred="touchback AND returned",
         note="mutually exclusive by definition; the survivors are rows where the feed "
              "itself says both (punt downed in the endzone, then a fumble return)"),
    dict(name="not returned but return_yds > 0", tol=0.0,
         scope="play_kind IN ('punt', 'kickoff')",
         pred="returned IS FALSE AND return_yds > 0",
         note="REAL PARSER GAP: the '76 Yd Punt Return (Kick)' scoring form records the "
              "yardage but never sets `returned`. Filter return analysis on return_yds "
              "IS NOT NULL, not on `returned`, until this is fixed"),
    dict(name="returned but return_yds null", tol=0.03, scope="returned",
         pred="returned AND return_yds IS NULL",
         note="the feed self-conflicts (says out-of-bounds and lists a return); the parser "
              "nulls the yardage and flags the row ambiguous rather than guessing"),
    dict(name="FG made/missed but no distance", tol=0.005,
         scope="play_kind = 'field_goal'",
         pred="play_kind = 'field_goal' AND fg_made IS NOT NULL AND fg_distance_yds IS NULL",
         note="parser recovered the result but not the yardage; these rows cannot enter any "
              "distance-adjusted model"),
    dict(name="FG distance disagrees with field position by >3 yds", tol=0.01,
         scope="play_kind = 'field_goal' AND yards_to_goal > 0",
         pred="play_kind = 'field_goal' AND fg_distance_yds IS NOT NULL AND yards_to_goal > 0 "
              "AND abs(fg_distance_yds - (yards_to_goal + 17)) > 3",
         note="distance should be yards_to_goal + 17 (10 endzone + 7 snap). A disagreement "
              "means one of the two fields is wrong on that row — this is the cheapest "
              "independent check on the parsed distance there is"),
    dict(name="yards_to_goal = 0 used as a null sentinel", tol=0.01,
         pred="yards_to_goal = 0",
         note="ESPN omits the start yardline on some plays and it lands as 0, not NULL. "
              "Exclude it from any field-position analysis or it will read as a snap on "
              "the opponent's goal line"),
    dict(name="FG distance outside 15-70", tol=0.001, scope="play_kind = 'field_goal'",
         pred="play_kind = 'field_goal' AND fg_distance_yds IS NOT NULL "
              "AND (fg_distance_yds < 15 OR fg_distance_yds > 70)",
         note="faithful parses of impossible source text, left visible rather than dropped "
              "(PLAN.md §Phase 3)"),
    dict(name="punt gross > 90 yds", tol=0.001, scope="play_kind = 'punt'",
         pred="punt_gross_yds > 90",
         note="physically impossible; same source-text class as above"),
    dict(name="kickoff distance > 100 yds", tol=0.001, scope="play_kind = 'kickoff'",
         pred="kickoff_yds > 100",
         note="physically impossible; same source-text class as above"),
    dict(name="punt net exceeds gross by >10 yds", tol=0.002, scope="play_kind = 'punt'",
         pred="punt_net_yds > punt_gross_yds + 10",
         note="net legitimately exceeds gross when the return loses yardage (~0.7% of "
              "punts), but not by ten; past that the two fields disagree"),
    dict(name="no wallclock timestamp", tol=0.05, pred="wallclock_utc IS NULL",
         note="blocks the play-level weather join planned for Phase 5; concentrated in 2017, "
              "where the old archive only carried 67% of them"),
]


def tab_validation(where: str) -> None:
    st.subheader("Rule assertions")
    st.caption("Every check runs against the filtered set, and tolerances are **shares**, so "
               "a check means the same thing on one conference-season as on the whole "
               "decade. Status is judged on the **worst single season**, not the pooled "
               "rate — a defect that lives in one season disappears when ten are averaged, "
               "which is how the 2025 punt gap stayed invisible. A non-zero count is not "
               "automatically a bug: several of these are documented source defects the "
               "build chose to keep visible rather than silently repair. Select a row to "
               "see the offending plays. `share` is measured against the rows the check "
               "applies to — punt checks against punts, not against everything.")

    # Pooling ten seasons hides a defect that lives in one of them -- the 2025 punt
    # classification gap is 46% of that season but only 4.6% of the decade. Status is
    # therefore judged on the WORST season, with the pooled share shown alongside.
    MIN_SEASON_ROWS = 500
    rows = []
    for chk in CHECKS:
        # `scope` is the check's denominator. A punt-only defect measured against every
        # special-teams play reads three times cleaner than it is.
        scope = chk.get("scope", "TRUE")
        by = q(f"""SELECT season, count(*) AS n, count(*) FILTER (WHERE {chk['pred']}) AS hits
                   FROM play WHERE {where} AND ({scope}) GROUP BY 1 ORDER BY 1""")
        if not len(by) or by["n"].sum() == 0:
            continue
        pooled = by["hits"].sum() / by["n"].sum()
        big = by[by["n"] >= MIN_SEASON_ROWS]
        big = big if len(big) else by
        share = big["hits"] / big["n"]
        i = int(share.idxmax())
        worst_share, worst_season = float(share.loc[i]), int(big.loc[i, "season"])
        total_hits = int(by["hits"].sum())
        status = ("PASS" if total_hits == 0
                  else "WARN" if worst_share <= chk["tol"] else "FAIL")
        rows.append({"check": chk["name"], "rows": total_hits, "share": pooled,
                     "worst season": worst_season if total_hits else None,
                     "worst share": worst_share, "status": status,
                     "tolerance": chk["tol"], "what it means": chk["note"]})

    if not rows:
        st.info("No rows in the current filter — nothing to check.")
        return
    res = pd.DataFrame(rows).sort_values(
        ["status", "worst share"], key=lambda c: c.map({"FAIL": 0, "WARN": 1, "PASS": 2})
        if c.name == "status" else -c).reset_index(drop=True)

    c1, c2, c3 = st.columns(3)
    kpi(c1, "PASS", int((res.status == "PASS").sum()), "zero rows matched")
    kpi(c2, "WARN", int((res.status == "WARN").sum()),
        "non-zero, but the worst season is still inside the documented tolerance")
    kpi(c3, "FAIL", int((res.status == "FAIL").sum()),
        "at least one season is over tolerance — read the note")

    picked = st.dataframe(
        res.style
           .map(lambda v: f"color:{ {'PASS': OK, 'WARN': WARN, 'FAIL': BAD}.get(v, '') };"
                          "font-weight:600", subset=["status"])
           .format({"rows": "{:,}", "share": "{:.3%}", "worst share": "{:.3%}",
                    "worst season": "{:.0f}", "tolerance": "{:.2%}"}, na_rep="—"),
        width="stretch", hide_index=True, height=430, key="checks",
        on_select="rerun", selection_mode="single-row",
        column_config={"what it means": st.column_config.TextColumn(width="large")})

    sel = picked.selection.rows if hasattr(picked, "selection") else []
    if sel:
        name = res.loc[sel[0], "check"]
        chk = next(c for c in CHECKS if c["name"] == name)
        pred = f"({chk.get('scope', 'TRUE')}) AND ({chk['pred']})"
        st.markdown(f"**Plays matching _{name}_**")
        st.dataframe(
            q(f"""SELECT season, play_kind, kicking_team, kicker_name, fg_distance_yds,
                         yards_to_goal, punt_gross_yds, punt_net_yds, return_yds, returned,
                         touchback, parse_confidence, play_text
                  FROM play WHERE {where} AND ({pred}) LIMIT 200"""),
            width="stretch", hide_index=True, height=280,
            column_config={"play_text": st.column_config.TextColumn(width="large")})

    st.divider()
    left, right = st.columns([3, 2])

    with left:
        st.subheader("Field completeness")
        st.caption("% non-null per column, by play kind. Only the **coloured** cells carry an "
                   "expectation — those columns should sit near 100 for that kind. Grey "
                   "numbers are incidental population and mean nothing on their own. "
                   "Conditional fields (`return_yds`, `returner_athlete_id`) look low here "
                   "because most kicks are never returned; their real coverage is in the "
                   "athlete-link panel below, on the right denominator.")
        st.plotly_chart(completeness_heatmap(where), width="stretch", key="completeness")

    with right:
        st.subheader("Parse confidence")
        pc = q(f"""SELECT season, coalesce(parse_confidence, 'null') AS parse_confidence,
                          count(*) n
                   FROM play WHERE {where} GROUP BY 1, 2 ORDER BY 1, 2""")
        if len(pc):
            tot = pc.groupby("season")["n"].transform("sum")
            pc["share"] = 100 * pc["n"] / tot
            fig = px.bar(pc, x="season", y="share", color="parse_confidence",
                         template=PT, height=330,
                         color_discrete_map={"exact": OK, "partial": WARN,
                                             "ambiguous": BAD, "none": "#888"})
            fig.update_layout(yaxis_title="% of rows", xaxis_title=None,
                              legend_title=None, margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig, width="stretch", key="parse_confidence")

    st.divider()
    st.subheader("Season-over-season continuity")
    st.caption("The failure this project already hit once: a metric stepped at 2021→2022 and "
               "looked like a rule change, but it was a source boundary in an earlier "
               "build. Any spike here is a claim about football that "
               "needs a football reason. Bars are the year-on-year change as a share of the "
               "prior level, so yards and percentage points are on one comparable scale; the "
               "shaded band is ±5%.")
    cont = continuity_frame(where)
    st.plotly_chart(continuity_chart(cont), width="stretch", key="continuity")
    with st.expander("The underlying levels"):
        st.dataframe(cont.style.format({c: "{:.2f}" for c in CONTINUITY}),
                     hide_index=True, width="stretch")

    st.divider()
    st.subheader("Coverage on the right denominator")
    st.caption("Returner id and return yardage are measured only on plays that were actually "
               "returned. Unconditionally they look like 20–50% coverage, which is not a gap "
               "— it is the wrong denominator. The one real hole is the 2025 kicker id: only "
               "65.7% of that season's plays appear in ESPN's participants payload at all, "
               "and the key matches perfectly where they do.")
    cov = q(f"""
        SELECT season,
               count(*) FILTER (WHERE play_kind IN ('punt','kickoff','field_goal')) AS st_plays,
               100.0 * count(kicker_athlete_id) FILTER (WHERE play_kind IN ('punt','kickoff','field_goal'))
                     / nullif(count(*) FILTER (WHERE play_kind IN ('punt','kickoff','field_goal')), 0) AS kicker_pct,
               100.0 * count(returner_athlete_id) FILTER (WHERE returned)
                     / nullif(count(*) FILTER (WHERE returned), 0) AS returner_pct,
               100.0 * count(return_yds) FILTER (WHERE returned)
                     / nullif(count(*) FILTER (WHERE returned), 0) AS return_yds_pct
        FROM play WHERE {where} GROUP BY 1 ORDER BY 1""")
    if len(cov):
        fig = px.line(cov, x="season",
                      y=["kicker_pct", "returner_pct", "return_yds_pct"], markers=True,
                      template=PT, height=300)
        fig.update_layout(yaxis_title="% linked to an athlete id", xaxis_title=None,
                          legend_title=None, yaxis_range=[0, 105],
                          margin=dict(l=0, r=0, t=10, b=0))
        fig.add_hline(y=95, line_dash="dot", line_color="#999",
                      annotation_text="95% floor", annotation_position="bottom right")
        st.plotly_chart(fig, width="stretch", key="athlete_coverage")

    with st.expander("Sample the rows the parser was unsure about"):
        amb = q(f"""SELECT season, play_kind, parse_confidence, kicker_name,
                           fg_distance_yds, punt_gross_yds, return_yds, play_text
                    FROM play
                    WHERE {where} AND parse_confidence IS DISTINCT FROM 'exact'
                    USING SAMPLE 200 ROWS""")
        st.dataframe(amb, width="stretch", hide_index=True,
                     column_config={"play_text": st.column_config.TextColumn(width="large")})


# Columns that should be populated on essentially every row of that kind. Deliberately
# excludes anything conditional -- `return_yds` is only meaningful when the kick was
# actually returned, and judging it on an unconditional denominator would flag healthy data.
#
# Since 2026-08-31 the five "how did it end" flags -- returned, touchback, fair_catch,
# downed, out_of_bounds -- are conditional in exactly that sense: they carry NULL when the
# play text states no outcome. Judging them here would paint 13% of 2025 red for a gap that
# has its own panel, measured properly, two sections up. `kick_blocked` and `onside` stay,
# because those two remain knowable on every row and so are still a real completeness test.
EXPECTED = {
    "field_goal": ["fg_distance_yds", "fg_made", "kick_blocked", "kicker_athlete_id",
                   "yards_to_goal", "wallclock_utc", "venue_id"],
    "punt": ["punt_gross_yds", "punt_net_yds", "kick_blocked", "kicker_athlete_id",
             "yards_to_goal", "wallclock_utc", "venue_id"],
    "kickoff": ["kickoff_yds", "onside", "kicker_athlete_id",
                "wallclock_utc", "venue_id"],
    "pat": ["converted", "wallclock_utc", "venue_id"],
    "two_point": ["converted", "two_point_type", "wallclock_utc", "venue_id"],
}
# Laid out by theme rather than alphabetically, so the eye reads kick → outcome → people.
FIELD_ORDER = ["fg_distance_yds", "fg_made", "miss_reason", "kick_blocked",
               "punt_gross_yds", "punt_net_yds", "kickoff_yds", "onside",
               "returned", "return_yds", "touchback", "fair_catch", "downed",
               "out_of_bounds", "converted", "two_point_type",
               "kicker_athlete_id", "returner_athlete_id",
               "yards_to_goal", "wallclock_utc", "venue_id"]


def completeness_heatmap(where: str):
    sel = ", ".join(f"100.0 * count({c}) / count(*) AS {c}" for c in FIELD_ORDER)
    df = q(f"SELECT play_kind, count(*) n, {sel} FROM play WHERE {where} GROUP BY 1")
    if not len(df):
        return go.Figure()
    df = df.set_index("play_kind").drop(columns=["n"])
    kinds = [k for k in EXPECTED if k in df.index]
    if not kinds:
        return go.Figure()
    df = df.loc[kinds, FIELD_ORDER]
    vals = df.to_numpy(dtype=float)
    # Colour only the cells that carry an expectation; everything else stays blank with the
    # number in grey, so an unexpectedly populated column is still visible but not alarming.
    mask = np.array([[c in EXPECTED[k] for c in FIELD_ORDER] for k in kinds])
    z = np.where(mask, vals, np.nan)
    text = np.round(vals, 0).astype(int).astype(str)
    fig = go.Figure(go.Heatmap(
        z=z, x=FIELD_ORDER, y=kinds, colorscale="RdYlGn", zmin=0, zmax=100,
        text=text, texttemplate="%{text}", textfont=dict(size=11),
        showscale=False, hoverongaps=False, xgap=1, ygap=1,
        hovertemplate="%{y} · %{x}<br>%{z:.1f}% populated<extra></extra>"))
    fig.update_layout(template=PT, height=400, margin=dict(l=0, r=0, t=10, b=110),
                      xaxis=dict(tickangle=-45, side="bottom"),
                      yaxis=dict(autorange="reversed"),
                      plot_bgcolor="#f6f6f6")
    return fig


CONTINUITY = {
    "FG %": "100.0 * avg(fg_made::int) FILTER (WHERE play_kind='field_goal' AND fg_made IS NOT NULL)",
    "PAT %": "100.0 * avg(converted::int) FILTER (WHERE play_kind='pat')",
    "punt gross": "avg(punt_gross_yds) FILTER (WHERE play_kind='punt' AND NOT kick_blocked AND punt_gross_yds > 0)",
    "punt net": "avg(punt_net_yds) FILTER (WHERE play_kind='punt' AND NOT kick_blocked)",
    "KO touchback %": "100.0 * avg(touchback::int) FILTER (WHERE play_kind='kickoff' AND NOT onside)",
    "punts / game": "count(*) FILTER (WHERE play_kind='punt')::double / count(DISTINCT game_id)",
    "kickoffs / game": "count(*) FILTER (WHERE play_kind='kickoff')::double / count(DISTINCT game_id)",
}


def _alias(name: str) -> str:
    return name.replace("%", "pct").replace("/", "per").replace(" ", "_").replace("__", "_")


def continuity_frame(where: str) -> pd.DataFrame:
    cols = {name: _alias(name) for name in CONTINUITY}
    sel = ", ".join(f"{expr} AS {cols[name]}" for name, expr in CONTINUITY.items())
    # An in-progress season is three weeks of football against a full prior year, so its
    # year-on-year bar measures the calendar, not the data. Held out until it is finished.
    df = q(f"SELECT season, {sel} FROM play WHERE {where} AND {settled()} "
           f"GROUP BY 1 ORDER BY 1")
    return df.rename(columns={v: k for k, v in cols.items()})


def continuity_chart(df: pd.DataFrame):
    """Year-on-year change as a percentage of the prior year's level.

    Plotting raw deltas would put percentage points and yards on one axis and make a 0.4-yard
    move in punt gross invisible next to a 5-point move in touchback rate. Relative change is
    unit-free, so every metric is judged on the same scale.
    """
    if len(df) < 2:
        return go.Figure()
    fig = go.Figure()
    for name in CONTINUITY:
        v = df[name].astype(float)
        rel = 100 * v.diff() / v.shift(1)
        fig.add_bar(x=df["season"], y=rel, name=name,
                    hovertemplate=f"{name} %{{x}}<br>%{{y:+.1f}}% vs prior<extra></extra>")
    fig.add_hrect(y0=-5, y1=5, fillcolor="#2f9e44", opacity=.06, line_width=0)
    fig.add_vline(x=2021.5, line_dash="dash", line_color="#666",
                  annotation_text="old source boundary", annotation_position="top left")
    fig.add_hline(y=0, line_color="#ccc")
    fig.update_layout(template=PT, height=380, barmode="group", legend_title=None,
                      yaxis_title="% change vs prior season", xaxis_title=None,
                      margin=dict(l=0, r=0, t=10, b=0))
    return fig


# --------------------------------------------------------------------------- tab 2
def tab_placekicking(where: str) -> None:
    fg_where = f"{where} AND play_kind = 'field_goal' AND fg_made IS NOT NULL"
    n = q(f"SELECT count(*) n FROM play WHERE {fg_where}")["n"].iloc[0]
    if not n:
        st.info("No field goal attempts in the current filter.")
        return

    _, fit = fg_baseline()
    c = st.columns(5)
    agg = q(f"""SELECT count(*) att, sum(fg_made::int) made, avg(fg_distance_yds) dist,
                       sum(kick_blocked::int) blocked,
                       max(fg_distance_yds) FILTER (WHERE fg_made) longest
                FROM play WHERE {fg_where}""").iloc[0]
    kpi(c[0], "Attempts", f"{int(agg.att):,}")
    kpi(c[1], "Make rate", pct(100 * agg.made / agg.att))
    kpi(c[2], "Avg distance", f"{agg.dist:.1f} yd")
    kpi(c[3], "Blocked", f"{int(agg.blocked):,}")
    kpi(c[4], "Longest made", f"{int(agg.longest)} yd" if pd.notna(agg.longest) else "—")

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Make rate by distance")
        st.caption("Points are observed rates with Wilson 95% intervals; the line is the "
                   "league baseline (cubic spline in distance + linear season term) fit on "
                   "all 25.6k attempts, not on the filtered subset. Monotonic decline with "
                   "no reversals is the single best structural check on the parser — a "
                   "distance field that was silently wrong would not produce this curve.")
        st.plotly_chart(fg_curve(fg_where), width="stretch", key="fg_curve")
    with right:
        st.subheader("Baseline fit")
        st.caption("AUC near 0.72 is the ceiling here, not a weak model: distance is almost "
                   "the only observable signal in a field goal, so the achievable "
                   "discrimination is low even when calibration is excellent. Calibration "
                   "is what the leaderboard depends on, and that is the table below.")
        st.dataframe(pd.DataFrame({
            "metric": ["attempts fit", "AUC", "Brier", "log loss"],
            "value": [f"{fit['n']:,}", f"{fit['auc']:.4f}", f"{fit['brier']:.4f}",
                      f"{fit['logloss']:.4f}"]}), hide_index=True, width="stretch")
        cal = q(f"""SELECT fg_dist_bucket bucket, count(*) att,
                           100.0 * avg(fg_made::int) actual, 100.0 * avg(p_hat) expected
                    FROM play JOIN fg_exp USING (play_uid)
                    WHERE {fg_where} GROUP BY 1 ORDER BY 1""")
        cal["gap"] = cal["actual"] - cal["expected"]
        st.dataframe(cal.style.format({"actual": "{:.1f}", "expected": "{:.1f}",
                                       "gap": "{:+.1f}", "att": "{:,}"}),
                     hide_index=True, width="stretch", height=260)

    st.divider()
    st.subheader("Kickers, distance-adjusted")
    st.caption("**FG points above expected** = 3 × Σ(made − P(make)). It is additive, so no "
               "shrinkage is needed and a kicker is never rewarded for a short schedule. "
               "Grouped on `kicker_athlete_id`, never on name — 71 names in this corpus are "
               "shared by more than one athlete.")
    min_att = st.slider("Minimum attempts", 10, 200, 40, step=5)
    board = q(f"""
        SELECT coalesce(a.known_name, p.kicker_name, '(unnamed)') AS kicker,
               any_value(p.kicking_team)                          AS team,
               min(p.season) || '-' || max(p.season)               AS seasons,
               count(*)                                            AS att,
               sum(p.fg_made::int)                                 AS made,
               100.0 * avg(p.fg_made::int)                         AS pct,
               avg(p.fg_distance_yds)                              AS avg_dist,
               sum(e.p_hat)                                        AS exp_made,
               3.0 * (sum(p.fg_made::int) - sum(e.p_hat))          AS pts_above_exp,
               100.0 * (sum(p.fg_made::int) - sum(e.p_hat)) / count(*) AS pct_above_exp,
               max(p.fg_distance_yds) FILTER (WHERE p.fg_made)     AS longest
        FROM play p
        JOIN fg_exp e USING (play_uid)
        LEFT JOIN dim_athlete a ON a.athlete_id = p.kicker_athlete_id
        WHERE {fg_where} AND p.kicker_athlete_id IS NOT NULL
        GROUP BY p.kicker_athlete_id, 1
        HAVING count(*) >= {min_att}
        ORDER BY pts_above_exp DESC""")
    st.dataframe(
        board.style.format({"pct": "{:.1f}", "avg_dist": "{:.1f}", "exp_made": "{:.1f}",
                            "pts_above_exp": "{:+.1f}", "pct_above_exp": "{:+.1f}",
                            "att": "{:,}", "made": "{:,.0f}", "longest": "{:.0f}"})
             .background_gradient(subset=["pts_above_exp"], cmap="RdYlGn"),
        width="stretch", hide_index=True, height=430)

    st.divider()
    a, b = st.columns(2)
    with a:
        st.subheader("Splits, held at equal difficulty")
        st.caption("Raw make rate confounds difficulty with skill. `gap` is observed minus "
                   "expected — the only column here that is comparable across splits.")
        dim = st.selectbox("Split by", ["surface", "is_clutch", "is_home_kicking",
                                        "neutral_site", "kicking_conference", "season",
                                        "conference_game"], index=0)
        sp = q(f"""SELECT coalesce(cast({dim} AS varchar), '(null)') AS split,
                          count(*) att, 100.0 * avg(fg_made::int) actual,
                          100.0 * avg(p_hat) expected, avg(fg_distance_yds) avg_dist
                   FROM play JOIN fg_exp USING (play_uid)
                   WHERE {fg_where} GROUP BY 1 HAVING count(*) >= 30 ORDER BY 2 DESC""")
        sp["gap"] = sp["actual"] - sp["expected"]
        lo, hi = wilson(sp["att"] * sp["actual"] / 100, sp["att"])
        sp["ci"] = [f"±{100 * (h - l) / 2:.1f}" for l, h in zip(lo, hi)]
        st.dataframe(sp.style.format({"actual": "{:.1f}", "expected": "{:.1f}",
                                      "gap": "{:+.1f}", "avg_dist": "{:.1f}", "att": "{:,}"})
                       .background_gradient(subset=["gap"], cmap="RdYlGn", vmin=-6, vmax=6),
                     hide_index=True, width="stretch")
    with b:
        st.subheader("Conversions")
        st.caption("The PAT drifting from ~96.6% to ~98.5% across the decade is real and is "
                   "why the baseline carries a season term. Two-point rate is a play-calling "
                   "signal, not a kicking one.")
        conv = q(f"""SELECT season,
                            100.0 * avg(converted::int) FILTER (WHERE play_kind='pat') AS pat_pct,
                            100.0 * avg(converted::int) FILTER (WHERE play_kind='two_point') AS two_pt_pct,
                            count(*) FILTER (WHERE play_kind='two_point')::double
                              / nullif(count(*) FILTER (WHERE play_kind IN ('pat','two_point')), 0)
                              * 100 AS two_pt_share
                     FROM play WHERE {where} GROUP BY 1 ORDER BY 1""")
        fig = px.line(conv, x="season", y=["pat_pct", "two_pt_pct", "two_pt_share"],
                      markers=True, template=PT, height=360)
        fig.update_layout(yaxis_title="%", xaxis_title=None, legend_title=None,
                          margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, width="stretch", key="conversions")


def fg_curve(fg_where: str):
    obs = q(f"""SELECT fg_distance_yds d, count(*) n, sum(fg_made::int) k
                FROM play WHERE {fg_where} AND fg_distance_yds BETWEEN 15 AND 70
                GROUP BY 1 HAVING count(*) >= 15 ORDER BY 1""")
    _, fit = fg_baseline()
    grid = np.arange(17, 66, 1.0)
    season_mid = q(f"SELECT avg(season) s FROM play WHERE {fg_where}")["s"].iloc[0]
    X = np.column_stack([grid, np.full_like(grid, float(season_mid) - 2020)])
    curve = fit["model"].predict_proba(X)[:, 1] * 100

    fig = go.Figure()
    if len(obs):
        lo, hi = wilson(obs["k"], obs["n"])
        p = 100 * obs["k"] / obs["n"]
        fig.add_trace(go.Scatter(
            x=obs["d"], y=p, mode="markers", name="observed",
            marker=dict(size=np.clip(obs["n"] / 12, 5, 18), color="#3b7dd8",
                        line=dict(width=0)),
            error_y=dict(type="data", symmetric=False, array=100 * hi - p,
                         arrayminus=p - 100 * lo, color="#3b7dd8", thickness=1, width=0),
            hovertemplate="%{x} yd<br>%{y:.1f}%<extra></extra>"))
    fig.add_trace(go.Scatter(x=grid, y=curve, mode="lines", name="league baseline",
                            line=dict(color="#111", width=2, dash="dash")))
    fig.update_layout(template=PT, height=430, xaxis_title="distance (yards)",
                      yaxis_title="make rate %", yaxis_range=[0, 100],
                      legend=dict(orientation="h", y=1.08, x=0),
                      margin=dict(l=0, r=0, t=10, b=0))
    return fig


# --------------------------------------------------------------------------- tab 3
def tab_punting(where: str) -> None:
    pw = f"{where} AND play_kind = 'punt'"
    n = q(f"SELECT count(*) n FROM play WHERE {pw}")["n"].iloc[0]
    if not n:
        st.info("No punts in the current filter.")
        return

    agg = q(f"""SELECT count(*) punts,
                       avg(punt_gross_yds) FILTER (WHERE NOT kick_blocked AND punt_gross_yds > 0) gross,
                       avg(punt_net_yds) FILTER (WHERE NOT kick_blocked) net,
                       100.0 * avg(touchback::int) tb, 100.0 * avg(fair_catch::int) fc,
                       100.0 * avg(returned::int) ret, sum(kick_blocked::int) blk
                FROM play WHERE {pw}""").iloc[0]
    c = st.columns(6)
    kpi(c[0], "Punts", f"{int(agg.punts):,}")
    kpi(c[1], "Gross", f"{agg.gross:.2f} yd", "FBS reality is ~41–43")
    kpi(c[2], "Net", f"{agg.net:.2f} yd")
    kpi(c[3], "Touchback", pct(agg.tb))
    kpi(c[4], "Fair catch", pct(agg.fc))
    kpi(c[5], "Returned", pct(agg.ret))

    _, pfit = punt_baseline()
    left, right = st.columns([3, 2])
    with left:
        st.subheader("Net and gross by field position")
        st.caption("The gap between the two lines is what the coverage unit and the return "
                   "give back. It widens as the punt gets longer, which is the whole "
                   "argument for judging punters on net rather than distance. The dashed "
                   "line is the spline baseline the punter table adjusts against. Its "
                   f"R² is {pfit['r2']:.3f} with RMSE {pfit['rmse']:.2f} yd — low by "
                   "construction, because a single punt's net is decided by what the "
                   "returner does, not by where it started. It is a conditional mean, not a "
                   "predictor; averaging the residual over 100+ punts is what makes it mean "
                   "something.")
        st.plotly_chart(punt_position_chart(pw), width="stretch", key="punt_position")
    with right:
        st.subheader("Outcome mix by season")
        st.caption(":red[Read the classification panel below before trusting this chart.] "
                   "Fair catch, downed and out-of-bounds are deliberate outcomes; only "
                   "`returned` gives the receiving team a chance. The bands do not sum to "
                   "100 in every season, and the shortfall is exactly the unclassified "
                   "share — it is a parsing artefact, not a change in how teams punt.")
        mix = q(f"""SELECT season,
                           100.0*avg(touchback::int) touchback,
                           100.0*avg(fair_catch::int) fair_catch,
                           100.0*avg(downed::int) downed,
                           100.0*avg(out_of_bounds::int) out_of_bounds,
                           100.0*avg(returned::int) returned,
                           100.0*avg(kick_blocked::int) blocked
                    FROM play WHERE {pw} GROUP BY 1 ORDER BY 1""")
        long = mix.melt("season", var_name="outcome", value_name="pct")
        fig = px.area(long, x="season", y="pct", color="outcome", template=PT, height=400)
        fig.update_layout(yaxis_title="% of punts", xaxis_title=None, legend_title=None,
                          margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, width="stretch", key="punt_outcome_mix")

    st.divider()
    st.subheader("Outcome classification coverage")
    cls = classification_frame(where, "punt")
    worst = cls.loc[cls["pct_unclassified"].idxmax()] if len(cls) else None
    if worst is not None and worst["pct_unclassified"] > 12:
        st.warning(
            f"**{worst['pct_unclassified']:.1f}% of {int(worst['season'])} punts state no "
            f"outcome at all** ({int(worst['unclassified']):,} of "
            f"{int(worst['plays']):,}). What remains is ESPN's terse rendering — "
            "`\"Joshua Brown punt for 34 yds\"` — which names no outcome anywhere in the "
            "text. No regex recovers what is not written.\n\n**These rows now carry NULL "
            "rather than false** on every outcome flag (fixed 2026-08-31), so they drop out "
            "of both halves of every rate on this page instead of dragging it down — the "
            "fair-catch, downed and return rates below are taken on classified punts only. "
            "What this number still tells you is how much of the season the rates are "
            "*based on*: at 30% unreadable, a season rate is one you quote with the "
            "denominator attached.\n\nThe gamebook dialect that used to dominate this "
            "number (`\"Bird,Oscar punt 55 yards to the GS27 Robinson,Javon return for loss "
            "of 3 yards\"`) **is also parsed** — it took 2025 punts from 45.8% to 21.9%, "
            "and 2025 kickoffs from 24.6% to 6.7%.")
    st.plotly_chart(classification_chart(cls, "punt"), width="stretch", key="punt_classified")

    st.divider()
    st.subheader("Punters, field-position-adjusted")
    st.caption("**Net yards above expected** per punt, against E[net | line of scrimmage]. "
               "Raw net average mostly ranks offenses; this ranks punters.")
    min_p = st.slider("Minimum punts", 20, 400, 100, step=10, key="min_punts")
    board = q(f"""
        SELECT coalesce(a.known_name, p.kicker_name, '(unnamed)') AS punter,
               any_value(p.kicking_team) AS team,
               min(p.season) || '-' || max(p.season) AS seasons,
               count(*) AS punts,
               avg(p.punt_gross_yds) AS gross,
               avg(p.punt_net_yds) AS net,
               avg(e.exp_net) AS exp_net,
               avg(p.punt_net_yds - e.exp_net) AS net_above_exp,
               sum(p.punt_net_yds - e.exp_net) AS total_yds_above_exp,
               100.0 * avg(p.touchback::int) AS tb_pct,
               100.0 * avg(p.returned::int) AS ret_pct
        FROM play p
        JOIN punt_exp e USING (play_uid)
        LEFT JOIN dim_athlete a ON a.athlete_id = p.kicker_athlete_id
        WHERE {pw} AND p.kicker_athlete_id IS NOT NULL
        GROUP BY p.kicker_athlete_id, 1
        HAVING count(*) >= {min_p}
        ORDER BY net_above_exp DESC""")
    st.dataframe(
        board.style.format({"gross": "{:.2f}", "net": "{:.2f}", "exp_net": "{:.2f}",
                            "net_above_exp": "{:+.2f}", "total_yds_above_exp": "{:+,.0f}",
                            "tb_pct": "{:.1f}", "ret_pct": "{:.1f}", "punts": "{:,}"})
             .background_gradient(subset=["net_above_exp"], cmap="RdYlGn"),
        width="stretch", hide_index=True, height=430)

    st.divider()
    st.subheader("Distribution of gross and net")
    st.caption("Two things to look for: a spike at exactly 0 (parse failures landing as zero "
               "rather than null), and a right tail past ~75 yards that physics does not "
               "allow.")
    dist = q(f"""SELECT punt_gross_yds gross, punt_net_yds net FROM play
                 WHERE {pw} AND NOT kick_blocked""")
    fig = go.Figure()
    fig.add_histogram(x=dist["gross"], name="gross", opacity=.6, xbins=dict(size=1),
                      marker_color="#3b7dd8")
    fig.add_histogram(x=dist["net"], name="net", opacity=.6, xbins=dict(size=1),
                      marker_color="#2f9e44")
    fig.update_layout(template=PT, height=320, barmode="overlay", legend_title=None,
                      xaxis_title="yards", yaxis_title="punts",
                      margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig, width="stretch", key="punt_distribution")


def punt_position_chart(pw: str):
    df = q(f"""SELECT (yards_to_goal // 5) * 5 AS ytg, count(*) n,
                      avg(punt_gross_yds) gross, avg(punt_net_yds) net
               FROM play
               WHERE {pw} AND NOT kick_blocked AND yards_to_goal BETWEEN 20 AND 100
               GROUP BY 1 HAVING count(*) >= 25 ORDER BY 1""")
    fig = go.Figure()
    if len(df):
        fig.add_trace(go.Scatter(x=df["ytg"], y=df["gross"], name="gross", mode="lines+markers",
                                 line=dict(color="#3b7dd8", width=2)))
        fig.add_trace(go.Scatter(x=df["ytg"], y=df["net"], name="net", mode="lines+markers",
                                 line=dict(color="#2f9e44", width=2), fill="tonexty",
                                 fillcolor="rgba(214,69,91,.12)"))
        _, pfit = punt_baseline()
        grid = np.arange(20, 101, 2.0).reshape(-1, 1)
        fig.add_trace(go.Scatter(x=grid.ravel(), y=pfit["model"].predict(grid),
                                 name="net baseline", mode="lines",
                                 line=dict(color="#111", width=1.5, dash="dash")))
    fig.update_layout(template=PT, height=400, legend=dict(orientation="h", y=1.08, x=0),
                      xaxis_title="yards to opponent goal line at the snap",
                      yaxis_title="yards", margin=dict(l=0, r=0, t=10, b=0))
    return fig


# --------------------------------------------------------------------------- tab 4
def tab_kickoffs(where: str) -> None:
    kw = f"{where} AND play_kind = 'kickoff'"
    n = q(f"SELECT count(*) n FROM play WHERE {kw}")["n"].iloc[0]
    if not n:
        st.info("No kickoffs in the current filter.")
        return

    agg = q(f"""SELECT count(*) ko, 100.0*avg(touchback::int) FILTER (WHERE NOT onside) tb,
                       100.0*avg(returned::int) ret,
                       avg(return_yds) FILTER (WHERE returned) ret_avg,
                       sum(onside::int) onside, sum(returned_for_td::int) td
                FROM play WHERE {kw}""").iloc[0]
    c = st.columns(5)
    kpi(c[0], "Kickoffs", f"{int(agg.ko):,}")
    kpi(c[1], "Touchback", pct(agg.tb))
    kpi(c[2], "Returned", pct(agg.ret))
    kpi(c[3], "Avg return", f"{agg.ret_avg:.1f} yd" if pd.notna(agg.ret_avg) else "—")
    kpi(c[4], "Onside / return TD", f"{int(agg.onside):,} / {int(agg.td):,}")

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Touchback rate — the rule-change test")
        st.caption("2018 let the receiving team fair-catch inside the 25 for a touchback, and "
                   "the rate steps that year without anyone telling it to — independent "
                   "evidence the touchback flag is reading the field it thinks it is. The "
                   "2021→2022 line is where the old build showed a fake step of the same "
                   "size from a source change instead. :red[Caveat:] from 2021 the "
                   "unclassified share below climbs, which pushes this line *down*; the "
                   "true rise in touchbacks is steeper than it looks here.")
        tb = q(f"""SELECT season, count(*) ko, 100.0*avg(touchback::int) tb_pct,
                          100.0*avg(returned::int) ret_pct, avg(kickoff_yds) avg_yds
                   FROM play WHERE {kw} AND NOT onside GROUP BY 1 ORDER BY 1""")
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=tb["season"], y=tb["tb_pct"], name="touchback %",
                                 mode="lines+markers", line=dict(color="#8b5cf6", width=3)))
        fig.add_trace(go.Scatter(x=tb["season"], y=tb["ret_pct"], name="returned %",
                                 mode="lines+markers", line=dict(color="#2f9e44", width=2)))
        fig.add_vline(x=2018, line_dash="dash", line_color=OK,
                      annotation_text="2018 fair-catch rule", annotation_position="top left")
        fig.add_vline(x=2021.5, line_dash="dot", line_color="#999",
                      annotation_text="old source boundary", annotation_position="bottom right")
        fig.update_layout(template=PT, height=400, yaxis_title="% of kickoffs",
                          xaxis_title=None, legend=dict(orientation="h", y=1.08, x=0),
                          margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, width="stretch", key="ko_touchback")
    with right:
        st.subheader("Kickoff distance")
        st.caption("A clean bimodal shape — deep kicks aimed at the endzone, plus a shorter "
                   "mode of directional and squib kicks. A single smooth blob would suggest "
                   "the parser is mixing kick distance with something else.")
        d = q(f"SELECT kickoff_yds, season FROM play WHERE {kw} AND NOT onside "
              f"AND kickoff_yds BETWEEN 20 AND 80")
        fig = px.histogram(d, x="kickoff_yds", nbins=60, template=PT, height=400,
                           color_discrete_sequence=["#8b5cf6"])
        fig.update_layout(xaxis_title="kickoff distance (yards)", yaxis_title="kickoffs",
                          margin=dict(l=0, r=0, t=10, b=0), bargap=.02)
        st.plotly_chart(fig, width="stretch", key="ko_distance")

    st.divider()
    st.subheader("Outcome classification coverage")
    st.caption("The touchback rate above is now taken on classified kicks only -- kickoffs "
               "whose text states no outcome carry NULL, not false, so they leave both "
               "halves of the ratio. This bar is how much of each season that removes: "
               "where it is high the rate is still trustworthy, but it rests on a smaller "
               "denominator, and the trend line is thinner evidence than it looks.")
    st.plotly_chart(classification_chart(classification_frame(where, "kickoff"), "kickoff"),
                    width="stretch", key="ko_classified")

    st.divider()
    a, b = st.columns(2)
    with a:
        st.subheader("Returns that were actually returned")
        st.caption("`return_yds = 0` means the ball was not advanced, so the denominator "
                   "here is `returned`, not all kickoffs. Getting this wrong deflates every "
                   "return average by roughly half.")
        r = q(f"""SELECT season, count(*) FILTER (WHERE returned) AS n_returns,
                         avg(return_yds) FILTER (WHERE returned) avg_ret,
                         quantile_cont(return_yds, 0.5) FILTER (WHERE returned) median_ret,
                         max(return_yds) FILTER (WHERE returned) longest,
                         sum(returned_for_td::int) return_tds
                  FROM play WHERE {kw} GROUP BY 1 ORDER BY 1""")
        st.dataframe(r.style.format({"avg_ret": "{:.1f}", "median_ret": "{:.0f}",
                                     "n_returns": "{:,}"}),
                     hide_index=True, width="stretch", height=390)
    with b:
        st.subheader("Rare events")
        st.caption("Ten seasons is enough that onside kicks and blocks stop being anecdotes. "
                   "These counts also double as a smoke test — if a season shows zero of "
                   "something that happens ~85 times a year, the parser lost a format.")
        rare = q(f"""SELECT season,
                            count(*) FILTER (WHERE play_kind='kickoff' AND onside) onside_kicks,
                            count(*) FILTER (WHERE returned_for_td) return_tds,
                            count(*) FILTER (WHERE kick_blocked AND play_kind='punt') punts_blocked,
                            count(*) FILTER (WHERE kick_blocked AND play_kind='field_goal') fgs_blocked,
                            count(*) FILTER (WHERE play_kind='kickoff' AND out_of_bounds) ko_oob
                     FROM play WHERE {where} GROUP BY 1 ORDER BY 1""")
        st.dataframe(rare, hide_index=True, width="stretch", height=390)


# --------------------------------------------------------------------------- tab 5
SAMPLE_QUERIES = {
    "The conference trap (team-only vs team-season join)": textwrap.dedent("""\
        -- Same 2018 punt count, two ways. The wrong one backdates 2024 realignment
        -- across the whole decade and understates the Pac-12 by 85%.
        WITH latest AS (
            SELECT team_id, last(conference_name ORDER BY season) AS conference_name
            FROM dim_team_season GROUP BY team_id)
        SELECT 'team-only (WRONG)' AS join_style, l.conference_name, count(*) AS punts
        FROM play p JOIN latest l ON l.team_id = p.kicking_team_id
        WHERE p.play_kind = 'punt' AND p.season = 2018
          AND l.conference_name IN ('Big Ten Conference', 'Pac-12 Conference')
        GROUP BY 1, 2
        UNION ALL
        SELECT 'team-season (RIGHT)', p.kicking_conference, count(*)
        FROM play p
        WHERE p.play_kind = 'punt' AND p.season = 2018
          AND p.kicking_conference IN ('Big Ten Conference', 'Pac-12 Conference')
        GROUP BY 1, 2
        ORDER BY 2, 1;"""),
    "Longest made field goals": textwrap.dedent("""\
        SELECT season, kicking_team, kicker_known_name, fg_distance_yds, venue_name, play_text
        FROM play
        WHERE play_kind = 'field_goal' AND fg_made AND fg_distance_yds >= 57
        ORDER BY fg_distance_yds DESC, season
        LIMIT 40;"""),
    "Clutch kicks: last 5 minutes, one score": textwrap.dedent("""\
        SELECT count(*) AS att,
               round(100.0 * avg(fg_made::int), 1) AS pct,
               round(avg(fg_distance_yds), 1)      AS avg_dist
        FROM play
        WHERE play_kind = 'field_goal' AND fg_made IS NOT NULL
        GROUP BY is_clutch
        ORDER BY is_clutch;"""),
    "Rows the parser flagged": textwrap.dedent("""\
        SELECT parse_confidence, play_kind, count(*) AS n
        FROM play
        WHERE parse_confidence IS DISTINCT FROM 'exact'
        GROUP BY 1, 2 ORDER BY n DESC;"""),
    "Surface effect, held at equal distance": textwrap.dedent("""\
        SELECT surface,
               count(*) AS att,
               round(avg(fg_distance_yds), 1)          AS avg_dist,
               round(100.0 * avg(fg_made::int), 1)     AS actual,
               round(100.0 * avg(p_hat), 1)            AS expected
        FROM play JOIN fg_exp USING (play_uid)
        WHERE play_kind = 'field_goal' AND fg_made IS NOT NULL AND surface IS NOT NULL
        GROUP BY 1;"""),
}


def tab_sql() -> None:
    st.subheader("SQL scratchpad")
    st.caption("Read-only DuckDB over the snapshot. `play` is the wide fact table; "
               "`fg_exp` and `punt_exp` are the fitted baselines, joinable on `play_uid`. "
               "The sidebar filters do **not** apply here — write your own predicates.")

    pick = st.selectbox("Start from", ["(blank)"] + list(SAMPLE_QUERIES))
    default = SAMPLE_QUERIES.get(pick, "SELECT * FROM play LIMIT 100;")
    sql = st.text_area("Query", value=default, height=240, key=f"sql_{pick}")

    if st.button("Run", type="primary"):
        stripped = sql.strip().rstrip(";")
        # leading -- comments are normal in a scratchpad; look past them for the verb
        first = next((ln for ln in stripped.splitlines()
                      if ln.strip() and not ln.strip().startswith("--")), "")
        if not first.strip().lower().startswith(
                ("select", "with", "describe", "summarize", "pragma")):
            st.error("Read-only: start with SELECT, WITH, DESCRIBE, SUMMARIZE or PRAGMA.")
        else:
            try:
                df = connect().execute(stripped).df()
                st.success(f"{len(df):,} rows")
                st.dataframe(df, width="stretch", height=420)
                st.download_button("Download CSV", df.to_csv(index=False),
                                   "query_result.csv", "text/csv")
            except Exception as exc:                       # surfacing the DuckDB message is the point
                st.error(str(exc))

    st.divider()
    st.subheader("What is in the snapshot")
    tables = q("SELECT table_name, estimated_size FROM duckdb_tables() ORDER BY 1")
    st.dataframe(tables, hide_index=True, width="stretch")
    with st.expander("Columns of `play`"):
        st.dataframe(q("DESCRIBE play")[["column_name", "column_type"]],
                     hide_index=True, width="stretch", height=500)


# --------------------------------------------------------------------------- page
def main() -> None:
    register_baselines()
    where, sel = sidebar()

    st.title("🏈 D-I FBS Special Teams — validation & pre-analysis")
    head = q(f"""SELECT count(*) plays, count(DISTINCT game_id) games,
                        count(DISTINCT season) seasons,
                        100.0*avg((parse_confidence='exact')::int) exact_pct,
                        100.0*avg(fg_made::int) FILTER (WHERE play_kind='field_goal' AND fg_made IS NOT NULL) fg,
                        avg(punt_net_yds) FILTER (WHERE play_kind='punt' AND NOT kick_blocked) net,
                        100.0*avg(touchback::int) FILTER (WHERE play_kind='kickoff' AND NOT onside) tb,
                        100.0*avg(converted::int) FILTER (WHERE play_kind='pat') pat
                 FROM play WHERE {where}""").iloc[0]
    c = st.columns(7)
    kpi(c[0], "Plays", f"{int(head.plays):,}")
    kpi(c[1], "Games", f"{int(head.games):,}")
    kpi(c[2], "Parsed exact", pct(head.exact_pct, 2))
    kpi(c[3], "FG %", pct(head.fg))
    kpi(c[4], "PAT %", pct(head.pat, 2))
    kpi(c[5], "Punt net", f"{head.net:.2f} yd" if pd.notna(head.net) else "—")
    kpi(c[6], "KO touchback", pct(head.tb))

    t1, t2, t3, t4, t5 = st.tabs(
        ["✅ Validation", "🎯 Placekicking", "🦶 Punting", "💥 Kickoffs & returns", "⌨️ SQL"])
    with t1:
        tab_validation(where)
    with t2:
        tab_placekicking(where)
    with t3:
        tab_punting(where)
    with t4:
        tab_kickoffs(where)
    with t5:
        tab_sql()


main()
