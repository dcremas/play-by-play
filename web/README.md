# Play-by-Play · Instance Explorer

A Dash application for reading the snapshot one **play at a time**: "show me the actual
instances, by player and by team, and let me take them apart."

```bash
.venv/bin/python -m web.app          # http://127.0.0.1:8060
```

It reads `data/out/pbp_cfb.duckdb` and `data/out/pbp_nfl.duckdb` **read-only** — one file
per corpus, both attached to an in-memory database — so it
runs alongside any other reader of the snapshot without contending for the file.

## Two choices: which league, then which side

The header carries **two** selectors, and they are independent axes rather than one combined
list of six — every lens works in both leagues.

| League | Kicks | Scrimmage | Games |
|---|---|---|---|
| **College** | 316,397 | 1,510,679 | 10,470 |
| **NFL** | 90,819 | 447,635 | 3,297 |

League comes first because it is the outer scope: changing it changes what every number on
the page counts. It is cheap to implement — `league` is an ordinary column on both facts and
`data.py`'s filter layer is column-driven — with one rule that is *not* optional:

> **`where_from_filters` always emits a league predicate, and `ignore` cannot drop it.** A
> chart that shows "the distribution of the thing you are filtering on" still means *within
> one league*. A query that forgets the predicate silently answers a question about 2.37M
> plays that was asked about 538k.

What was not cheap is everything that was *scoped by lens alone* and now has to be scoped by
both: every option list, the player picker's floor, and the profile routes. See
`web/league.py`, which is `lens.py`'s sibling and holds the per-league scalars that used to
be literals in `lens.py`'s prose — the leaderboard floors and the kicker-id coverage, which
is 98.7% in one league and 99.9% in the other.

**Team and athlete ids behave differently across leagues, and the UI has to respect that.**
Team ids collide — `2` is Auburn *and* the Buffalo Bills — so every picker, every join and
every profile URL is league-qualified (`/team/nfl/2`; the bare `/team/2` still resolves to
college so older links keep working). Athlete ids do *not* collide; they are one id space,
so a kicker who went pro is one person with two league-scoped profile pages that link to
each other. Jason Sanders is `3124679` at New Mexico and at Miami.

Within a league, every play is reachable through one of three **lenses**:

| Lens | Reads | Subject of a row |
|---|---|---|
| **Offense** | `off_play` over `snap.scrimmage` | the team with the ball |
| **Defense** | `def_play` over `snap.scrimmage` | the team facing it |
| **Special teams** | `st_play` over `snap.play` | the kicking team |

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

Switching **league** clears more, and for a sharper reason. The player picker empties because
a college athlete id left selected after a switch to the NFL would *silently match that same
man's professional plays* rather than nothing — the shared id space makes a stale filter look
like a working one. The division chips empty because `FBS` is not a value the NFL column can
hold, and a selection carried across would match nothing and read as an empty corpus rather
than a stale filter. The FBS-vs-FBS switch is hidden outright in the NFL, where it cannot
change the answer.

## Scope inside each lens

**Special teams** covers all 316,397 kick rows. Field goals, punts and kickoffs are on by
default — 244,937 rows — and the fourth chip adds the conversion family: extra points,
two-point tries and the 75 defensive conversions. They are **off by default** because
67,678 of them are extra points from one spot, which puts a spike at one distance in every
distance-based view. Both halves of the original exclusion are recorded in the project
README's "Not built"; the identity half evaporated on 2026-08-31 when conversions started
linking at 98.6%, and the one-distance half is why the chip starts off rather than absent.

**Off by default is the only thing about them that is (2026-09-09).** Everything the other
three phases have, the conversions have:

| | what it is |
|---|---|
| measures | `pat_att`, `pat_made`, `pat_rate`, `pat_blocked`, `two_att`, `two_made`, `two_rate`, `def_conv` in `common.METRICS` / `DERIVED` |
| leaderboards | an XP and two-point block on both the kickers and the teams grid. `Def conv` is on the **team** grid only — all 75 defensive conversions carry a NULL kicker id, so on a player it is zero on every row |
| profile tab | a **Conversions** tab on `/player` and `/team`, over a per-season grid that keeps extra point and two-point apart and splits the two-point tries into pass and rush |
| KPI tiles | `Extra points` and `XP make rate` on a player, `XP / 2pt rate` on a team |
| charts | `Extra point rate by season` on the profile panel and `charts.conversion_by_type` beside it; `phase_detail` draws both rates by season in the explorer |

**The two rates are never pooled.** An extra point converts at 97.4% and a two-point try at
42.6%, so one rate over both moves with how often a team went for two and reads as kicking
form. The trend line is the extra point alone for the same reason: the kicker takes one
after every touchdown, and the two-point try is a coach's decision on a twentieth of the
volume.

**`Blocked` is an outcome on a conversion, as of 2026-09-09.** The view used to collapse
all 531 blocked extra points into `Failed`; it now splits them the way a field goal is
split, which takes the conversion palette to the field goals' validated blue / red / violet
triple. No rate moved — a blocked PAT was already a failure and still is — and the detail
drawer's `Blocked: yes` line, written for a value the view never produced, now fires.

**`Down`, `Dist to go` and `Yards to goal` are NULL on a derived conversion.** They used to
carry the touchdown's, because `emit_pat` copies the scoring play and does not null them:
14,546 extra points read "3rd down". The view nulls them on the 71,330 derived rows and
keeps them on the 130 conversions ESPN emits as their own play. The four columns that are
NULL on *every* conversion — returner, return yards, blocker, returned-for-TD — are no
longer offered under **Add columns** on this chip. The upstream fix is in the project
README's Known limits section 12.

**The player picker is not only kickers once the chip is on.** ESPN tags a two-point
try `patPasser` / `patScorer`, so the athlete on the row is whoever threw or ran it, not
the placekicker. Of the 3,578 names the picker can offer at its 3-kick floor, **254 reach
this lens through a conversion and nothing else** — and they get a profile page like
anyone else, with one Conversions tab and no others. `lens.player_hint()` says so under the
picker.

**An empty profile tab says which kind of empty it is.** The tabs are built from an
athlete's whole career and the panels from the current filter, so any tab can come up empty
— but the conversion chip is the one that starts off, so an unqualified "no conversions
under these filters" would be the default state of every profile page. `common.empty_panel`
distinguishes the chip being off from the entity genuinely having none.

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
| **Player** | `/player/<athlete_id>` | One page per athlete, with a section for each of the four kick phases he actually appears in. Kicking-side only. |
| **Team** | `/team/<team_id>` | Decade roll-up on top, one row per season underneath, then who kicked, then every kick. Kicking-side only. |
| **Experience curve** | `/experience` | Does a kicker improve with years in the job? One row per player-season, numbered by season in the role, read three ways: the naive cohort curve, the within-player paired curve, and the survivorship split that explains the gap between them. Kicking-side only, and the one surface that does **not** read the sidebar. |

The two profile pages are **pinned to the kicks lens** whatever the header is set to
(`common.entity_where`, `common.st_chips`) — they profile placekickers, punters and kickoff
specialists, so reading them through the offense lens would apply scrimmage-only facets to
a view with no such columns. Team rows on the offense and defense lenses therefore do not
navigate, and the hint above the grid says so rather than silently changing the subject.

Filters in the sidebar are **global**: a player page and a team page both honour
them, each ignoring only the facet it *is*.

**Seasons is not in the sidebar (moved 2026-09-29).** It sits in a full-width band at the
top of the main pane, above the page and below the header. It is the filter most questions
here start with and it had the least room to be one: the sidebar track measured 264px,
which cannot carry thirteen year marks, so it carried six two-digit ones and the exact
selection had to be read off a text label because the marks could not say it. The band's
track measures 830px and carries all thirteen as four-digit years, with the readout beside
it rather than standing in for it.

Two things about where it lives. It is in `AppShellMain`, not in `explorer.layout()`,
because it is global like the rest — a control that existed only on `/` would leave the
store holding a season range with nothing on screen saying so on a profile page. And it is
**above** the `dcc.Loading` that wraps the page, not inside it, so a route change does not
blink the control out from under a reader who is dragging it.

Its CSS is the one place in this app where the Mantine part names are
`mantine-RangeSlider-*` rather than `mantine-Slider-*` — RangeSlider is its own component,
not a Slider with two thumbs, and a rule written against the Slider names matches nothing
while looking correct. The narrow-width rule that thins the marks to every other year
counts `markWrapper`, not `markLabel`: each mark renders as `markWrapper > mark +
markLabel`, so every label is the second child of its own wrapper and
`markLabel:nth-child(even)` selects all thirteen rather than six.

**The Roof filter's two sides are not complements, and that is on purpose (added
2026-09-09).** `venue_indoor` is nullable — 438 kick and 2,149 scrimmage rows have no venue
at all, the 14 Hawai'i home games in 2019–2020 that carry no venue in the scoreboard — so
the clause is `venue_indoor` for indoor and `venue_indoor IS FALSE` for outdoor, and rows
with an unknown roof leave the selection under *either* choice. On the default kick
selection that reads 11,033 indoor + 233,584 outdoor against 244,937 total: the 320-row
shortfall is the point, not a bug. The feed does not say those plays were outdoors, and a
filter that kept unknown-roof rows on the outdoor side would be claiming something it
cannot know. It is a `Select` rather than a chip pair for the same reason — two toggles
would imply each is the other's inverse.

It is also a **stadium** property and not a **game** condition: 5 of the 18 indoor venues
are retractable and read indoor whether or not the roof was open that day. The control
carries that caveat inline, because someone filtering to indoor kicks is usually asking a
weather question and this is the one thing that would mislead them. Clicking any play row opens a detail
drawer that leads with **who was involved**. On a kick that is the kicker, returner,
tackler, whoever got a hand on it, plus assisting tacklers from `play_athlete` — each with
the parser's name-match confidence and a link to their profile, and the kicker carrying the
line he had built that season *before* this kick. On a scrimmage play it is the passer,
rusher and receiver off the row plus every role the participant bridge carries. Situation,
environment and the verbatim ESPN `play_text` sit in collapsed sections underneath.

## The experience curve, and why it ignores your filters

`/experience` asks whether a kicker gets better the longer he does the job. It is the only
view here that answers a question about **careers** rather than about a set of plays, and
that difference is why it is a route rather than a fourth grain on the explorer: a career
is not a selection you can filter without changing what "year one" means. Narrow the season
band to 2018–2022 and a man who started in 2016 becomes a rookie. Filter to one team and
every transfer becomes two rookie years. So the panel is always the whole corpus for the
league on screen, `_chrome()` in `app.py` collapses the filter panel and hides the season
band on this route rather than leaving two inert controls on screen, and the page says so
in a note at the top.

**Counts and rates over counts, and nothing else.** No fitted baseline, no significance
test, no index, no smoothing, no chained or cumulative line. Every value is a total over a
total, every chart point carries the number of players behind it, and every rate has its
numerator and denominator in a column of the table below it — a reader can divide 6,774 by
9,170 and get the 73.9% back. The page's job is to put the true numbers in front of
someone in the order that lets them see what is going on. The conclusion is theirs.

That order matters, because the obvious way to answer the question is wrong, and
`web/career.py` is built around the three reasons:

- **The year-two group is not the year-one group.** A kicker who misses in year one does
  not get a year two, so comparing all of year two to all of year one compares the whole
  intake against the survivors of it. No statistics are needed to see it: college year-one
  placekickers who never appeared again made **69.6%**, and the ones who came back made
  **75.6%** — both measured in year one, before either group gained a season. That split is
  its own chart on the page. `paired_steps()` sidesteps it entirely by comparing each man
  with himself a year later, so the one who left is missing from *both* columns.
- **Left censoring.** The corpus starts in 2014, so a player whose first row is a 2014 row
  may have been a fourth-year starter in 2013 — 29.7% of the college year-one cohort.
  Dropped by default; the switch is on the page because it moves the answer.
- **Degree of difficulty.** Attempt distance rises with experience. Rather than adjust for
  it, mean attempt distance is a column beside the make rate in every table.

The three roles give three different answers, which is why the page has a role selector
rather than one curve. College, year 1 → year 2, the same men in both seasons:

| Role, measure | Earlier | Later | Better | Worse | Level |
|---|---|---|---|---|---|
| Placekicker, make rate | 75.6% | 76.2% | 176 | 181 | 11 |
| Punter, net yards | 39.4 | 40.3 | — | — | — |
| Kickoff specialist, touchback rate | 46.1% | 51.8% | 226 | 148 | 0 |

Read as a cohort, college placekickers climb from 73.9% to 77.0% across three years. Read
as the same men, they go 75.6% → 76.2% with 176 better and 181 worse — and over those same
years their mean attempt gets a yard and a half longer, which is in the column next to it.
Kickoff specialists move 226-to-148 in one year, which is a different shape of number
entirely.

**The player table is one row per career, and its columns are read off the survival
curve.** The long form — a row per player-season — is what everything above is added up
from, but it is the wrong shape for the question a reader brings to the bottom of the
page: *did this man get better?* Pivoted, a career is one line read left to right, and
the per-column volume and seasons are in the cell's tooltip so a 100% column can say it
rests on 14 kicks.

College gets one column per season; the NFL gets four bands, and the bands are not evenly
cut. The two corpora have different career shapes and the survival curves say so:

| Share who get another season | yr 1 | yr 2 | yr 3 | yr 4 | yr 5 | yr 6 |
|---|---|---|---|---|---|---|
| College placekickers | 66% | 71% | 56% | **27%** | 0% | — |
| NFL placekickers | **73%** | 87% | 90% | 100% | 77% | 94% |

College has a wall in it — eligibility — and every year up to it is its own event, which
is why every year is its own column. The NFL has no wall: after the rookie cull the
continuation rate is flat at 85–95%, and the measure agrees, with make rate at .826 and
.833 in years one and two before settling at .86–.87. So **year one keeps its own column**
and the rest group as `2–3`, `4–6`, `7+`.

The obvious alternative — even three-year bands — was rejected on the same evidence. It
buries the single structural break in the data inside its first band, on a page whose
entire subject is that the men who leave are not a random sample; and its top band `10+`
holds 3 players and 5 seasons, which is a column of blanks. `7+` is open-ended because
years 7 through 11 hold 15, 9, 7, 3 and 2 players and no honest split of that tail has two
sides. The bands are one literal in `career.YEAR_BANDS`.

**Two display floors, and they are not evidence rules — there are none on this page.**
`MIN_SHOWN = 15` is the group size below which an average is a name rather than a level, so
a thin year is kept out of the charts and kept *in* the tables, where its player count sits
in the column beside it. `MIN_SPLIT = 5` is the smaller floor for the came-back split,
because a 32-team league loses about five kickers a season and fifteen emptied the NFL
panel completely. Both curves **stop** at the first year that fails rather than skipping
it: cohorts shrink monotonically, so the first thin year is the last one worth drawing, and
a line with a hole in it draws a straight segment from year 7 to year 10 and invites a
reader to call it one season's change.

## Why the play grid is server-side

`rowModelType="infinite"`. Sorting and every per-column header filter are translated
to SQL (`data.filter_model_to_sql`, `data.sort_model_to_sql`) and pushed into DuckDB,
so the browser never holds more than a dozen 120-row blocks and header filters
compose with the sidebar rather than fighting it. Column filters are supported for
text, number and date; every ORDER BY is tie-broken on `play_uid` so paging is
stable.

## Taking the grid away: CSV and Excel

Every grid that holds detail carries a **Download** pair on the line above it — the
explorer's three grains, and the play grid on both profile pages. `web/export.py` is the
whole implementation.

**The file is the pane, and that is the hard part.** The plays grid runs on the infinite
row model, so a client-side "export what you can see" would export a *scroll position*.
The export therefore re-runs the query — the sidebar's WHERE clause, the grid's own
header filters, and its sort order, composed exactly as `_rows` composes them. The two
halves the grid knows and the server does not (its `filterModel` and `sortModel`) are
kept in a `dcc.Store` fed by the last `getRowsRequest`, because that is literally what
the grid asked for rather than a guess reconstructed from props. The rolled-up grids
(Teams, Kickers) are client-side, so `virtualRowData` *is* the displayed rows and is used
as given, with a server-side recompute as the fallback for the beat after a mount.

**Columns come from the same `columnDefs` the grid was built with**, so the file has the
pane's columns, in the pane's order, under the pane's headers — including whatever *Add
columns* has switched on. **Values do not**: a rate goes into the cell as
`0.7423728813559322`, not as the string `"74.2%"`, because a rate rounded to text is a
rate nobody can average afterwards. What the grid's `valueFormatter` contributes instead
is a *display* format, mapped from d3 to Excel's own format language and applied as a
column format — so the workbook shows what the pane showed and holds what the warehouse
holds. CSV has no formats at all and gets the full number, which is the right answer for
a file whose purpose is to be read by something else.

The workbook has two sheets. **Data** is frozen at the header row with an autofilter over
it. **About** records the selection: league, lens, grain, every non-idle sidebar facet in
the sidebar's own words, the grid's column filters, the ORDER BY, the row count — and the
generated `WHERE` clause verbatim, the same habit as the drawer's Provenance panel. On a
profile page it also names the profile and flags the facet that page *ignores*, listing it
as ignored rather than omitting it: a reader who left the player picker set would
otherwise have no way to tell whether it had bitten.

**The row cap is real and is in the filename.** `PBP_WEB_EXPORT_MAX` (default 100,000)
bounds a single download; 1.5M rows of thirty columns is a multi-hundred-megabyte CSV
built in the worker's memory, and it is over Excel's own 1,048,576-row sheet limit
besides. A truncated file is named `…-first100000-…` and carries the full selection size
on its About sheet, so nobody can quote a capped count as a total.

**The one deployment consequence.** `xlsxwriter` used to be the pin the box deliberately
did *not* carry. It carries it now — see the note in `deploy/requirements-deploy.txt`.
pandas resolves the engine at call time, so nothing in `web/` imports it at module level
and a box missing it still starts, still serves every page and still downloads CSV; it
fails only on the Excel button. That is a degraded feature rather than a dead app, which
is exactly what makes the pin easy to forget.

## Three honesty decisions worth knowing

**1. `Unknown` is a first-class outcome — and now a fact of the table, not a convention of
this app.** 20,950 kicks state no outcome anywhere in the play text — 9.9% of the punts and
kickoffs in the corpus, and that is the right denominator: the five "how did it end" flags
do not apply to field goals at all, so `returned IS NULL` means "unstated" only within those
two phases.

When this app was first built the warehouse could not express that: the outcome flags had no
NULL state, so a kick whose ending the parser could not read was stored `false` on every one
of them, and this view had to reconstruct the unreadable set by testing for all-false. As of
2026-08-31 `st_parser_cfb.py` writes NULL on the five flags in exactly that case, so
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
The console was removed on 2026-09-09, so carrying them over is now a rebuild rather than
a port; what they measured is recorded in the README's "Not built". Reversible, not
permanent.

`/experience` briefly broke this rule on 2026-09-29 and was rewritten the same day to
respect it. A "make rate over expected" metric had subtracted a fitted logistic of make
probability on distance from every placekicker season. It answered a real question —
attempt distance rises about a yard a season, so a flat make rate over a longer kick is an
improvement the raw rate cannot show — but it answered it with a number the reader could
not reconstruct from anything on the page. **The distance column does the same job
without the model**: mean attempt distance now sits beside the make rate in every table,
the rate holds while the distance climbs, and the reader draws the conclusion. That is
also the better outcome for this app, because the adjusted figure could only ever be
taken on trust and the two raw columns can be added up by hand.

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
- **Conversion outcomes** — the field-goal triple exactly: blue (Converted) ↔ red
  (Failed), violet (Blocked). Not a sequence of its own and not a new validator run: the
  same three hues in the same roles, because a conversion fails the two ways a placekick
  fails. Blue/red only until 2026-09-09, when `Blocked` stopped being folded into `Failed`.
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
  export.py         the Download pair: a grid's rows as CSV, or as a two-sheet workbook
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

Three implementation notes that cost real debugging time:

- **dash-ag-grid bundles only the light `quartz` stylesheet.** Asking for
  `ag-theme-quartz-dark` silently leaves cell text near-black on a dark surface. Both
  modes use the light theme class and drive every colour from CSS variables.
- **Never pass `None` to a Mantine colour prop.** Dash serialises it to `null`,
  `typeof null === "object"`, and Mantine's parser throws and takes the surrounding
  subtree down with it. Omit the prop instead.
- **Plotly's `responsive` does not mean what it sounds like, and `assets/resize.js` is
  what actually keeps a chart the size of its box.** Plotly measures its container when
  it draws and re-measures on a **window resize event** and nothing else. Every way a
  container changes size in this app is some other way, and each left a chart at a
  width that was right when it was drawn:

  | | measured before the fix |
  |---|---|
  | a chart drawn in an inactive tab panel or the collapsed *Shape of the current selection* accordion | host 0, so plotly fell back to its default **700** — opening the Punt tab spilled the chart 231px past a 469px panel |
  | the explorer's chart row re-columning, three columns on one phase and two on several | hosts moved 296 ↔ 452 and both charts stayed at **296.3**: 156px wrong, in one direction as dead space and in the other as overflow |
  | a new figure arriving | `Plotly.react` keeps the existing size, which is why the figure that arrives in the same callback as the re-layout does not rescue it |

  A resize event fixes all three. **Wiring one to each Dash control that can cause them
  does not** — the re-layout case has no control to hang it on, and the next tab set or
  collapsible added to the app would be broken until someone remembered. So the asset
  watches the boxes rather than the controls: one `ResizeObserver` over every
  `.js-plotly-plot` (plotly sizes that div to 100% of its host, so its box tracks the
  host's), calling `Plotly.Plots.resize` when the measured width no longer matches the
  drawn one. A `MutationObserver` picks up graphs as callbacks create them, scanning
  only the added subtrees — never the whole document, because the plays grid mutates
  constantly while it scrolls.

  Two guards are load-bearing: a zero width is skipped, because a hidden panel has
  nothing to measure and comes straight back when it is shown; and a graph already at
  the right size is skipped, which is what stops a resize from feeding itself. Verified
  with no `ResizeObserver loop` warning in the console.

  **Testing this needs the tab in the foreground.** `ResizeObserver` and
  `requestAnimationFrame` callbacks are part of the rendering steps, so in a
  background tab neither fires and every chart measures stale — which looks exactly
  like the bug. A hidden tab reports `document.visibilityState === "hidden"` and zero
  animation frames; check that before believing a measurement.
