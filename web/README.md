# Special Teams · Instance Explorer

> **Scope note, 2026-09-08.** The warehouse now holds every play, not just kicks — the
> schema is `pbp` and the snapshot carries a `scrimmage` table of 1.5M rows beside `play`.
> **This app is deliberately unchanged and remains special-teams-only** (PLAN.md §10j.4).
> The interface decision was deferred until the new fact had been queried directly, not
> because it is hard. Two open questions are recorded in the README's "Not built".
>
> The DuckDB view this app creates is named `st`. That is unrelated to the old Postgres
> schema name and was left alone on purpose: it still means special teams, which is exactly
> what this app shows.

A Dash application for reading the snapshot one **kick at a time**: "show me the actual
instances, by player and by team, and let me take them apart."

```bash
.venv/bin/python -m web.app          # http://127.0.0.1:8060
```

It reads `data/out/pbp.duckdb` **read-only**, attached to an in-memory database, so it
runs alongside any other reader of the snapshot without contending for the file.

## Scope

The three kicking phases only — **field goals, punts, kickoffs**: 242,868 of the
snapshot's 313,583 rows. PATs and two-point tries are deliberately excluded.

**Half the reason for that exclusion has since evaporated**, and it is on the list to
revisit. When this app was built, PAT rows carried no `kicker_athlete_id` at all — NULL on
all 58,535 of them — so a kicker page could not have shown a player's extra points even if
it wanted to. That was fixed on 2026-08-31 and conversions now link at 98.6%. What still
holds is the other half: they are ~71k attempts at essentially one distance, so they would
drown every distance-based view in the app.

Profiles are for the **kicking side only** — placekickers, punters and kickoff
specialists. Returners and tacklers are named on every play they appear in, and are
searchable and filterable in the grids, but have no page of their own.

## The three surfaces

| Surface | Route | What it is |
|---|---|---|
| **Explorer** | `/` | One filter set, three grains. **Plays** is 243k rows on AG Grid's infinite row model. **Kickers & punters** and **Teams** are the same selection rolled up; clicking a row opens that entity. |
| **Player** | `/player/<athlete_id>` | One page per athlete, with a section for each phase he actually kicks in. |
| **Team** | `/team/<team_id>` | Decade roll-up on top, one row per season underneath, then who kicked, then every kick. |

Filters in the sidebar are **global**: a player page and a team page both honour
them, each ignoring only the facet it *is*. Clicking any play row opens a detail
drawer that leads with **who was involved** — kicker, returner, tackler, whoever got
a hand on it, plus assisting tacklers from `play_athlete` — each with the parser's
name-match confidence and a link to their profile, and the kicker carrying the line
he had built that season *before* this kick. Situation, environment and the verbatim
ESPN `play_text` sit in collapsed sections underneath.

## Why the play grid is server-side

`rowModelType="infinite"`. Sorting and every per-column header filter are translated
to SQL (`data.filter_model_to_sql`, `data.sort_model_to_sql`) and pushed into DuckDB,
so the browser never holds more than a dozen 120-row blocks and header filters
compose with the sidebar rather than fighting it. Column filters are supported for
text, number and date; every ORDER BY is tie-broken on `play_uid` so paging is
stable.

## Three honesty decisions worth knowing

**1. `Unknown` is a first-class outcome — and now a fact of the table, not a convention of
this app.** 20,950 kicks state no outcome anywhere in the play text — 9.9% of the punts and
kickoffs in the corpus, and that is the right denominator: the five "how did it end" flags
do not apply to field goals at all, so `returned IS NULL` means "unstated" only within those
two phases.

When this app was first built the warehouse could not express that: the outcome flags had no
NULL state, so a kick whose ending the parser could not read was stored `false` on every one
of them, and this view had to reconstruct the unreadable set by testing for all-false. As of
2026-08-31 `st_parser.py` writes NULL on the five flags in exactly that case, so
`returned IS NULL` is the single-column test and the derivation here just reads it. Unknown
is a visible slice in every chart, a filterable value in every grid, and a stat tile on every
page. It is worst in 2023–2025 — 29.6% of 2023 punts and 28.0% of its kickoffs — so rates
from those seasons rest on a visibly smaller denominator.

**2. `Onside` is tested before the real outcomes.** An onside kick is a different play, not
a kickoff with an unusual result. Folding it in both muddied touchback rates and dumped
1,382 of the 1,486 onside kicks into `Unknown`. Onside kicks keep `false` (not NULL) flags,
so they never collide with the Unknown test.

**3. Mangled kicker names are flagged rather than cleaned — and the guard is still working.**
1,012 field goals, 955 of them in 2025, used to carry the gamebook clock and jersey prefix
inside the name (`(09:34) #98 I.Hankins`) because `parse_field_goal` never stripped the
clock that the punt and kickoff parsers had stripped since the dialect was added. This app
exposed them as `player_name_unparsed` instead of stripping the prefix for display, on the
grounds that a cosmetic repair would hide a real parser bug. That turned out to be the right
call: **the bug was fixed upstream on 2026-08-31** and all 1,012 affected rows now link to a
real athlete id.

The column stays as a standing regression guard, and exactly one row lights up today — from
a different cause. `(Fake Punt) Michael Burton run for 2 yds` (2014) is a fake punt where
the leading parenthetical was captured as the punter. One row is what a working guard looks
like: it is showing a real, tiny parse defect rather than sitting at a decorative zero.

**Athlete linking is no longer the 2025 problem it was.** When this app was built, 2025
kicks linked to an athlete id only 58% of the time, and the sidebar's "only kicks linked to
an athlete id" switch was a blunt instrument for a big gap. The cause was a join key that
diverged between two ESPN endpoints from week 9 of 2025; it is fixed, and coverage across
the three phases is now **98.73%** overall and **98.82%** for 2025, with no season below
97.5%. See the main README.

One denominator worth stating, because the switch invites the wrong one: returner coverage
must be measured on kicks that were actually **returned** (98.2%), not on all kicks (32%).
Most kicks are not returned.


## A season still being played

2026 is in the corpus. It arrives through `scripts/update_season.py`, and because this app
takes its season range from `min/max(season)` it appears with no change needed — which is
exactly the problem: a team's 2026 row is a game or two sitting next to twelve full seasons
(the season is 8 games old as of the current snapshot), and nothing on the row says so.

`build_snapshot.py` writes a `season_status` table marking any season whose most recent game
kicked off inside the last 30 days. `data.in_progress_seasons()` reads it, the header carries
a **`2026 partial`** badge whose tooltip gives games, kicks and week, and the by-season grids
on team pages carry a note above them. Nothing is filtered out — this app fits no models, so
a part-season is a *reading* problem here, not a correctness one. The correctness half only
ever mattered to something fitted, and nothing in the repository fits a model any more.

The rule needs nothing unset in January. A month after the last bowl, 2026 stops being in
progress on its own.

## No models

Descriptive only, by design. Two fitted baselines (`fg_exp.p_hat`, `punt_exp.exp_net`)
once lived in the Streamlit console and were deliberately *not* carried over here — every
number on these pages traces directly to a column, with nothing to calibrate or defend.
The console was removed on 2026-09-09, so carrying them over is now a rebuild rather than a
port; what they measured is recorded in the README's "Not built". Reversible, not
permanent.

## Colour

`web/theme.py` holds the palette. Every categorical sequence in the app was run
through the data-viz validator against the surface it actually renders on, in both
modes:

- **Kick outcomes** — blue, orange, aqua, yellow, magenta, violet. Light: CVD ΔE 9.1,
  normal ΔE 19.6. Dark: CVD ΔE 8.4, normal ΔE 19.3.
- **Field goal outcomes** — the documented **diverging** pair, because made/missed is
  a polarity rather than an identity: blue (Made) ↔ red (Missed), violet (Blocked).
  Light CVD ΔE 21.6, dark 19.2. Green/red was tried first and rejected — it landed in
  the 6–8 CVD warn band, which is exactly the wrong place for made-vs-missed.
- **`Unknown` is muted ink, not a hue.** It fails the chroma floor deliberately: a
  data-quality state must read as absence, not as another outcome.

**The stack order is the CVD-safety mechanism, not cosmetics.** Re-run the validator
before reordering anything. Two consequences are load-bearing: field-goal and kick
outcomes are never stacked together (pooling them put ten categories on one bar with
Made and Touchback both blue — so selecting more than one phase switches the charts
to comparing *phases*, the only axis the three share), and every 100% stacked bar
carries its denominator above it, because a 67/33 split on three attempts otherwise
reads as confidently as one on three thousand.

## Layout

```
web/
  app.py            shell, global filters, routing, the detail drawer
  data.py           read-only DuckDB attach, the `st` view, filter + AG Grid translation
  columns.py        per-phase column sets and the opt-in catalogue
  charts.py         Plotly figures; explorer_figs() dispatches on phase count
  detail.py         the single-play drawer
  theme.py          validated palettes, Plotly and AG Grid defaults
  ui.py             stat tiles, notes, grid factory
  pages/
    common.py       aggregates and season tables shared by every page
    explorer.py  player.py  team.py
  assets/app.css    surfaces, ink, and all AG Grid colour variables
```

Two implementation notes that cost real debugging time:

- **dash-ag-grid bundles only the light `quartz` stylesheet.** Asking for
  `ag-theme-quartz-dark` silently leaves cell text near-black on a dark surface. Both
  modes use the light theme class and drive every colour from CSS variables.
- **Never pass `None` to a Mantine colour prop.** Dash serialises it to `null`,
  `typeof null === "object"`, and Mantine's parser throws and takes the surrounding
  subtree down with it. Omit the prop instead.
