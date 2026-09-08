# D-I FBS Special Teams

Every placekick, punt and kickoff in FBS college football from 2014 to the game played
last weekend — one row per instance, with the situation it happened in, the venue, the
people identified by stable id rather than name string, and the raw source text kept
alongside so any row can be audited back to what the feed actually said.

**313,583 rows** over **10,210 games** and **13 seasons**. Built from ESPN play-by-play and
nothing else. Stored in local Postgres, read through a DuckDB snapshot by three
applications.

This file is the whole project. It is written to be read cold, after time away, and it is
complete on its own — `PLAN.md` (the original design record, with the parser's
answer-key agreement tables and the unbuilt scrimmage-play design) and `web/README.md`
(the explorer's implementation notes) are kept, but nothing here depends on them.

| | |
|---|---|
| **Grain** | one row per kick attempt: `kickoff` \| `punt` \| `field_goal` \| `pat` \| `two_point` |
| **Window** | 2014–2026. 2026 is in progress and flagged as such |
| **Source** | ESPN site API (play text, venue) + ESPN core API (per-play athlete ids). No API key, no other feed |
| **Warehouse** | PostgreSQL 18.6, database `cfb`, schema `st`, 8 tables, 235 MB |
| **Read path** | `data/out/pbp.duckdb` — one wide `play` table, 44 MB, rebuilt from Postgres in one command |
| **Parse quality** | 98.51% of rows `exact`; 100% of kicks match a known text format |
| **People** | 98.7% of plays carry an ESPN athlete id for the kicker; 675,856-row play × role × athlete bridge |
| **Apps** | Streamlit validation console · Dash instance explorer · Excel reports · one-page ERD |
| **Figures below** | measured from the snapshot built **2026-09-01 11:41**. Re-derive any of them with the queries in [Query recipes](#query-recipes) |

Does it behave like football? These come out of the data, not out of a reference book:

| | corpus | 2014 | 2018 | 2025 | reality check |
|---|---|---|---|---|---|
| Field goal % | 74.3 | 71.8 | 72.7 | 76.4 | rises across the decade, as PAT% does |
| Punt gross (yd) | 42.04 | 41.30 | 41.52 | 43.19 | FBS is ~41–43 |
| Punt net (yd) | 39.76 | 38.93 | 39.29 | 39.69 | |
| Kickoff touchback % | 50.9 | 38.2 | **51.5** | 59.6 | steps in 2018, the year the fair-catch rule landed |
| PAT % | 97.34 | **98.49** | 96.61 | 98.54 | 2014 is a source defect, not a trend — see [Known limits](#known-limits) |
| FG % by distance | 93.6 (20–24) → 48.5 (50–54) → 36.6 (60+) | | | | monotonic decline over 30k attempts |

The 2018 touchback step and the monotonic distance curve are the two checks that matter
most, because neither was targeted: a distance field that was silently wrong would not
produce that curve, and a touchback flag reading the wrong thing would not step on the
exact season the rule changed.

---

## Contents

- [Orientation](#orientation) — what exists, where it lives, how to start it
- [What a row is](#what-a-row-is) — grain, corpus shape, what is in and out of scope
- [Where the data comes from](#where-the-data-comes-from) — the single source, the four dialects, the parser
- [Identity](#identity) — athlete ids, derived names, conference realignment
- [Data dictionary](#data-dictionary) — every column of every table
- [Known limits](#known-limits) — read this before quoting a number
- [The pipeline](#the-pipeline) — the DAG, what owns what
- [Runbooks](#runbooks) — the five procedures, and which loader is right when
- [The applications](#the-applications) — console, explorer, reports, ERD
- [Query recipes](#query-recipes)
- [Changelog](#changelog) — every material fix, dated
- [Not built](#not-built) — what was decided against or deferred, and what it would take
- [Conventions](#conventions) — for whoever edits this next

---

## Orientation

### The map

```
cfb-pbp/
├── PLAN.md            design record: source recon, parser answer-key proof, phase log,
│                      §10 scrimmage-play design (unbuilt)
├── README.md          this file
├── requirements.txt   pinned; Python 3.14 required (see below)
│
├── scripts/           the pipeline, in dependency order
│   ├── fetch_espn.py          scoreboard + game summaries  -> data/espn/
│   ├── fetch_participants.py  per-play athlete ids         -> data/espn/participants/
│   ├── st_parser.py           the play-text parser. 495 lines, four dialects, no deps
│   ├── build_table.py         ESPN JSON + parser           -> data/out/st_plays.csv
│   ├── build_dims.py          conf | venue | athlete       -> the dimension CSVs
│   ├── build_snapshot.py      Postgres                     -> data/out/pbp.duckdb
│   ├── update_season.py       the weekly in-season driver; calls all of the above
│   └── build_erd.py           live Postgres                -> reports/cfb_st_erd.pdf
│
├── sql/               DDL and loaders. Numbered files run in order
│   ├── schema.sql  dims.sql  enrich.sql          DDL
│   ├── load_1_stage.sql  load_2_insert.sql       backfill loader (TRUNCATE + INSERT)
│   ├── load_3_season.sql                         in-season loader (one season, atomic)
│   ├── reparse.sql                               re-derive parser columns in place
│   ├── load_dims.sql                             the five dimension CSVs, FK order
│   ├── load_athletes_{1_stage,2_apply,3_season}.sql   athlete link: backfill / in-season
│   ├── enrich_game_context.sql                   the three columns load_2 leaves NULL
│   └── verify.sql  verify_phase4.sql             assertion suites
│
├── app.py             Streamlit validation & pre-analysis console (5 tabs, 1,189 lines)
├── web/               Dash instance explorer (separate app, same snapshot)
├── reports/           formatted Excel workbooks + the ERD output
│
└── data/              394 MB, all of it re-derivable from ESPN
    ├── espn/
    │   ├── games_<season>.json          13 files, the completed-game lists
    │   ├── summaries/<game_id>.json.gz  10,379 files, 180 MB — play text lives here
    │   ├── participants/<game_id>.json.gz  10,379 files, 41 MB — athlete ids
    │   └── participants_pre20260831/    454 files kept from before the key fix
    └── out/
        ├── st_plays.csv                 80 MB, the frozen full extract
        ├── st_plays_2026.csv            the current in-progress season
        ├── dim_*.csv  fact_game.csv  play_athlete*.csv
        ├── pbp.duckdb                    44 MB — what every app reads
        └── fg_by_distance.xlsx
```

### First run

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # first time only
.venv/bin/python scripts/build_snapshot.py                          # Postgres -> DuckDB
.venv/bin/streamlit run app.py                                      # the console
.venv/bin/python -m web.app                                         # the explorer, :8060
```

**Python 3.14 is required, and not by choice.** The pyenv 3.12 and 3.13 builds on this
machine were compiled without `blake2`, which breaks `hashlib` and therefore pip. The venv
runs 3.14.7.

Nothing in either app talks to Postgres at runtime. They open `data/out/pbp.duckdb`
read-only, so both start cold in about a second, run side by side without contending for
the file, and keep working when the database is down. The console's sidebar and the
explorer's header both show how old the snapshot is.

### State of play

| | |
|---|---|
| **Built and trusted** | fetch → parse → load → enrich → snapshot; all three applications; the weekly in-season update; the ERD |
| **In progress right now** | the 2026 season, 8 games deep. `scripts/update_season.py 2026` pulls it forward |
| **Rollback tables in Postgres** | none. The database holds exactly the 8 tables the pipeline needs |
| **Deferred by decision** | weather (Phase 5 — tabled, everything needed to start is in place), scrimmage plays (designed in `PLAN.md` §10, not built) |
| **Open follow-ups** | reconsider PATs for the explorer now that they link at 98.8%; decide whether the explorer carries the console's two fitted baselines. Both are in [Not built](#not-built) |

---

## What a row is

One row per **kick attempt**, in one table. Not three tables — the situational context
(down, distance, field position, score, clock, venue) is identical across all three kick
types, and one table makes "kicks versus punts in the same conditions" a `WHERE` rather
than a `UNION`. Type-specific columns are nullable by kind; 313k rows means there is no
performance argument for splitting.

`play_kind` takes five values:

| `play_kind` | rows | share | notes |
|---|---|---|---|
| `kickoff` | 113,396 | 36.2% | includes 1,486 onside kicks, flagged separately |
| `punt` | 98,263 | 31.3% | includes 917 blocked |
| `field_goal` | 31,209 | 10.0% | includes blocked (865), missed and penalty-negated (`fg_made IS NULL`) attempts |
| `pat` | 67,070 | 21.4% | extra points, **derived from touchdown text** — see below |
| `two_point` | 3,645 | 1.2% | `two_point_type` is `pass` or `rush` where the text says |

**Conversions are second-class rows by construction, and it matters.** ESPN does not emit
an extra point as its own play: it folds it into the touchdown text
(`"Derrick Henry 37 Yd Run (Adam Griffith Kick)"`). `build_table.emit_pat` lifts it out and
writes a *second* row off the scoring play with `:pat` appended to the `play_uid`. So one
ESPN play can feed two fact rows — a kickoff-return touchdown produces both a `kickoff`
row and a `pat` row — and anything that joins the participants feed has to route roles to
the right one. `build_dims.stage_athlete` does exactly that; it is the reason conversions
link at all.

### Corpus shape

| season | kickoff | punt | FG | PAT | 2-pt | total | games | outcome unstated |
|---|---|---|---|---|---|---|---|---|
| 2014 | 9,533 | 8,858 | 2,667 | 5,759 | 158 | 26,975 | 851 | 553 |
| 2015 | 9,850 | 8,995 | 2,707 | 5,962 | 256 | 27,770 | 863 | 668 |
| 2016 | 9,748 | 8,835 | 2,553 | 5,976 | 276 | 27,388 | 856 | 790 |
| 2017 | 9,623 | 8,918 | 2,643 | 5,855 | 248 | 27,287 | 869 | 742 |
| 2018 | 9,917 | 8,786 | 2,591 | 6,078 | 303 | 27,675 | 881 | 1,292 |
| 2019 | 9,845 | 8,554 | 2,698 | 5,902 | 287 | 27,286 | 887 | 1,466 |
| 2020 | 6,316 | 5,074 | 1,719 | 3,832 | 261 | 17,202 | 563 | 832 |
| 2021 | 9,455 | 7,858 | 2,577 | 5,480 | 282 | 25,652 | 839 | 2,310 |
| 2022 | 9,416 | 8,301 | 2,576 | 5,420 | 358 | 26,071 | 857 | 2,219 |
| 2023 | 9,696 | 8,110 | 2,738 | 5,594 | 392 | 26,530 | 903 | **5,111** |
| 2024 | 9,843 | 7,795 | 2,821 | 5,545 | 447 | 26,451 | 901 | 2,499 |
| 2025 | 10,073 | 8,102 | 2,898 | 5,625 | 374 | 27,072 | 932 | 2,455 |
| 2026 | 81 | 77 | 21 | 42 | 3 | 224 | 8 | 13 |

2020 is a COVID-shortened season — 563 games against ~880 either side. It is flagged
nowhere and deleted nowhere; any per-season rate that treats it as a normal year is
comparing a two-thirds season to full ones.

### Scope decisions, made once and still standing

- **Every game with at least one FBS team is ingested**, FBS-vs-FCS included, and filtered
  at query time rather than at ingest. 274,629 rows are FBS-vs-FBS; 38,954 are not. The
  snapshot carries `fbs_vs_fbs` precomputed, and the console defaults to FBS-only, because
  kicker-quality work almost always wants that cut.
- **Extra points and two-point tries are in the table.** They are placekicks, they are
  70,715 rows, and they are cheap to carry. The Dash explorer excludes them from its own
  scope; the table does not.
- **169 of 10,379 games have no special-teams play at all** (1.6%), because ESPN carries no
  play-by-play for them. Mostly FBS-vs-FCS. They are in `fact_game` and absent from the
  fact table, which is why `count(DISTINCT game_id)` on plays is 10,210, not 10,379.
- **Nothing is deleted for being ugly.** Values the feed states impossibly get their
  distance NULLed and the row marked `ambiguous`; `play_text` is always retained.

---

## Where the data comes from

### One source, and why

ESPN is the single spine for every season. Two endpoints, neither documented, neither
requiring a key:

| endpoint | gives | used for |
|---|---|---|
| `site.api.../scoreboard?dates=<season>&seasontype=&week=&groups=80` | the completed-game list per week | `games_<season>.json`, `fact_game`, `dim_team` |
| `site.api.../summary?event=<game_id>` | drives → plays: type id, start/end yard line, `statYardage`, `wallclock`, plus `gameInfo.venue` (name, city, state, zip, **`grass` boolean**) and attendance | play text, `dim_venue` |
| `sports.core.api.../events/<id>/competitions/<id>/plays` | `participants[]` with an athlete `$ref` per role | every athlete id in the warehouse |
| `sports.core.api.../seasons/<y>/types/2/groups/{80,81}/children` | conference membership by season | `dim_team_season`, `dim_conference` |

Three things about ESPN that will bite anyone who edits the fetchers:

1. **ESPN 403s browser-like User-Agent strings on these endpoints and serves urllib's
   default.** Counterintuitive, and do not "fix" it by adding a realistic UA.
2. **The API is unofficial.** Rate limits are respected by keeping concurrency at 6 with
   exponential backoff, and every stage is resumable — already-downloaded files are
   skipped unless a refresh flag says otherwise.
3. **Summaries are slimmed on the way to disk.** A full summary is ~1 MB; `fetch_espn.py`
   keeps only `drives`, `gameInfo` and a trimmed `header`. 180 MB for 10,379 games instead
   of 10 GB.

**A bulk pre-parsed archive was used first and then removed from the pipeline entirely.**
It looked like a shortcut for 2016–2021 and was in fact an *incomplete extract of ESPN*:
missing 78 whole games, ~6% of plays inside the games it did have, and 10–22% of kickoffs.
Touchbacks are the cheapest plays for an extract to drop, so every pre-2022 kickoff rate
came out depressed, and the artifact presented itself as a rule change — a fake
34.7% → 50.3% touchback step at exactly the 2021/2022 source boundary. Refetching the same
game ids from ESPN returned more plays every time. On the ESPN spine the step becomes
50.9% → 50.3%, i.e. nothing, while the *real* 2018 rule change survives. The archive also
typed `start.team.id` as `double` in 2016 and 2018 and `int64` elsewhere, which made a
string comparison silently set receiving team = kicking team on 24,676 rows.

The lesson is written into the checks: `kicking team = receiving team` and
`null kicking team` are both assertions in `app.py` with a tolerance of zero, and the
console's season-continuity chart still draws a marker at the old boundary so a
source-shaped discontinuity would be visible if one ever reappeared.

**A CFBD API key was planned for 2022–2025 and never needed.** The archive turned out to
*be* parsed ESPN data, so running `st_parser.py` against live ESPN text returned `exact` on
every play with no changes. One dialect family, one source, all thirteen seasons.

### The team convention

**`start.team.id` is always the KICKING team.** Verified across 105k plays with zero
exceptions: on punts and field goals it equals the offense; on kickoffs it equals the
defense. Do not "simplify" this to a possession field.

That holds for the three kicks, which are read straight off the play. It does **not** hold
for conversions, which are derived from the touchdown play — and neither direction is
right: on an ordinary touchdown the scorer is the offense that already had the ball, on a
pick-six or a punt return it is the other side, and ESPN types both as `Punt` or `Sack`
often enough that a type whitelist cannot separate them. `emit_pat` therefore reads the
scorer **off the scoreboard**: `homeScore` / `awayScore` are the score *after* the play, so
whichever side's points went up is the side lining up to convert. A small residue — 193 of
the 58,542 conversions in the corpus when this was fixed — falls back to `start.team.id`
because ESPN's own scoreboard does not move on the play; those rows keep whatever it says.

### The four dialects, and the parser

`scripts/st_parser.py` is the heart of the project: 495 lines, regex only, no dependencies,
grounded in skeleton analysis of 833,544 plays. ESPN's play text is not one format — it is
four, arriving at different times in the corpus:

| dialect | example | notes |
|---|---|---|
| **ESPN** | `Chris Callahan 33 yd FG GOOD` · `Dom Dzioban punt for 36 yds` | the base form, all seasons |
| **NCAA official** | `A. MacGinnis field goal attempt from 44 GOOD, clock 12:28, PENALTY ...` | mainly on penalty-flagged plays |
| **Gamebook** | `(07:53) #33 C.Brown punt 48 yards to the USU37 #7 K.Davis return 1 yard to the USU38 (#84 N.Elksnis)` | ESPN began concatenating this in from 2021; dominant by 2025 |
| **Gamebook conversions** | `#15 B.McAlister kick attempt good (H: ..., LS: ...)` | from 2025 the conversion sits *outside* any parenthetical. 1,949 plays in 2025 against 14 in all of 2024 |

The gamebook dialect broke four regexes at once, and each break is now a named pattern: a
leading `(MM:SS)` clock (which also landed inside `kicker_name`), `#NN` jersey prefixes on
every name, `Last,First` and `F.Last` renderings, and outcome clauses written without the
connecting words the older patterns required — `return 9 yards` not `returns for 9 yds`,
`fair catch by X at UNT06` not `at the UNT06`.

**Four rules in the parser were each chosen against a plausible wrong reading.** They are
the difference between a table that looks right and one that is right:

1. **A touchback is not a return.** The feed records resulting field position — the 25 on a
   kickoff, the 20 on a punt — and reading that as return yardage would credit a phantom
   return to every one of the corpus's 57,532 touchbacks. It accounted for 15,918 of the
   disagreements against the answer key, more than all other causes combined.
   `return_yds` is NULL and the `touchback` flag carries the outcome.
2. **The trailing yard line is not the return distance.** `returns for no gain to the
   IowSt 19` is 0 yards, not 19. ~2,400 plays.
3. **A return loss is negative.** `a loss of 34 yards` is stored as −34, not +34. ~1,500
   plays.
4. **Two number traps lead with return yardage, not kick distance.** `72 Yd Return of
   Blocked Field Goal` and `55 Yd Punt Return` are the scoring-summary form; without an
   ordered trap pattern tested *first*, the generic pattern reads the return as the kick and
   files the returner as the kicker. That last bug did happen — 170 kickoffs had the
   returner recorded as the kicker before `_KO_RET_TRAP` was added.

Every parse carries a confidence:

| `parse_confidence` | rows | meaning |
|---|---|---|
| `exact` | 308,895 (98.51%) | matched a known format, key fields recovered |
| `ambiguous` | 3,116 | structure recognised, values contradictory — feed self-conflict (text says out-of-bounds *and* lists a return), or a value the feed states impossibly |
| `partial` | 1,508 | matched, but a key field is genuinely absent from the text |
| `none` | 64 | no pattern matched. 45 punts, 15 kickoffs, 4 field goals. Most are rows where `play_text` is NULL in the source |

`play_text` is retained on every row forever. That is what makes the parser half of the
pipeline **re-derivable without re-fetching** — see [Re-derive after a parser
change](#b-re-derive-after-a-parser-change). It is not a nice-to-have; it is the reason a
parser bug found in 2026 could be fixed across twelve seasons in about five minutes of
`UPDATE`.

**How the parser was proved.** Not by inspection. It was diffed field-by-field against the
bulk archive's own parsed columns as an answer key over ~105k plays, and every one of
20,904 disagreements was run down and classified. The worst field came out at 99.76%
agreement, and the gap between raw and net agreement *was the finding*: 15,918
disagreements were the key recording a touchback as a 25-yard return, 2,362 were the key
reading a trailing yard line as return yardage, 1,567 were the key storing return losses as
positive. Full tables are in `PLAN.md` §Phase 2 / §Phase 2b.

---

## Identity

### Athlete ids, not names

Name collisions across thirteen seasons and 130+ teams are guaranteed — five distinct
athletes came out as "Bennett Moehring" on an early build. Every person on a play is keyed
by **ESPN athlete id**, taken from the core API's `participants[]`.

Athlete ids begin in **2014**: 2013 and earlier return `participants[]` with no athlete
`$ref` at all (0% of plays), 2014 onward ~91%. That is why the corpus starts in 2014 and
not earlier.

Coverage today, on the right denominators:

| | coverage |
|---|---|
| `kicker_athlete_id`, all plays | **98.7%** |
| — kickoffs / PATs / field goals / punts / two-point | 99.2 / 98.8 / 98.6 / 98.2 / 94.3% |
| — worst season (2021) | 97.5% |
| `returner_athlete_id`, **on kicks that were actually returned** | **98.2%** overall; 99%+ in eight of thirteen seasons, 95%+ in twelve |
| — worst season (2023) | 85.7% — see [Known limits](#known-limits) |
| `tackler_athlete_id` | 10.9% of all plays. Low because most kicks are not tackled, and because the wide column keeps only the first tackler; the bridge has the rest |

Unconditional returner coverage looks like 32% and means nothing — most kicks are not
returned. Always measure returner coverage on `WHERE returned`.

**There is no `blocker` role in ESPN.** Blocker identity is a parsed name string
(`blocker_name`, 0.5% of rows) and cannot be resolved to an id.

`pbp.play_athlete` is the full-fidelity bridge — one row per (play, role, athlete), 675,856
rows — and it is the truth. The three id columns on the fact table are a denormalised
convenience for the hot path. A play has many tacklers; only the bridge knows them all.

| role | bridge rows | | role | bridge rows |
|---|---|---|---|---|
| `kicker` | 143,725 | | `passer` | 34,089 |
| `returner` | 103,119 | | `rusher` | 33,938 |
| `punter` | 97,147 | | `receiver` | 32,308 |
| `scorer` | 94,136 | | `assistedBy` | 21,133 |
| `patScorer` | 70,000 | | `penalized` | 4,222 |
| `tackler` | 34,473 | | `patPasser`, `recoverer`, `passDefender`, `forcedBy`, `sackedBy`, `fumbler` | 2,631 → 75 |

The offensive roles are there because a conversion, a return touchdown and a fake all put
non-kickers on a special-teams play.

### Names are derived, and they carry a confidence

`dim_athlete.known_name` does not come from a roster feed. There isn't one in the
participants payload. Names come from the play text the parser already extracted, keyed on
the authoritative athlete id — which means a player tagged in a kick role on a play whose
text names someone *else* inherits the wrong name. Harmless for a kicker with 242 plays,
dominant for one with 2.

Two rules make it survivable:

1. **A modal vote with a floor.** The winning name must be seen at least twice and hold
   ≥60% of that athlete's observed names, or `known_name` stays NULL. `name_confidence`
   stores the share so it can be filtered harder. When it was introduced it cut names shared
   by more than one athlete from 228 to 71; on today's larger corpus it stands at **109** —
   and the survivors are genuinely different players who share a name, which is the entire
   reason athlete ids exist.
2. **The vote runs over `(first initial, surname)`, not the raw string.** ESPN changed how
   it writes names mid-corpus: 2025 play text is 40.2% abbreviated (`T.Ahmetbasic`), 2026 is
   **97.7%**. Voting on the raw string made a returning player split his own vote between
   two spellings of his own name, fall under the floor, and end up with no name at all —
   1,281 athletes active into 2025/26 were holding a full-form name for 2026 to dilute.
   `build_dims.name_key()` collapses to initial + surname (suffixes stripped, `J.T. Smith`
   and `JT Smith` alike) so both spellings back one candidate, then displays the winning key
   in its most informative observed spelling. Keys are counted per `athlete_id`, so two
   different players sharing an initial and a surname never collide.

Applied to the whole corpus that change **recovered 209 names, lost none**, and respelled
123 into a fuller form. Some were not marginal: `Dillon Curtis` at 177 plays and
`Beckham Sunderland` at 82 had no name at all before it.

Name coverage is therefore uneven *by design*, and the pattern is exactly what you would
predict from where names appear in text:

| `primary_role` | athletes | named | |
|---|---|---|---|
| `punter` | 1,345 | 95.1% | the punter is named in nearly every punt |
| `kicker` | 2,258 | 84.3% | |
| `returner` | 5,320 | 76.7% | |
| `tackler` | 8,978 | **2.4%** | tacklers are usually a parenthetical jersey number, or absent |
| all 33,194 | | 25.9% | dominated by tacklers and one-play athletes |

If you are grouping by person, group by `athlete_id` and treat `known_name` as a label.
Never the reverse.

### Conference realignment is handled, and it is the single most likely way to get a wrong answer

Conference is a **team-season** attribute, never a team attribute. Between 2014 and 2026 the
Pac-12 went 12 teams → 2 → 8, the Big Ten 14 → 18, the Big 12 10 → 16; **83 of 275 teams
changed conference at least once**.

The same 2018 punt count, grouped two ways:

| 2018 punts | team-only join (**wrong**) | team-season join (**right**) | error |
|---|---|---|---|
| Big Ten | 1,128 | 881 | +28% |
| Pac-12 | 492 | 730 | **−33%** |

A team-only join backdates 2024 realignment across the whole decade. Nothing errors. The
number is just wrong — and note that *how* wrong depends on when you run it: with the corpus
ending in 2025 the same query returned 108 Pac-12 punts, an 85% understatement, because the
conference was down to two members. 2026 rebuilt it to eight and the error shrank to 33%
without anything about 2018 changing. A wrong answer whose magnitude drifts with the present
day is the worst kind to have in a report.

Two defences are in place. `pbp.dim_team_season` carries a `COMMENT` saying so, and
`build_snapshot.py` resolves every conference at **build time** on `(team_id, season)` —
so the snapshot's `play` table has `kicking_conference` and `receiving_conference` already
correct and a query cannot get it wrong later. `verify_phase4.sql` runs both versions of
the query side by side and prints them, and the console's SQL tab ships the comparison as
its first sample query.

FBS membership is likewise per season, and it grows: 134 FBS teams in 2014, 129 in 2016,
136 in 2025, 138 in 2026. `dim_team_season.division` is the right test for "was this team
FBS *that year*", which is why a team promoted mid-window shows blank rather than zero in
its pre-promotion seasons in the Excel report.

---

## Data dictionary

Three things to know before reading it:

- **NULL is meaningful in this schema, in three different ways.** `fg_made IS NULL` means
  the kick was wiped out by penalty. `returned IS NULL` means the text stated no outcome
  (on punts and kickoffs) *or* the column does not apply (on field goals and conversions).
  `fg_distance_yds IS NULL` on a field goal means the parser could not recover it, or the
  feed stated it impossibly and it was dropped.
- **Populated percentages below are within the kinds where the column applies**, not across
  all 313k rows.
- The Postgres fact table has 46 columns; the DuckDB snapshot's `play` table has 65 — the
  same columns minus `loaded_at`, plus flattened dimension attributes and six derived
  columns. Both are listed.

### `pbp.special_teams_play` — the fact table

313,583 rows, 117 MB. `play_uid text PRIMARY KEY`.

**Keys and grain**

| column | type | populated | notes |
|---|---|---|---|
| `play_uid` | text NOT NULL | 100% | `espn:<game_id>:<sequenceNumber>`, with `:pat` appended for a derived conversion row |
| `source` | text NOT NULL | 100% | always `espn` |
| `game_id` | bigint NOT NULL | 100% | joins `fact_game` |
| `season` | smallint NOT NULL | 100% | 2014–2026 |
| `week` | smallint | 100% | 1–17 regular, 1–5 postseason |
| `season_type` | text | 100% | `regular` \| `postseason` |
| `play_kind` | text NOT NULL | 100% | `kickoff` \| `punt` \| `field_goal` \| `pat` \| `two_point` |

**Situation** — all read off structured ESPN fields, not parsed

| column | type | populated | notes |
|---|---|---|---|
| `period` | smallint | 100% | >4 is overtime |
| `clock_secs_period` | integer | 100% | seconds remaining in the period |
| `wallclock_utc` | timestamptz | **96.6%** | per-play UTC timestamp. This is what makes a play-level weather join possible. 66.9% in 2017 — see [Known limits](#known-limits) |
| `down` | smallint | 100% | |
| `distance` | smallint | 100% | yards to a first down |
| `yards_to_goal` | smallint | 100% | yards to the opponent's goal line at the snap. **`0` is a null sentinel**, not the goal line — 1,114 rows |
| `kicking_team_id` | integer | 100% | ESPN `start.team.id`. Always the kicking team; see [The team convention](#the-team-convention) |
| `receiving_team_id` | integer | 100% | derived as the other competitor |
| `is_home_kicking` | boolean | 100% | |
| `score_diff_kicking` | smallint | 100% | kicking team's margin *before* the play |

**Outcome — field goals**

| column | type | populated (FG) | notes |
|---|---|---|---|
| `fg_distance_yds` | smallint | 99.8% | parsed. Should equal `yards_to_goal + 17` (10 of end zone + ~7 of snap) |
| `fg_made` | boolean | 99.4% | **NULL = negated by penalty**, neither a make nor a miss. 1,351 rows corpus-wide |
| `miss_reason` | text | 3.0% | `blocked` (874) \| `wide left` (28) \| `wide right` (19) \| `short` (15). Only populated when the text says so |
| `fg_dist_bucket` | *snapshot only* | 99.8% | 5-yard bucket keyed by lower bound; `<20` collapses to 15 and `60+` to 60 |

**Outcome — punts and kickoffs**

| column | type | populated | notes |
|---|---|---|---|
| `punt_gross_yds` | smallint | 99.6% of punts | |
| `punt_net_yds` | smallint | 87.7% of punts | net legitimately exceeds gross when a return loses yardage (~0.7% of punts) |
| `kickoff_yds` | smallint | 98.1% of kickoffs | |
| `return_yds` | smallint | 88.0% punts / 89.3% kickoffs / 1.3% FG | **0 means not advanced**; NULL on a touchback by design. Populated on 419 field goals — returns of blocked or missed kicks |
| `returned` | boolean | 89.6% punts / 90.5% kickoffs | did a return actually happen. **NULL = the text stated no outcome.** NULL by construction on FGs and conversions |
| `touchback` | boolean | as `returned` | |
| `fair_catch` | boolean | as `returned` | |
| `downed` | boolean | as `returned` | |
| `out_of_bounds` | boolean | as `returned` | |
| `onside` | boolean | 100% of kickoffs | 1,486 onside kicks. **Never NULL** — an unqualified "kickoff" is a normal one, so this is knowable |
| `kick_blocked` | boolean | 100% where applicable | 917 punts, 865 field goals. **Never NULL** — a punt recorded as travelling 41 yards was not blocked |
| `returned_for_td` | boolean | 100% of the three kicks | 1,235 rows |

`returned`, `touchback`, `fair_catch`, `downed` and `out_of_bounds` are the **five "how did
it end" flags**, and they are NULLed *together* when the text answers that question not at
all. `kick_blocked` and `onside` deliberately stay `false` in that case, because both are
knowable from what the text *does* say. `returned IS NULL` is the single-column test for
"outcome not stated" — but only within punts and kickoffs.

**Outcome — conversions**

| column | type | populated | notes |
|---|---|---|---|
| `converted` | boolean | 100% of pat + two_point | |
| `two_point_type` | text | 65.1% of two_point | `pass` (1,621) \| `rush` (752). NULL when the text does not say |
| `negated_by_penalty` | boolean | 100% of the three kicks | 1,351 rows. A negated kick records `fg_made`/`converted` NULL — asserted at tolerance zero |

**People**

| column | type | populated | notes |
|---|---|---|---|
| `kicker_athlete_id` | bigint | 98.7% | ESPN athlete id. **Prefer this over `kicker_name` for any grouping** |
| `returner_athlete_id` | bigint | 98.2% *on returned kicks* | 32.1% unconditionally, which is the wrong denominator — most kicks are not returned |
| `tackler_athlete_id` | bigint | 10.9% | first tackler only; the bridge has all of them |
| `kicker_name` | text | 98.3% | as the feed wrote it. Kept unrepaired on purpose |
| `returner_name` | text | 33.2% | |
| `blocker_name` | text | 0.5% | no athlete id exists for this role |

**Provenance and enrichment**

| column | type | populated | notes |
|---|---|---|---|
| `play_text` | text | 100% | the verbatim ESPN string. Never dropped, never cleaned |
| `parse_confidence` | text | 100% | `exact` \| `partial` \| `ambiguous` \| `none` |
| `loaded_at` | timestamptz | 100% | Postgres only; not carried into the snapshot |
| `venue_id` | integer | 99.9% | copied from `fact_game`. **Cleared by `load_2_insert.sql`** — see the loader table |
| `neutral_site` | boolean | 100% | same origin, same caveat |
| `conference_game` | boolean | 100% | same origin, same caveat |

### The DuckDB snapshot's `play` table

`scripts/build_snapshot.py` flattens the fact table against every dimension and precomputes
the derived columns, so no query has to repeat a join or a `CASE`. Everything above, minus
`loaded_at`, plus:

**Flattened from the dimensions** — all resolved at build time

| column | source |
|---|---|
| `kicker_known_name`, `kicker_name_confidence` | `dim_athlete` on `kicker_athlete_id` |
| `kickoff_utc`, `attendance` | `fact_game` |
| `venue_name`, `venue_city`, `venue_state`, `venue_country`, `surface` | `dim_venue` |
| `kicking_team`, `receiving_team` | `dim_team` |
| `kicking_conference`, `kicking_division`, `receiving_conference`, `receiving_division` | `dim_team_season` on **`(team_id, season)`** |

**Derived, computed once**

| column | definition |
|---|---|
| `game_secs_remaining` | seconds left in regulation: `(4 - period) * 900 + clock_secs_period`. Overtime collapses to 0 |
| `is_clutch` | 4th quarter or later, margin ≤ 8, under five minutes |
| `fg_dist_bucket` | 5-yard bucket keyed by lower bound; `<20` → 15, `60+` → 60 |
| `game_month` | `month(kickoff_utc)` |
| `fbs_vs_fbs` | both divisions `FBS` — the FBS-vs-FCS games that scope decision chose to ingest anyway |

**Also in the snapshot**

| table | rows | what |
|---|---|---|
| `play` | 313,583 | the wide fact |
| `play_athlete` | 675,856 | the bridge, copied verbatim |
| `dim_athlete` | 33,194 | |
| `dim_team_season` | 3,368 | |
| `dim_venue` | 200 | |
| `fact_game` | 10,379 | |
| `season_status` | 13 | one row per season: games, plays, `last_regular_week`, `last_kickoff`, `is_in_progress` |
| `snapshot_meta` | 1 | `built_at`, `source_db`, `rows` — so an app shows the snapshot's age rather than pretending to be live |

`dim_conference` and `dim_team` are not copied; their content is already flattened onto
`play`.

### The dimensions and the bridge

| table | rows | grain | columns |
|---|---|---|---|
| `pbp.fact_game` | 10,379 | one game | `game_id` PK · `season` · `week` · `season_type` · `kickoff_utc` · `home_team_id` · `away_team_id` · `venue_id` **(the only declared FK in the schema)** · `attendance` · `neutral_site` · `conference_game` |
| `pbp.play_athlete` | 675,856 | play × role × athlete | `play_uid` · `role` · `athlete_id` · `ordinal`; PK on the first three |
| `pbp.dim_athlete` | 33,194 | one athlete, **career** | `athlete_id` PK · `known_name` · `name_confidence` · `primary_role` · `primary_team_id` · `first_season` · `last_season` · `st_plays` |
| `pbp.dim_team_season` | 3,368 | team × season | `team_id`, `season` PK · `conference_id` · `conference_name` · `division` (`FBS` \| `FCS`) |
| `pbp.dim_team` | 246 | one team | `team_id` PK · `display_name` (the team's *most recent* name in the window) |
| `pbp.dim_venue` | 200 | one venue | `venue_id` PK · `venue_name` · `city` · `state` · `zip` · `country` · `surface` (73 grass / 127 turf). **No roof field** and no lat/lon |
| `pbp.dim_conference` | 31 | one conference | `conference_id` PK · `conference_name` · `short_name` |

`dim_athlete` is a **career aggregate**, and that is load-bearing: `first_season`,
`last_season`, `st_plays`, the modal `known_name` and `primary_team_id` are all taken across
every season at once. A season-scoped rebuild would give every returning kicker
`first_season = 2026`. This is why `build_dims.py athlete` grew a `--plays` flag that
accepts several extracts.

`primary_role` describes the *player*, not the row that linked him: `patScorer` is
canonicalised to `kicker` before the vote, because a placekicker takes far more extra
points than field goals and counting the raw role literally would relabel most kickers
`patScorer`. The bridge keeps the raw role.

Five venues are outside the United States: Aviva Stadium and Croke Park (Dublin), Thomas A.
Robinson National Stadium (Nassau), ANZ Stadium and Allianz Stadium (Sydney).

### Referential integrity, measured

**Only one foreign key is declared in this entire schema** (`fact_game.venue_id`). The other
fifteen joins are invariants of the build scripts, not constraints. That is a deliberate
design — it keeps the loaders simple and lets a dimension be swapped wholesale without
cascade — but it means integrity is only as good as the last load, which is why it is
*measured* rather than asserted:

| check | rows |
|---|---|
| plays with no `fact_game` row | 0 |
| `kicker_athlete_id` not in `dim_athlete` | 0 |
| kicking team with no `(team_id, season)` row | 0 |
| `venue_id` not in `dim_venue` | 0 |
| `play_athlete` rows orphaned on either side | 0 |
| `dim_team_season` rows naming a team absent from `dim_team` | **223** (29 teams) |

That last one is expected and benign: `dim_team` is built from the game lists and so holds
only the 246 teams that actually appear in a game in the window, while `dim_team_season`
carries every FBS and FCS team the conference feed knows about. 29 FCS programmes never
played an ingested game. The ERD prints this count on the page rather than claiming the
schema is clean.

---

## Known limits

Read this section before quoting any number out of this warehouse. None of these are bugs
waiting to be fixed; they are properties of the source, or decisions to keep a defect
visible instead of silently repairing it.

### 1. 20,950 kicks state no outcome — 9.9% of the 211,659 punts and kickoffs

ESPN's terse form (`"Joshua Brown punt for 34 yds"`) names no outcome at all, and no regex
recovers what is not written. Those rows carry **NULL** on the five "how did it end" flags,
so `avg(touchback::int)` drops them from numerator *and* denominator by itself and no rate
is deflated. What you must not do is treat a season's rate as equally solid regardless:

| unstated outcome share | punts | kickoffs |
|---|---|---|
| 2014–2017 | 2.6–4.7% | 3.4–3.9% |
| 2018–2020 | 4.1–4.6% | 9.2–10.9% |
| 2021–2022 | 14.7–15.2% | 10.6–11.8% |
| **2023** | **29.6%** | **28.0%** |
| 2024 | 16.7% | 12.1% |
| 2025 | 21.9% | 6.7% |

A clean season runs ~5%. Above that, a per-season outcome rate rests on a visibly smaller
denominator — 2023's touchback rate of 68.6% is computed on 72% of that season's kickoffs.
The console scores this as an assertion with a 5% tolerance and colours anything over 12%
red; the explorer makes `Unknown` a first-class, filterable outcome value with its own stat
tile, painted in muted ink rather than a categorical hue so it reads as *absence*.

**No answer key can adjudicate this.** Any comparison source that also defaults an
unreadable outcome to `false` scores those rows as agreement either way, because
`bool(None) == bool(False)`. The distinction is real and invisible to a field-by-field
diff, which is why it is written down rather than measured.

### 2. `returned IS NULL` means two different things

Within punts and kickoffs it means "the text stated no outcome". On field goals and
conversions it means "this column does not apply" — all 31,209 field goals and all 70,715
conversions have `returned IS NULL`. Always scope the test:

```sql
WHERE play_kind IN ('punt', 'kickoff') AND returned IS NULL   -- unstated outcome
```

### 3. 2014 PAT and two-point rates are not trend points

2014 reports a **98.49%** PAT rate against 96.5–96.9% for 2015–2018. It is not a parse
failure: ESPN's 2014 play text simply contains about half the failed extra points the
neighbouring seasons do, against an essentially identical number of touchdowns.

| counted in the raw summary text | 2014 | 2015 | 2016 |
|---|---|---|---|
| PATs recorded good | 5,742 | 5,782 | 5,783 |
| PATs recorded failed / blocked / missed | **91** | 182 | 203 |
| implied PAT rate | **98.44%** | 96.95% | 96.61% |

Counting the outcome words in the raw text independently of the parser gives 98.44 / 96.95 /
96.61; the loaded table gives 98.49 / 96.96 / 96.60. The pipeline reproduces the feed
faithfully. Roughly 90–110 failed PATs are missing from the source. 2014's PAT rows have no
NULL outcomes and the same low-confidence count as every other season.

**The denominator is right and the numerator is too high.** Punts, kickoffs and field goals
in 2014 show no such gap and track the adjacent seasons closely.

### 4. 2023 returner identity drops to 85.7%

Every other season links returners at 96–99.5% on returned kicks; 2023 comes in at 85.7%.
2023 is also the worst season for unstated outcomes (29.6% / 28.0%), so its *returned* set
is both smaller and less well identified than any other season's. Treat 2023 returner
leaderboards with suspicion. The cause has not been run down.

### 5. A season still being played needs holding out of anything fitted

2026 is in the corpus with 8 games. `build_snapshot.py` writes a `season_status` table
marking any season whose most recent game kicked off inside **30 days** as in progress, and
the rule is self-maintaining: roughly a month after the last bowl, 2026 stops being in
progress on its own, and the offseason correctly reports none.

The two applications treat it differently, on purpose:

- **The console holds it out of both fitted baselines and out of the season-continuity
  chart.** `fg_exp` enters season *linearly*, so a 2026 twenty-one attempts deep would tug
  the whole decade-long quality trend and every kicker in every season would be measured
  against it. That is a correctness problem.
- **The explorer filters nothing.** It fits no models, so a part-season is a *reading*
  problem there, not a correctness one. It carries a `2026 partial` badge whose tooltip
  gives games, kicks and week, and a note above every by-season grid.

### 6. `yards_to_goal = 0` is a null sentinel

ESPN omits the start yardline on some plays and it arrives as `0`, not NULL — 1,114 rows
(0.36%), spread across all five kinds. Exclude it from any field-position analysis or it
reads as a snap on the opponent's goal line. The punt baseline restricts to
`yards_to_goal BETWEEN 20 AND 100` for exactly this reason.

### 7. `wallclock_utc` is 66.9% populated in 2017

99%+ in every other season, 96.6% overall. It blocks nothing today, but it is a real hole
in the play-level weather join if that is ever built.

### 8. 2020 is two-thirds of a season

563 games against ~880 either side. Nothing flags it. Any per-season rate that treats 2020
as a normal year is comparing a COVID-shortened season to full ones.

### 9. Remaining defects the build chose to keep visible

| | rows | why it is still there |
|---|---|---|
| `punt_gross_yds > 90` | 1 | `Kaare Vedvik punt for 92 yds for a touchback` (2017). A faithful parse of source text; the plausibility guard covers field goals and kickoffs, not punt gross |
| FG distance disagrees with `yards_to_goal + 17` by >3 yd | 153 (0.49% of FGs) | one of the two fields is wrong on that row and there is no way to tell which. This is the cheapest independent check on the parsed distance there is |
| FG made/missed with no distance | 61 | parser recovered the result but not the yardage. These cannot enter any distance-adjusted model |
| `returned` true but `return_yds` NULL | 1,598 | the feed self-conflicts (says out-of-bounds *and* lists a return). The parser NULLs the yardage and flags the row `ambiguous` rather than guessing |
| `touchback` and `returned` both true | 6 | the feed itself says both — a punt downed in the end zone, then a fumble return |
| `punt_net_yds > punt_gross_yds + 10` | 55 | past ten yards the two fields disagree, rather than the return having lost yardage |
| `kicker_name` starting with `(` or `#` | 1 | `(Fake Punt) Michael Burton run for 2 yds` — a fake punt where the leading parenthetical was captured as the punter. A standing regression guard, described below |
| `parse_confidence = 'none'` | 64 | mostly rows where `play_text` is NULL in the source |

**The `kicker_name` guard is worth keeping even at one row.** 1,012 field goals — 955 of
them in 2025, a third of that season's attempts — used to carry the gamebook clock and
jersey prefix inside the name (`(09:34) #98 I.Hankins`), matched no athlete id, and read as
973 phantom one-kick kickers in any leaderboard grouped by name. The Dash explorer exposed
them as `player_name_unparsed` rather than stripping the prefix for display, on the grounds
that a cosmetic repair would hide a real parser bug. That was the right call — the bug was
fixed at the parser and all 1,012 now link to a real athlete id. The column stays so the
next dialect ESPN introduces lights up in a grid instead of quietly producing phantom
kickers.

### 10. Where the baselines are weak, stated plainly

The console fits two models. Both are fit on the **whole corpus**, never on the filtered
subset — "above expected" needs a fixed league yardstick or a filter moves the goalposts
along with the players — and both exclude any in-progress season.

**`fg_exp.p_hat`** — P(make | distance, season). Cubic spline in distance, linear in season.
30,932 attempts. **AUC 0.7172, Brier 0.1694, log loss 0.5116.** AUC that low is the ceiling,
not a weak fit: distance is very nearly the only observable signal in a field goal.
Calibration is what a leaderboard depends on, and calibration is good — within **1.2
points** in every 5-yard bucket holding 2,000+ attempts. The two thin long-distance buckets
drift: 55–59 (403 attempts) is 2.4 points optimistic, 60+ (41 attempts) 1.7 points
pessimistic. Do not read a "points above expected" figure built mostly on 55+ kicks.

**`punt_exp.exp_net`** — E[net | yards to goal at the snap]. 85,040 punts. **R² 0.0374, RMSE
10.97 yd.** Low *by construction*: one punt's net is decided by the returner, not by the
line of scrimmage. It is a conditional mean, not a predictor. The residual only means
something averaged over 100+ punts.

---

## The pipeline

```
                        ESPN — two undocumented APIs, no key
       site.api                                            sports.core.api
  ┌────────────────────────────┐              ┌──────────────────────────────────┐
  │ scoreboard?groups=80       │              │ events/…/plays  (participants[]) │
  │ summary?event=<game_id>    │              │ seasons/…/groups/{80,81}         │
  └─────────────┬──────────────┘              └────────────────┬─────────────────┘
                │ fetch_espn.py                                │ fetch_participants.py
                v                                              v  build_dims.py conf
   games_<season>.json                            participants/<gid>.json.gz
   summaries/<gid>.json.gz                        dim_team_season.csv
                │                                 dim_conference.csv
     ┌──────────┴──────────┐                                   │
     v                     v                                   │
 build_dims.py venue   build_table.py + st_parser.py            │
     │                     │                                   │
     v                     v                                   │
 dim_venue.csv         st_plays.csv ──> build_dims.py athlete <─┘   ids from participants,
 dim_team.csv              │                   │                    names from st_plays
 fact_game.csv             │                   v
     │                     │           dim_athlete.csv
     │                     │           play_athlete.csv
     │                     │           play_athlete_wide.csv
     └──────────────┬──────┴───────────────────┘
                    │  sql/load_*.sql — \copy into all-text staging, then a typed INSERT
                    v
 ┌────────────────────────────────────────────────────────────────────────┐
 │ PostgreSQL  cfb.st.*  — 8 tables, 235 MB, the system of record         │
 │ special_teams_play · play_athlete · fact_game                          │
 │ dim_athlete · dim_team_season · dim_team · dim_venue · dim_conference   │
 └────────────────────────────────────────────────────────────────────────┘
                    │  build_snapshot.py — DuckDB postgres extension,
                    │  read-only ATTACH, one CREATE TABLE AS per table
                    v
             data/out/pbp.duckdb  (44 MB)
                    │
     ┌──────────────┼──────────────────┐
     v              v                  v
   app.py       web/app.py          reports/
 Streamlit     Dash explorer      xlsxwriter
  console         :8060            workbooks
```

Nothing reads Postgres at runtime. The extract is one direction, one command, and the
snapshot is a build artifact — delete it and rebuild rather than repairing it.

### What each stage owns

| stage | script | reads | writes | cost |
|---|---|---|---|---|
| game lists | `fetch_espn.py games` | scoreboard, 22 week-requests per season | `games_<season>.json` | ~30 s/season |
| play text | `fetch_espn.py summaries` | one summary per game | `summaries/*.json.gz` | 6 workers, ~0.4 s/game |
| athlete ids | `fetch_participants.py` | core-API plays, paged at 400 | `participants/*.json.gz` | 6 workers |
| conferences | `build_dims.py conf` | core-API season groups | `dim_team_season.csv`, `dim_conference.csv` | ~250 API calls |
| venues, teams, games | `build_dims.py venue` | every stored summary on disk | `dim_venue.csv`, `dim_team.csv`, `fact_game.csv` | the slow local stage — re-reads all 10,379 summaries |
| the fact table | `build_table.py` | summaries + `st_parser` | `st_plays.csv` — 40 columns, 80 MB | a few minutes for the full window |
| the athlete link | `build_dims.py athlete` | participants + `st_plays.csv` | `dim_athlete.csv`, `play_athlete.csv`, `play_athlete_wide.csv` | reads all participants files |
| the snapshot | `build_snapshot.py` | Postgres | `pbp.duckdb` | seconds |

Two properties hold across every stage and are worth relying on:

- **Every fetch stage skips what it already has**, so re-running is cheap and an interrupted
  run is fixed by running it again. Refresh flags exist to override that for a live season.
- **The parser half is re-derivable from disk.** `play_text` is on every row, and every
  summary is on disk, so a parser change needs no re-fetching at all.

### Which loader is right when

This is the part most likely to be got wrong, because the wrong choice does not error — it
quietly empties columns. Three loaders exist and they are not interchangeable:

| you are… | use | why not the others |
|---|---|---|
| **adding or rebuilding whole seasons** (a backfill) | `load_1_stage.sql` → `\copy` → `load_2_insert.sql` → **`enrich_game_context.sql`** → the athlete link | `reparse.sql` is an `UPDATE` joined on `play_uid` and cannot insert new rows. `load_3_season.sql` handles one season |
| **changing the parser** (no new rows) | `load_1_stage.sql` → `\copy` → `reparse.sql` | `load_2_insert.sql`'s `TRUNCATE` would wipe the enrichment and force the whole athlete link to be re-run behind it |
| **pulling an in-progress season forward** (weekly) | `scripts/update_season.py <season>` — which uses `load_3_season.sql` and `load_athletes_3_season.sql` | `load_2_insert.sql` rewrites all 313k rows to add thirty games, three times over once the enrichment `UPDATE`s follow. That is the bloat the *reclaim space* step exists to mop up |

**The trap in `load_2_insert.sql`, stated once so it is never rediscovered.** Its `TRUNCATE`
clears three groups of columns that its own `INSERT` column list does not repopulate:

| cleared by the TRUNCATE | restored by |
|---|---|
| `venue_id`, `neutral_site`, `conference_game` | `sql/enrich_game_context.sql` |
| `kicker_athlete_id`, `returner_athlete_id`, `tackler_athlete_id` | the athlete link (runbook D) |
| nothing else — every parser column is in the insert list | |

The venue columns are the quiet one. Nothing errors; every `dim_venue` join simply returns
zero rows, so the field-goal-by-surface section of `verify_phase4.sql` goes silently empty.
**Run the verify suite after any backfill and check that section is non-empty.**
`load_3_season.sql` avoids the whole problem by applying that season's game context inside
the same transaction, so the enrichment cannot be forgotten.

Both `reparse.sql` and `load_athletes_2_apply.sql` write a rollback table before they touch
anything (`pbp.special_teams_play_prereparse`, `pbp.special_teams_play_preathletefix`). Both
refuse to run if staging and the fact table disagree about which plays exist — a mismatch
means the CSV was built from a different fetch, and that is the wrong operation, not a
warning. **The in-season loaders deliberately write no rollback table**: copying 313k rows
aside every week to protect a load that only ever touches one season would cost more than
the thing it protects, and that season is re-derivable by running the script again.

---

## Runbooks

Every one of these is run from the repository root — several SQL files resolve CSV paths
relative to the caller.

### A. Pull the in-progress season forward (the weekly run)

```bash
.venv/bin/python scripts/update_season.py 2026              # the whole thing
.venv/bin/python scripts/update_season.py 2026 --dry-run    # fetch, build, change nothing
.venv/bin/python scripts/update_season.py 2026 --skip-conf  # skip the ~250-call conf refetch
.venv/bin/python scripts/update_season.py 2026 --skip-fetch # rebuild from summaries on disk
```

It runs fetch → build → load → snapshot in order, streams each stage, stops at the first
non-zero exit, and is idempotent. On the eight games of 2026 week 1 it takes about 90
seconds, most of it `build_dims.py venue` re-reading every stored summary.

It exists because the backfill path is wrong in-season in three specific ways:

| backfill behaviour | why it breaks in-season | replaced by |
|---|---|---|
| `fetch_espn.py games` skips the games list if the file exists | the list freezes at whatever had finished the first time it ran | `--refresh`, always passed for the live season |
| nothing revisits a game once fetched | a late box-score correction is never picked up | `--refresh-days 21`, which re-pulls summaries **and** participants for anything that kicked off inside the window |
| `load_2_insert.sql` truncates everything and needs the enrichment and the whole athlete link behind it | 313k rows rewritten to add thirty games | `load_3_season.sql` + `load_athletes_3_season.sql` |

**Where the season boundary sits.** Not everything can be scoped, and the exception is the
one worth remembering:

| | scope | why |
|---|---|---|
| games list, summaries, participants | season | `--seasons` / the `SEASONS` env var |
| `st_plays_<season>.csv` | season | `build_table.py --seasons` |
| `pbp.special_teams_play`, `pbp.play_athlete`, the three id columns | season | `DELETE` + `INSERT` on that season only; earlier seasons are provably untouched, and both loaders print every season's coverage so you can see they did not move |
| `dim_team_season`, `dim_venue`, `dim_team`, `fact_game` | **global** | small enough that a wholesale swap cannot leave a stale row behind — 200 venues, 3.4k team-seasons, 10.4k games |
| `pbp.dim_athlete` | **global** | a *career* aggregate. A 2026-only rebuild would give every returning kicker `first_season = 2026`. Eight games of 2026 updated 100 existing athletes |

That last row is why `build_dims.py athlete` takes `--plays` with several extracts: it
derives the dimension from the frozen full `st_plays.csv` **plus** the new season's, so the
career fields stay right without re-running `build_table.py` over thirteen seasons of
summaries. `--only-season` then narrows the bridge and wide outputs — the parts
`load_athletes_3_season.sql` deletes and re-inserts — and writes them as
`play_athlete_<season>.csv` and `play_athlete_wide_<season>.csv`, deliberately **not** over
the global pair. `load_athletes_2_apply.sql` truncates `pbp.play_athlete` and reloads it from
whatever `play_athlete.csv` holds, so one season's rows sitting under the global name would
arm that script to destroy the other twelve.

Two guards fire before anything is written, and both have caught a real mistake:

- `update_season.py` refuses to proceed if `dim_team_season.csv` has no rows for the target
  season. On the first run of a new season `--skip-conf` is not safe, and loading the file
  as-is would replace `dim_team_season` with one that has no 2026 rows — silently emptying
  every conference on every 2026 play, because the snapshot joins on `(team_id, season)`.
- `load_athletes_3_season.sql` refuses if staging holds any play outside the target season,
  because the season-scoped `DELETE` would then strip bridge rows the `INSERT` never puts
  back.

### B. Re-derive after a parser change

`play_text` is on every row, so no re-fetching is needed.

```bash
.venv/bin/python scripts/build_table.py --out /tmp/new.csv    # diff before going near Postgres
.venv/bin/python scripts/build_table.py                       # -> data/out/st_plays.csv
psql -d cfb -f sql/load_1_stage.sql
psql -d cfb -c "\copy pbp.stg_plays FROM 'data/out/st_plays.csv' WITH (FORMAT csv, HEADER true)"
psql -d cfb -f sql/reparse.sql                                # UPDATE, not TRUNCATE
.venv/bin/python scripts/build_snapshot.py
psql -d cfb -f sql/verify.sql                                 # then read the console's checks
```

`reparse.sql` updates the **25 columns the loader derives** — the 22 from the parser plus
`kicking_team_id`, `receiving_team_id` and `is_home_kicking`. Those last three are not
parser output but they *are* derived (`emit_pat` picks the conversion's team off the
scoreboard), so leaving them out would load a corrected CSV and change nothing. Everything
else — the athlete ids, the situation, `play_text` — is left alone. Pre-update values go to
`pbp.special_teams_play_prereparse`; drop that table once you are satisfied.

Judge the result on the console's Validation tab, not on the pooled rate. It scores every
assertion on the **worst single season**, which is the whole reason the 2025 punt gap was
findable: it was 46% of that season and 4.6% of the decade.

### C. Add a finished season (backfill)

The window is set in three places that must agree: `SEASONS` in `scripts/build_table.py`,
`scripts/build_dims.py` and `scripts/fetch_participants.py`. All three run to 2026.
`fetch_espn.py` reads its own from the environment.

```bash
SEASONS=2013 .venv/bin/python scripts/fetch_espn.py games
SEASONS=2013 .venv/bin/python scripts/fetch_espn.py summaries
.venv/bin/python scripts/fetch_participants.py           # reads SEASONS from the script
.venv/bin/python scripts/build_dims.py conf
.venv/bin/python scripts/build_dims.py venue
.venv/bin/python scripts/build_table.py
psql -d cfb -f sql/load_dims.sql
psql -d cfb -f sql/load_1_stage.sql
psql -d cfb -c "\copy pbp.stg_plays FROM 'data/out/st_plays.csv' WITH (FORMAT csv, HEADER true)"
psql -d cfb -f sql/load_2_insert.sql                     # TRUNCATE + reload
psql -d cfb -f sql/enrich_game_context.sql               # REQUIRED — restores venue/neutral/conf
# then runbook D (the athlete link), then build_snapshot.py, then reclaim space
psql -d cfb -f sql/verify.sql
psql -d cfb -f sql/verify_phase4.sql                     # check the surface section is non-empty
```

**2013 will not work**, for the record: the participants feed has no athlete `$ref` before
2014, so the season would land with no identity at all. 2014 is the floor.

`load_dims.sql` runs all five dimension loads in one transaction in FK order (`dim_venue`
before `fact_game`), `DELETE` then `\copy`. Doing it by hand is how `dim_venue` gets loaded
after `fact_game` and the FK rejects the whole batch.

### D. Re-derive the athlete link

Only needed when the participants feed or `build_dims.stage_athlete` changes, or after a
`load_2_insert.sql` backfill. Not after an ordinary parser edit.

```bash
.venv/bin/python scripts/build_dims.py athlete           # participants + names -> 3 CSVs
psql -d cfb -f sql/load_athletes_1_stage.sql
psql -d cfb -c "\copy pbp.stg_dim_athlete FROM 'data/out/dim_athlete.csv' WITH (FORMAT csv, HEADER true)"
psql -d cfb -c "\copy pbp.stg_play_athlete FROM 'data/out/play_athlete.csv' WITH (FORMAT csv, HEADER true)"
psql -d cfb -c "\copy pbp.stg_play_athlete_wide FROM 'data/out/play_athlete_wide.csv' WITH (FORMAT csv, HEADER true)"
psql -d cfb -f sql/load_athletes_2_apply.sql
.venv/bin/python scripts/build_snapshot.py
```

`load_athletes_2_apply.sql` clears the three id columns before applying, so the result does
not depend on what ran before it, and keeps the previous values in
`pbp.special_teams_play_preathletefix`. It prints coverage by kind and by season on the way
out.

To repair games whose stored participants file uses the old id-based key:

```bash
.venv/bin/python scripts/fetch_participants.py --games games.json   # a JSON list of game ids
```

`build_dims` reads either stored shape — a key beginning with the game id is the old form
and gets the prefix stripped, anything else is already a sequence number — so no future
re-fetch is forced.

### E. Rebuild the snapshot

```bash
.venv/bin/python scripts/build_snapshot.py            # -> data/out/pbp.duckdb
.venv/bin/python scripts/build_snapshot.py --db cfb --out /tmp/other.duckdb
```

It deletes and recreates the file, installs and loads DuckDB's `postgres` extension,
`ATTACH`es read-only, and runs one `CREATE TABLE AS` per table. Both apps pick up a new
snapshot on restart. Run it after any load.

### F. Reclaim space after a backfill

`load_2_insert.sql` followed by the two enrichment `UPDATE`s rewrites every row three times,
so the heap ends up roughly 3× the size of the live data.

```bash
psql -d cfb -c "VACUUM (FULL, ANALYZE) pbp.special_teams_play"
psql -d cfb -c "VACUUM (FULL, ANALYZE) pbp.play_athlete"
```

Autovacuum frees the space for reuse but cannot shrink the file. `VACUUM FULL` takes an
exclusive lock and rewrites the table, but the apps read the snapshot rather than Postgres,
so it can run any time. This took `cfb` from 612 MB to 232 MB on 2026-08-31 (dropping five
rollback tables accounted for 173 MB of that). The in-season path does not create the bloat
and does not need this.

### G. Verify

```bash
psql -d cfb -f sql/verify.sql          # counts by season/kind, football sanity, integrity
psql -d cfb -f sql/verify_phase4.sql   # athlete coverage, the conference trap, surface, orphans
.venv/bin/streamlit run app.py         # then the Validation tab: 20 assertions
```

`verify.sql` and `verify_phase4.sql` print; they do not assert. The console's Validation tab
is the part that grades. Between them they cover row counts by season and kind (which is
how a new dialect gets caught — one season's PAT count merely looks low), the monotonic
field-goal curve, punt gross, the 2018 touchback step, parse confidence, athlete coverage on
the right denominators, the team-only versus team-season conference comparison run side by
side, field goals by surface (empty if the enrichment was skipped), and four
referential-integrity counts.

### Rollback points currently in Postgres

**None.** The database holds exactly the eight tables the pipeline needs. All five rollback
tables were dropped on 2026-08-31 once their changes were trusted. `reparse.sql` and
`load_athletes_2_apply.sql` each recreate the one they own the next time they run, so this
list refills itself as soon as either is used. `stg_*` tables are transient — every load
script drops them on the way out.

One rollback point is still on disk rather than in Postgres:
`data/espn/participants_pre20260831/` holds 454 participants files as they were before the
join-key re-fetch.

---

## The applications

Three separate front ends over the same snapshot, with deliberately different jobs.

### `app.py` — the Streamlit validation and pre-analysis console

One page, five tabs. Two jobs kept together on purpose: *does this table say what it
claims*, and *what is worth modelling*.

| tab | what it answers |
|---|---|
| **Validation** | 20 rule assertions, scored on the **worst single season** rather than the pooled rate; a field-completeness heatmap that colours only the cells carrying an expectation; season-over-season continuity with a marker at the old source boundary; parse confidence; athlete-link coverage on the right denominator |
| **Placekicking** | make rate by distance with Wilson 95% intervals against the fitted baseline, a calibration table, kickers ranked by points above expected, splits held at equal difficulty |
| **Punting** | net versus gross by field position, outcome mix, punters ranked by net above expected, and the outcome-classification panel |
| **Kickoffs & returns** | touchback rate against the 2018 rule change, kickoff distance, return rates on the returned-only denominator, onside kicks and return touchdowns |
| **SQL** | read-only scratchpad over the snapshot, with both baselines joinable as `fg_exp` / `punt_exp` and five worked sample queries |

Three design choices in it are worth not undoing:

- **Assertions are scored on the worst season and their tolerances are *shares*,** so a check
  means the same thing on one conference-season as on the whole decade. Pooling ten seasons
  hides a defect that lives in one of them.
- **A non-zero count is not automatically a failure.** Several checks track documented source
  defects the build chose to keep visible; each carries a note saying which. Selecting a row
  shows the offending plays with their `play_text`.
- **Each check has a `scope`,** i.e. its own denominator. A punt-only defect measured against
  every special-teams play reads three times cleaner than it is.

### `web/` — the Dash instance explorer

```bash
.venv/bin/python -m web.app          # http://127.0.0.1:8060
```

Where the console answers "is this trustworthy and what should I model", this answers "show
me the actual instances and let me take them apart". Field goals, punts and kickoffs only —
242,868 of the 313,583 rows — and profiles for the **kicking side only**.

| surface | route | what it is |
|---|---|---|
| **Explorer** | `/` | one filter set, three grains. **Plays** is the point: 242k rows on AG Grid's infinite row model. **Kickers & punters** and **Teams** are the same selection rolled up; clicking a row opens that entity |
| **Player** | `/player/<athlete_id>` | one page per athlete, with a section for each phase he actually kicks in. 1,533 of the 2,729 players with five or more kicks work more than one phase and 302 do all three, which is why it is one page per person rather than per role |
| **Team** | `/team/<team_id>` | decade roll-up on top, one row per season under it, then who kicked, then every kick |

Sidebar filters are **global**: a player page and a team page both honour them, each
ignoring only the facet it *is*. Clicking any play row opens a detail drawer that leads with
**who was involved** — kicker, returner, tackler, plus assisting tacklers from
`play_athlete` — each with the parser's name-match confidence and a link to their profile,
and the kicker carrying the line he had built that season *before* this kick. Situation,
environment and the verbatim `play_text` sit in collapsed sections underneath.

The play grid is server-side (`rowModelType="infinite"`). Sorting and every per-column
header filter are translated to SQL and pushed into DuckDB, so the browser never holds more
than a dozen 120-row blocks, header filters compose with the sidebar rather than fighting
it, and every `ORDER BY` is tie-broken on `play_uid` so paging is stable.

Design decisions in `web/` that are settled unless deliberately reopened: descriptive only,
**no models**; three phases only (extra points and two-point tries were removed from scope
mid-design); kicking side only; `Unknown` is a first-class outcome; `Onside` is tested before
the real outcomes, because an onside kick is a different play rather than a kickoff with an
unusual result — folding it in both muddied touchback rates and would dump 1,382 of the 1,486
onside kicks into `Unknown`. `web/README.md` carries the palette-validation record and two
implementation notes that each cost real debugging time (dash-ag-grid ships only the light
`quartz` stylesheet; never pass `None` to a Mantine colour prop).

### `reports/` — formatted Excel

House style and grid mechanics in `reports/workbook.py`; each report is a module beside it.

```bash
.venv/bin/python -m reports.fg_by_distance                          # -> data/out/fg_by_distance.xlsx
.venv/bin/python -m reports.fg_by_distance --team "Ohio State Buckeyes" --seasons 2019-2025
```

**`fg_by_distance`** — attempts, makes and make rate, 10-yard distance bands down and
seasons across. `Totals` is every FBS team pooled; `By Team` is the same grid driven by a
dropdown in B4, computed as `SUMIFS` over a hidden `_data` sheet — one sheet rather than 137,
recalculating in place with no macros, so it survives email, SharePoint and Google Sheets'
importer. Formulas carry a cached value so a non-Excel viewer shows the number instead of a
blank. `Notes` restates every scope decision.

Two presentation choices that encode real distinctions: **attempts blank out at zero** so a
team's pre-promotion seasons do not read as seasons it kicked nothing, while **makes keep
their zero**, because 0 for 4 from 50 is a real and different statement. And unparseable
distances land in an `Unknown` row rather than being dropped, so `All distances` reconciles
to the team's true attempt count.

### The ERD

```bash
.venv/bin/python scripts/build_erd.py        # -> reports/cfb_st_erd.pdf (+ .png proof)
```

One landscape **Tabloid (17×11in)** page, Crow's Foot notation, rendered by Chrome headless
from an SVG the script lays out itself. Everything on it — columns, types, primary keys, row
counts, table sizes and the orphan-row column of the relationship inventory — is read **live
from Postgres on each run**, so it cannot drift from the database.

The distinction the page exists to make: only one foreign key is declared in this schema,
drawn as a solid blue line; the other fifteen joins are invariants of the build scripts,
drawn dashed. That is a deliberate design, and it means integrity is only as good as the
last load — which is why the diagram *measures and prints* the orphan count for every
relationship rather than asserting they are clean.

Layout mechanics, for whoever retargets it: geometry is in points and the script lays out in
a 946×612 design space, scaling the whole canvas by `S = PAGE_W / W` onto the 1224×792pt
page. Tabloid's aspect (1.545) differs from Letter's (1.294), so a uniform blow-up would
have letterboxed; designing at the page aspect keeps the vertical layout intact and spends
the extra width on wider boxes and gutters. A font size in the source is a real size on
paper, multiplied by `S`. To retarget, change `PAGE_W`/`PAGE_H` and `W` together so
`PAGE_W/W == PAGE_H/H`. Entity positions and connector waypoints are the two tables near the
top of the script (`ENTITIES`, `RELS`); a waypoint is `V`/`H` to travel to an x, `Y` to
travel to a y.

---

## Query recipes

All of these run against the DuckDB snapshot (`data/out/pbp.duckdb`), where the dimensions
are already flattened onto `play`. The console's SQL tab ships the first one as its default.

**The conference trap — the same 2018 punt count, two ways**

```sql
WITH latest AS (                                    -- the WRONG way: team-only
    SELECT team_id, last(conference_name ORDER BY season) AS conference_name
    FROM dim_team_season GROUP BY team_id)
SELECT 'team-only (WRONG)' AS join_style, l.conference_name, count(*) AS punts
FROM play p JOIN latest l ON l.team_id = p.kicking_team_id
WHERE p.play_kind = 'punt' AND p.season = 2018
  AND l.conference_name IN ('Big Ten Conference', 'Pac-12 Conference')
GROUP BY 1, 2
UNION ALL
SELECT 'team-season (RIGHT)', p.kicking_conference, count(*)   -- resolved at build time
FROM play p
WHERE p.play_kind = 'punt' AND p.season = 2018
  AND p.kicking_conference IN ('Big Ten Conference', 'Pac-12 Conference')
GROUP BY 1, 2 ORDER BY 2, 1;
```

**How much of a season is quotable — the unstated-outcome share**

```sql
SELECT season,
       count(*) AS kicks,
       count(*) FILTER (WHERE returned IS NULL)                       AS unstated,
       round(100.0 * avg((returned IS NULL)::int), 1)                 AS pct_unstated
FROM play
WHERE play_kind IN ('punt', 'kickoff')      -- scope matters: NULL means something else on FGs
GROUP BY 1 ORDER BY 1;
```

**Outcome rates, on the denominator they belong on**

```sql
SELECT season,
       count(*) FILTER (WHERE returned IS NOT NULL)         AS classified,
       round(100.0 * avg(touchback::int), 1)                AS touchback_pct,  -- NULLs drop out
       round(100.0 * avg(returned::int), 1)                 AS returned_pct,
       round(avg(return_yds) FILTER (WHERE returned), 1)     AS avg_return_yds
FROM play
WHERE play_kind = 'kickoff' AND NOT onside   -- onside is a different play, not an outcome
GROUP BY 1 ORDER BY 1;
```

**A kicker's career, resolved by id and not by name**

```sql
SELECT a.known_name, a.name_confidence, any_value(p.kicking_team) AS team,
       a.first_season, a.last_season,
       count(*) AS att,
       round(100.0 * avg(p.fg_made::int), 1)           AS pct,
       max(p.fg_distance_yds) FILTER (WHERE p.fg_made) AS longest
FROM play p
JOIN dim_athlete a ON a.athlete_id = p.kicker_athlete_id
WHERE p.play_kind = 'field_goal' AND p.fg_made IS NOT NULL
GROUP BY a.athlete_id, a.known_name, a.name_confidence, a.first_season, a.last_season
ORDER BY att DESC LIMIT 10;
-- Christopher Dunn  114 att  84.2%  2018-2022 | Daniel Carlson  112  81.3%  2014-2017
```

**Everyone who touched one play**

```sql
SELECT b.role, b.ordinal, a.known_name, a.primary_role
FROM play_athlete b
LEFT JOIN dim_athlete a USING (athlete_id)
WHERE b.play_uid = 'espn:401520281:101849906'
ORDER BY b.role, b.ordinal;
```

**Audit anything back to source**

```sql
SELECT season, play_kind, kicking_team, kicker_name, kicker_known_name,
       parse_confidence, play_text
FROM play
WHERE parse_confidence IS DISTINCT FROM 'exact'
ORDER BY season DESC LIMIT 100;
```

**Which seasons are finished**

```sql
SELECT season, games, plays, last_regular_week, last_kickoff, is_in_progress
FROM season_status ORDER BY season;
```

Two habits worth keeping: filter `fbs_vs_fbs` for any kicker-quality question, and exclude
`yards_to_goal = 0` from any field-position analysis.

---

## Changelog

Every material change to the data, dated. Each row is a defect that was real, what it cost,
and what the number reads now. The live consequences of the ones that could not be fully
fixed are in [Known limits](#known-limits).

| date | change | what was wrong | result |
|---|---|---|---|
| 2026-08-30 | **Spine moved to ESPN; bulk archive dropped** | the archive was an incomplete extract of ESPN — 78 missing games, ~6% of plays, 10–22% of kickoffs — presenting as a 34.7% → 50.3% touchback jump at the source boundary. Its `start.team.id` was typed `double` in 2016/2018, making receiving = kicking on 24,676 rows | 240,636 → 258,614 rows; 8,556 → 8,634 games; touchback boundary 50.9% → 50.3% (i.e. flat); null kicking team 531 → **0**; kicking = receiving 24,676 → **0** |
| 2026-08-30 | **Gamebook dialect (Phase 2b)** | ESPN began concatenating the NCAA-official rendering from 2021 and nothing matched it. Four simultaneous breakages: leading `(MM:SS)` clock, `#NN` jersey prefixes, `Last,First` names, outcome clauses without the connecting words. The kickoff scoring form had no trap pattern and filed the *returner* as the kicker on 170 plays | unclassified 2025 punts 45.8% → **21.9%**, kickoffs 24.6% → **6.7%**; corpus-wide 24,900 → 19,716. Answer-key agreement improved on every field it touched and regressed on none (worst field 99.58% → **99.76%**) |
| 2026-08-30 | **Conversion attribution off the scoreboard** | `emit_pat` swapped kicking and receiving team *unconditionally*, on the premise that a conversion belongs to the offense that scored. True only for a defensive or return touchdown; for an ordinary one the swap moved the extra point to the opponent | conversions on the correct team 5.77% → **99.76%**. 52,093 PAT and 3,114 two-point rows changed team; 0 of 200,079 punts, FGs and kickoffs moved, and no parser-derived column changed anywhere |
| 2026-08-30 | **Phase 4 enrichment** | no dimensions, no athlete identity | four dimensions + `fact_game` + the bridge; `special_teams_play` gained the three id columns plus `venue_id`, `conference_game`, `neutral_site`. Conference stored **by season**, which the 2018 Pac-12 count (492 on a team-only join versus 730) is the argument for |
| 2026-08-31 | **Unreadable outcomes became NULL** | the five "how did it end" flags had no NULL state, so a kick whose ending the text never mentions was stored `false` on all of them — indistinguishable from a kick that genuinely had none. Every rate built on them was deflated by however many rows were unreadable. The README had claimed this needed a schema change; `\d` said otherwise, every column was already nullable and the parser was simply writing `false` | `avg(flag::int)` now drops those rows from numerator *and* denominator by itself. `returned IS NULL` replaced two eight-clause `NOT coalesce(...)` predicates, proven equivalent on the rebuilt snapshot with zero rows differing. The 19,716-row gap remains — it is a property of the feed — but the silent deflation is gone |
| 2026-08-31 | **Field goal clock strip + plausibility floors** | `parse_field_goal` never stripped the gamebook `(MM:SS)` that the punt and kickoff parsers had stripped since the dialect was added. 1,012 field goals — 955 in 2025, a third of the season — carried the clock and jersey number inside `kicker_name`, matched no athlete id, and read as 973 phantom one-kick kickers. The NCAA dialect's `nullified by penaltyGOOD` (no separating space) defeated the pattern entirely | all 1,012 clean and linked; 10 penalty-negated rows recovered; FG coverage 100%, `parse_confidence='none'` 12 → 2. Impossible values now dropped rather than published: FG distance outside 15–70 0.078% → **0.000%**, kickoff > 100 yd 0.031% → **0.000%**. Fourteen other assertions unchanged, none worse |
| 2026-08-31 | **Athlete identity, two independent bugs** | (1) all 58,535 conversions had a NULL `kicker_athlete_id`: `stage_athlete` considered only punt/kickoff/FG plays and looked for roles `kicker`/`punter`, which a conversion never carries — ESPN tags it `patScorer`/`patPasser`. (2) 2025 kicks collapsed from week 9: `fetch_participants` keyed each play by the core-API play `id`, assuming it is the game id plus sequence number. From week 9 the two endpoints stopped agreeing | conversions 0% → **98.4%**, and the links are as trustworthy as the ones already relied on (96.27% parsed-name agreement against a 96.26% control). Kicks 94.33% → **98.65%**; 2025 alone 58.2% → **98.82%**. Only the 454 unjoinable games were re-fetched, not all 8,634; the originals are kept on disk |
| 2026-08-31 | **`known_name` votes on `(initial, surname)`** | ESPN went 40.2% abbreviated in 2025 to **97.7%** in 2026. A modal vote over the raw string made a returning player split his own vote between two spellings of his own name, drop under the confidence floor, and end up with no name at all. 1,281 athletes were exposed | **recovered 209 names, lost none**, respelled 123 into a fuller form. `Dillon Curtis` (177 plays) and `Beckham Sunderland` (82) had no name before it |
| 2026-08-31 | **2014–2015 backfilled** | the corpus started in 2016; the participants feed supports 2014 | +54,745 rows, +1,714 games. 2014 brought its own source defect — see [Known limits](#known-limits) §3 |
| 2026-08-31 | **Rollback tables dropped, database vacuumed** | five rollback tables and three-rewrites-per-load bloat | `cfb` 612 MB → 232 MB; the database now holds exactly the eight tables the pipeline needs |
| 2026-08-31 | **Phase 4 load scripts brought into the repository** | the step that applies the athlete CSVs lived outside the repo, and `reparse.sql` warned about "a step that is not in this directory" | `load_athletes_1_stage.sql` and `load_athletes_2_apply.sql`. The athlete link is now reproducible from the same command list as everything else |
| 2026-09-01 | **In-season path** | the backfill path is wrong three ways against a season still being played | `update_season.py`, `load_3_season.sql`, `load_athletes_3_season.sql`, `load_dims.sql`, `build_dims.py athlete --plays/--only-season`, and the self-maintaining `season_status` table. 2026 loaded, 8 games |

---

## Not built

Four things are deliberately absent. None is blocked; each is a fresh decision rather than
a re-derivation, and what each would need is written down so picking it up does not start
from scratch.

### Weather — tabled, but both halves of the join key already exist

This was Phase 5 and was tabled at the user's request. It is the most valuable unbuilt thing
here: **per-play wind against 200k+ kicks is analysis that essentially does not exist
publicly** — most kicking data carries game-level weather at best, or none.

Already in place:

- `wallclock_utc` on 96.6% of rows — a real UTC timestamp, so the join can be at the moment
  of the kick rather than at the game.
- `dim_venue` with city, state, zip and country for 200 venues; `fact_game.venue_id` links
  every game and `venue_id` is denormalised onto every play.

Three things to decide before writing any code:

1. **Source.** The existing `weatherdata` warehouse starts in 2019 and has 112 stations
   against 200 venues, so it covers neither the full window nor the geography. NCEI ISD/LCD
   (free, ~2,000 US airport stations, decades of history) is the likely better fit. Either
   way `dim_venue` has **no lat/lon** — only city/state/zip — so venue geocoding is the first
   task.
2. **Indoor venues must be flagged first.** `dim_venue` records surface but not roof, and
   there is no roof field in the ESPN payload — it needs a separate source or a manual list.
   Assigning outdoor conditions to a dome is worse than assigning nothing. The five non-US
   venues need their own handling.
3. **Grain.** Nearest-station-at-kick-time is the obvious default, but its quality varies
   silently with station distance. Store the station id **and its distance from the venue**
   on every row, so weak joins stay filterable — the same discipline `parse_confidence`
   already applies to the parser.

Note the 2017 `wallclock_utc` hole (66.9%) is a real gap in that season's play-level join.

### Scrimmage plays — designed, measured, not built

`PLAN.md` §10 carries the full design, agreed 2026-08-31, with every number measured against
the local files rather than estimated. The headline is that it is a *cheaper* build than
special teams was, because the problem reverses: kick outcomes exist only in prose, but
scrimmage outcomes are **structured fields**.

| | |
|---|---|
| **Buildable with no new fetching** | `summaries/` and `participants/` already hold everything |
| **Grain** | one row per scrimmage play. **1,497,044 rows**, after excluding 111,510 administrative rows (`Timeout`, `End Period`, `Coin Toss`) and 2,294 plays the special-teams classifier already claims |
| **`st_parser.py` is not needed** | `statYardage`, down/distance/yards-to-endzone, `isTurnover`, `scoringPlay` and `sequenceNumber` are all **100%** populated, flat across all twelve seasons. Text parsing becomes optional enrichment (air yards, pass direction, penalty reason), not the spine |
| **Plus a bridge** | `pbp.scrimmage_athlete`, ~3.27M rows, same shape as `play_athlete`. The four id columns on the fact would be convenience; the bridge is the truth — `assistedBy` alone is 383,400 rows |
| **Optional third table** | drives. ~246,500 rows, and the natural home for "what did this drive end in". Build it *with* the fact, since `drive_id` is on every play and backfilling a key later is more work |
| **Size** | `cfb` goes from 235 MB to roughly **1.3 GB**. That moves `VACUUM FULL` from housekeeping to necessary — three-rewrites-per-load costs ~1 GB of bloat at that scale, not ~170 MB |

**The one part that is not purely additive:** `pbp.dim_athlete` would be rebuilt as the union
of both facts. 23,327 distinct athletes appear on scrimmage plays and 13,790 of them are
already in the dimension; it grows by roughly 29%. That is the entire reason for using ESPN
athlete ids over name strings — a receiver who also returns kicks must be *one* row, or every
cross-phase question silently double-counts him — but it **mutates a table the ST fact and
both apps already depend on**, and `primary_role` and `primary_team_id` will shift for
two-phase players. Take a rollback point first and re-run `load_athletes_2_apply.sql` for the
ST side afterwards so both facts point at the rebuilt dimension.

Four open decisions are listed in `PLAN.md` §10j. The one that is a UI decision rather than a
data one: both apps are built around a single `play` table in the snapshot, so a second fact
means either a second snapshot table or a union view.

On naming: the schema is called `st` because special teams was all it held, and a scrimmage
fact makes that a misnomer. Renaming is one statement plus a sweep of every script and SQL
file — mechanical but wide. **Keep `st` and accept the misnomer**; a rename touches working
code for cosmetic gain.

### Two live follow-ups on the explorer

Both are the user's words and both are currently unstarted:

1. **Reconsider PATs for the explorer.** Part of why conversions were excluded was that they
   had no usable kicker identity — `kicker_athlete_id` was NULL on all 58,535 of them. That
   was fixed on 2026-08-31 and they now link at 98.8%, so the original reason no longer
   holds. The other reason — that PATs are ~67k attempts at one distance and would drown any
   distance-based view — still does.
2. **The baselines.** The explorer deliberately carries no models, and that was framed as
   provisional ("for now"), making it the likeliest of these decisions to revisit. Carrying
   `fg_exp` / `punt_exp` across would give it "above expected" leaderboards; not carrying
   them is what lets every number on the page trace directly to a column with nothing to
   calibrate or defend.

### An MCP server

The original Phase 6 was "a read-only MCP server over the schema, and/or a Streamlit view".
The Streamlit view was built; the MCP server was not. The pattern is already proven elsewhere
on this machine against a different warehouse, so this is a small job whenever it is wanted.

---

## Conventions

The rules this codebase actually follows. Most exist because breaking one produced a wrong
answer that did not look wrong.

**On the data**

1. **`play_text` is never dropped, never cleaned, never repaired.** It is the audit trail and
   the reason the parser half is re-derivable. Every app can show it; the explorer's detail
   drawer does.
2. **Group by `athlete_id`, label with `known_name`.** Never the reverse. 109 names are shared
   by more than one athlete and that is *correct* — they are different people.
3. **Join conference on `(team_id, season)`.** Always. The snapshot has it pre-resolved so a
   query cannot get it wrong; if you are writing SQL against Postgres, this is on you.
4. **NULL means something here — do not `coalesce` it away.** `fg_made IS NULL` is a negated
   kick. `returned IS NULL` is an unstated outcome (within punts and kickoffs). `return_yds
   IS NULL` on a touchback is deliberate. A `coalesce(flag, false)` re-creates precisely the
   defect that was fixed on 2026-08-31.
5. **Keep a defect visible rather than cosmetically repairing it.** The mangled-name guard,
   the impossible-value flags, the `Unknown` outcome slice and `parse_confidence` all exist
   because a silent repair hides a real upstream bug. The explorer's decision not to strip
   `(09:34) #98` for display was vindicated within a day.
6. **A new value's meaning goes in the parser docstring**, next to the rule that produces it.
   `st_parser.py`'s header is the specification for what the flags mean; it is long on
   purpose.

**On the pipeline**

7. **Run everything from the repository root.** `load_dims.sql` and friends resolve CSV paths
   relative to the caller.
8. **`SEASONS` is set in three places that must agree** — `build_table.py`, `build_dims.py`,
   `fetch_participants.py`. `fetch_espn.py` reads its own from the environment. Changing the
   window means changing all four.
9. **A destructive load writes its rollback table first, and refuses rather than warns.**
   `reparse.sql` and `load_athletes_2_apply.sql` both snapshot the columns they are about to
   change and both abort if staging and the fact table disagree about which plays exist. A
   mismatch is the wrong operation, not a caution.
10. **Build a candidate CSV to a scratch path and diff it before it goes near Postgres.**
    `build_table.py --out /tmp/new.csv` exists for this.
11. **Reach for `--dry-run` on the in-season update.** It fetches, builds every CSV, reports
    what would change, and stops.
12. **Verify after any load, and read the console's Validation tab rather than the pooled
    rate.** Tolerances are shares and status is judged on the worst single season, because a
    defect living in one season disappears when thirteen are averaged.

**On the apps**

13. **Neither app touches Postgres.** If a new surface needs a column, add it to
    `build_snapshot.py` and rebuild the snapshot — do not open a second connection.
14. **Derived columns are computed once, in the snapshot.** `game_secs_remaining`,
    `is_clutch`, `fg_dist_bucket`, `fbs_vs_fbs` are there so no two queries can disagree
    about their definition.
15. **Both baselines are fit on the whole corpus and exclude any in-progress season.** "Above
    expected" needs a fixed yardstick; a filtered fit moves the goalposts along with the
    players, and a part-season would tug the decade-long trend every kicker is measured
    against.
16. **A 100% stacked bar carries its denominator.** A 67/33 split on three attempts otherwise
    reads as confidently as one on three thousand. The explorer enforces this; so should
    anything new.

**Regenerating the figures in this document.** Every number above came from the snapshot at
`data/out/pbp.duckdb`, and the queries are the ones in [Query recipes](#query-recipes) plus
`sql/verify.sql` and `sql/verify_phase4.sql`. `snapshot_meta` records when the snapshot was
built and how many rows it holds; `season_status` records what was finished at that moment.
If a figure here disagrees with the database, the database is right and this file is stale —
say so, and fix the file.
