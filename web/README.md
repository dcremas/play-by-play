# CFB Play-by-Play · Instance Explorer

A Dash application for reading the snapshot one **play at a time**: "show me the actual
instances, by player and by team, and let me take them apart."

```bash
.venv/bin/python -m web.app          # http://127.0.0.1:8060
```

It reads `data/out/pbp.duckdb` **read-only**, attached to an in-memory database, so it
runs alongside any other reader of the snapshot without contending for the file.

## The first choice is the side

Every one of the corpus's 1,827,076 plays is reachable, through one of three **lenses**:

| Lens | Reads | Rows | Subject of a row |
|---|---|---|---|
| **Offense** | `off_play` over `snap.scrimmage` | 1,510,679 | the team with the ball |
| **Defense** | `def_play` over `snap.scrimmage` | 1,510,679 | the team facing it |
| **Special teams** | `st_play` over `snap.play` | 316,397 | the kicking team |

**Offense and defense are the same rows read from opposite ends, not two copies.** One
row in `snap.scrimmage` is one row in each view: `team_id` is whoever the lens is about,
`opp_id` is the other side, and everything signed flips with the subject — `score_diff` is
the subject's margin, and `points_scored` is +7 to the defense on a pick-six and −7 to the
offense. That is why there is no `defense_play` table upstream: offense and defense are a
*perspective*, and only special teams is a genuinely different fact with different
measures. `web/lens.py` is the one module that knows the difference.

**Exactly one lens is on screen at a time, and that is the design.** A rush row and a punt
row share almost no measured fields; a grid holding both would be mostly empty cells in
both directions. Making the side an exclusive choice means every column set, metric and
chart downstream has one vocabulary to serve, which is what keeps `data.py`'s filter and
sort translation completely lens-blind — it interpolates a view name and never learns what
is in it.

The sidebar is **one static set of controls that relabels itself** rather than three
sidebars. "Team" reads *Offense*, *Defense* or *Kicking team*; the phase chips are rebuilt
per lens; down and field zone appear only on the scrimmage lenses and kick distance only on
the kicks. Every filter that still means something keeps its value when the side changes.
Two do not and are cleared: the player picker (a quarterback is not a tackler) and any
outcome the new vocabulary cannot produce (`Touchback` on offense).

## Scope inside each lens

**Special teams** covers all 316,397 kick rows. Field goals, punts and kickoffs are on by
default — 244,937 rows — and the fourth chip adds the conversion family: extra points,
two-point tries and the 75 defensive conversions. They are **off by default** because
67,678 of them are extra points from one spot, which puts a spike at one distance in every
distance-based view. Both halves of the original exclusion are recorded in the project
README's "Not built"; the identity half evaporated on 2026-08-31 when conversions started
linking at 98.6%, and the one-distance half is why the chip starts off rather than absent.

**Offense** names the passer on a pass or sack and the rusher on a run — 99.9% of those
rows — with receivers as their own column and their own filter. The player filter matches
any of the three roles, so a receiver is as findable as the passer who threw to him.

**Defense** is the honest gap. ESPN's structured tackler field is credited on **41% of
rushes and 28% of passes**, so `First tackler` is a two-thirds-empty column and says so in
its header tooltip. The real defensive vocabulary is in `snap.scrimmage_athlete` — 482k
`tackler`, 389k `assistedBy`, 45k `sackedBy`, 41.5k `passDefender`, 32,316 distinct
defenders — which is a (play, role, athlete) grain rather than a play grain. The detail
drawer already reads it, which is why a sack with no tackler id still names who got there.
A defensive *leaderboard* has to be built on it too, and that is the next pass: joining it
onto the play grid would multiply a play by its defenders.

## The surfaces

| Surface | Route | What it is |
|---|---|---|
| **Explorer** | `/` | One filter set, one lens, two or three grains. **Plays** is up to 1.5M rows on AG Grid's infinite row model. **Teams** is the same selection rolled up. **Kickers & punters** exists on the kicks lens only, because that is the only side with an honest player grain today. |
| **Player** | `/player/<athlete_id>` | One page per athlete, with a section for each phase he actually kicks in. Kicking-side only. |
| **Team** | `/team/<team_id>` | Decade roll-up on top, one row per season underneath, then who kicked, then every kick. Kicking-side only. |

The two profile pages are **pinned to the kicks lens** whatever the header is set to
(`common.entity_where`, `common.st_chips`) — they profile placekickers, punters and kickoff
specialists, so reading them through the offense lens would apply scrimmage-only facets to
a view with no such columns. Team rows on the offense and defense lenses therefore do not
navigate, and the hint above the grid says so rather than silently changing the subject.

Filters in the sidebar are **global**: a player page and a team page both honour
them, each ignoring only the facet it *is*. Clicking any play row opens a detail
drawer that leads with **who was involved**. On a kick that is the kicker, returner,
tackler, whoever got a hand on it, plus assisting tacklers from `play_athlete` — each with
the parser's name-match confidence and a link to their profile, and the kicker carrying the
line he had built that season *before* this kick. On a scrimmage play it is the passer,
rusher and receiver off the row plus every role the participant bridge carries. Situation,
environment and the verbatim ESPN `play_text` sit in collapsed sections underneath.

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
`returned IS NULL` is the single-column test and the derivation here just reads it. It is
worst in 2023–2025 — 29.6% of 2023 punts and 28.0% of its kickoffs — so rates from those
seasons rest on a visibly smaller denominator.

**Where it is surfaced changed on 2026-09-09, at the user's request.** It used to also be a
stat tile on every page and a dedicated chart — "Unreadable-outcome share by season", the
third chart of the multi-phase kicks view. Both are gone: the tile from the explorer, the
team page and the player page, and the chart entirely (`unknown_share_by_phase` is deleted,
not just unwired). What remains is where it belongs — **a value in the Outcome column, a
filterable option in the sidebar, a band in the mix charts, an `Unknown` column in the
by-season grids, and one line of note under the tiles.**

That is a presentation change and not a retreat from the decision above, but the line is
worth naming: the number is no longer *summarised* anywhere, only *shown*. A reader who
never scrolls the grid will not learn that 8.6% of the corpus has no stated outcome unless
they read the note. The note is therefore not optional furniture — it is the only remaining
summary, which is why it survived the cull and why it still carries the count, the share and
the reason. The notes on the team and player pages survived for the same reason: they explain
the `Unknown` column in the grid directly beneath them, which is still there.

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

Two data properties are handled in `data.py` rather than left to each caller, because both
would otherwise produce a confidently wrong number:

- **`statYardage` on a turnover is the defense's return, not the offense's gain.** ESPN
  credits 35 yards to the offense's row of a 35-yard pick-six. Turnovers are held out of
  every mean-yards measure — it lifts the passing mean from 7.5 to 13.2 yards otherwise —
  and the KPI note and the chart title both say so.
- **Four rows carry an impossible `statYardage`**: 11,131 and 561 yards gained, −5,114 and
  1,105 on penalties. They are NULLed, never clamped (a clamped value is indistinguishable
  from a real one), and `yards_impossible` flags them so they light up in a grid instead of
  quietly setting a longest-play record. 100-yard interception returns are real — 45 of
  them — so the plausibility window sits at ±110, above them.

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
- **Scrimmage outcomes** — the kick order above with the red pole appended: blue, orange,
  aqua, yellow, magenta, violet, **red**. Validated as its own sequence on 2026-09-09.
  Light: CVD ΔE 9.1 (protan, yellow/aqua), normal ΔE 19.6. Dark: CVD ΔE 8.4, normal ΔE
  19.3, all ≥ 3:1. The worst adjacent pair is the same yellow/aqua one the kick sequence
  already carries, so it adds no new risk. Seven was the ceiling, and the search that found
  it was exhaustive rather than eyeballed: every ordering that also used green failed, and
  red beside magenta failed the **normal-vision** floor at ΔE 7.8 in dark mode — the first
  arrangement tried, and a reminder that the check catches what the eye does not.
- **The red end is always the pole the subject does not want** — `Turnover` on offense,
  `TD allowed` on defense, `Missed` on a field goal. Offense and defense therefore share
  the same seven hues positionally; they can never appear in one chart, exactly as the kick
  and field-goal sequences never do.
- **`Unknown`, `Negated` and `Unclassified` are muted ink, not hues.** They fail the chroma
  floor deliberately: a data-quality state must read as absence, not as another outcome.

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
  lens.py           the three lenses: which view, which phases, what a row is called
  app.py            shell, the side selector, global filters, routing, the detail drawer
  data.py           read-only DuckDB attach, the three views, filter + AG Grid translation
  columns.py        per-lens, per-phase column sets and the opt-in catalogue
  charts.py         Plotly figures; explorer_figs() dispatches on lens and phase count.
                    Always returns a 3-tuple, but the THIRD MAY BE None -- the multi-phase
                    kicks view has two charts. Callers hide the dead slot and drop their
                    SimpleGrid to two columns, or the survivors keep a third of the width
  detail.py         the single-play drawer, one renderer per fact
  theme.py          validated palettes, Plotly and AG Grid defaults
  ui.py             stat tiles, notes, grid factory
  pages/
    common.py       aggregates and season tables shared by every page
    explorer.py  player.py  team.py
  assets/app.css    surfaces, ink, and all AG Grid colour variables
```

**The explorer opens on the detail, not the summary (2026-09-09).** "Shape of the current
selection" is an accordion that starts **collapsed**, because the plays grid is what the page
is for and three 340px charts above it pushed the first row of detail off most screens. The
section keeps its label and chevron so it reads as collapsed rather than absent, and the grid
grew from 620px to 720px with the space. Two consequences to know:

- **The chart callbacks still fire while the section is shut.** They are cheap against a
  local snapshot, and gating them on the accordion would serve stale figures the moment it
  opened. If they ever stop being cheap, cache them — do not gate them.
- **Stat-tile rows size to their content**, `min(len(tiles), 6)` columns rather than a fixed
  6. The kicks lens has five tiles since the Unknown-outcome one was dropped, and a
  six-column grid left the row stopping short with dead space on the right.

Two implementation notes that cost real debugging time:

- **dash-ag-grid bundles only the light `quartz` stylesheet.** Asking for
  `ag-theme-quartz-dark` silently leaves cell text near-black on a dark surface. Both
  modes use the light theme class and drive every colour from CSS variables.
- **Never pass `None` to a Mantine colour prop.** Dash serialises it to `null`,
  `typeof null === "object"`, and Mantine's parser throws and takes the surrounding
  subtree down with it. Omit the prop instead.
