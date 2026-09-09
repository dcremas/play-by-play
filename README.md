# D-I FBS Play-by-Play

Every play in FBS college football from 2014 to the game played last weekend — one row per
play, with the situation it happened in, the venue, the people identified by stable ESPN id
rather than name string, and the raw source text kept alongside so any row can be audited
back to what the feed actually said.

**1,827,076 plays** over **10,301 games** and **13 seasons**, split across two fact tables
that are disjoint by construction:

| fact | rows | what it holds |
|---|---|---|
| `pbp.special_teams_play` | 316,397 | kickoffs, punts, field goals, and the conversion family |
| `pbp.scrimmage_play` | 1,510,679 | rushes, passes, sacks, penalties |

Built from ESPN play-by-play and nothing else. Stored in local Postgres, read through a
DuckDB snapshot by two applications.

> **The name.** This repository was `cfb-special-teams` and the schema was `st` until
> 2026-09-08, when the scrimmage fact was added and both became misnomers. The special-teams
> half is not deprecated — it is the older, more heavily parsed, and better validated of the
> two, and most of this document is still about it.

This file is the whole project. It is written to be read cold, after time away, and it is
complete on its own — `PLAN.md` (the original design record, with the parser's answer-key
agreement tables and the scrimmage design in §10) and `web/README.md` (the explorer's
implementation notes) are kept, but nothing here depends on them.

| | |
|---|---|
| **Grain** | one row per play. Kicks: `kickoff` \| `punt` \| `field_goal` \| `pat` \| `two_point` \| `defensive_conversion`. Scrimmage: `rush` \| `pass` \| `sack` \| `penalty` \| `other` |
| **Window** | 2014–2026. 2026 is in progress and flagged as such |
| **Source** | ESPN site API (play text, venue) + ESPN core API (per-play athlete ids, athlete identity). No API key, no other feed |
| **Warehouse** | PostgreSQL 18.6, database `cfb`, schema `pbp`, 11 tables + rollback copies, 1.34 GB |
| **Read path** | `data/out/pbp.duckdb` — wide `play` and `scrimmage` tables plus `drive`, 251 MB, rebuilt from Postgres in one command |
| **Parse quality** | kicks: 98.51% of rows `exact`. Scrimmage needs no parser — `statYardage` and `down` are structured fields at 100% coverage |
| **People** | one shared `dim_athlete` of 62,879 athletes, 100% named and positioned from ESPN; 29,819 appear in both facts. 98.7% of kicks carry a kicker id |
| **Apps** | Dash instance explorer · Excel reports · one-page ERD. Both apps are special-teams-only by choice — see [Not built](#not-built) |

Does it behave like football? These come out of the data, not out of a reference book:

| | corpus | 2014 | 2018 | 2025 | reality check |
|---|---|---|---|---|---|
| Field goal % | 74.3 | 71.8 | 72.7 | 76.4 | rises across the decade, as PAT% does |
| Punt gross (yd) | 42.04 | 41.30 | 41.52 | 43.19 | FBS is ~41–43 |
| Kickoff touchback % | 50.9 | 38.2 | **51.5** | 59.6 | steps in 2018, the year the fair-catch rule landed |
| PAT % | 97.34 | **98.49** | 96.61 | 98.54 | 2014 is a source defect, not a trend — see [Known limits](#known-limits) |
| FG % by distance | 93.6 (20–24) → 48.5 (50–54) → 36.6 (60+) | | | | monotonic decline over 30k attempts |
| Completion % | 60.1 | 58.5 | 59.3 | 61.6 | the real, documented rise in passing efficiency |
| Yards per carry | 5.14 | | | | FBS is ~4.5–5.5 |
| Yards per pass attempt | 7.60 | | | | |
| First-down rate by down | 28% (1st) → 33% (2nd) → 42% (3rd) → 55% (4th) | | | | 4th down is selected-for, hence highest |
| Drive TD% by start | 51.9% (inside opp 20) → 36.3% (midfield) → 20.1% (own 10) | | | | monotonic in field position |

The 2018 touchback step and the two monotonic curves are the checks that matter most,
because none was targeted: a distance field that was silently wrong would not produce the
field-goal curve, a touchback flag reading the wrong thing would not step on the exact
season the rule changed, and a drive table with its field position reversed would produce
the touchdown curve backwards.

---

## Contents

- [Orientation](#orientation) — what exists, where it lives, how to start it
- [What a row is](#what-a-row-is) — grain, corpus shape, what is in and out of scope
- [Where the data comes from](#where-the-data-comes-from) — the single source, the four dialects, the parser (kicks only)
- [Identity](#identity) — athlete ids, derived names, conference realignment
- [Data dictionary](#data-dictionary) — every column of every table, both facts
- [Known limits](#known-limits) — read this before quoting a number
- [The pipeline](#the-pipeline) — the DAG, what owns what
- [Runbooks](#runbooks) — the nine procedures, and which loader is right when
- [The applications](#the-applications) — explorer, reports, ERD
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
│                      §10 scrimmage design + decisions
├── README.md          this file
├── requirements.txt   pinned; Python 3.14 required (see below)
│
├── scripts/           the pipeline, in dependency order
│   ├── fetch_espn.py          scoreboard + game summaries  -> data/espn/
│   ├── fetch_participants.py  per-play athlete ids         -> data/espn/participants/
│   ├── fetch_athletes.py      athlete name/position/jersey -> data/espn/athletes.json.gz
│   ├── st_parser.py           the play-text parser. 497 lines, four dialects, no deps.
│   │                          KICKS ONLY -- the scrimmage fact does not use it
│   ├── build_table.py         ESPN JSON + parser           -> data/out/st_plays.csv
│   ├── build_scrimmage.py     ESPN JSON, no parser         -> scrimmage_plays.csv,
│   │                                                          scrimmage_athlete.csv, drives.csv
│   ├── build_dims.py          conf | venue | athlete       -> the dimension CSVs
│   ├── build_snapshot.py      Postgres                     -> data/out/pbp.duckdb
│   ├── update_season.py       the weekly in-season driver; calls all of the above
│   └── build_erd.py           live Postgres                -> reports/cfb_pbp_erd.pdf (2 sheets)
│
├── sql/               DDL and loaders. Numbered files run in order
│   ├── schema.sql  dims.sql  enrich.sql          DDL (special teams)
│   ├── schema_scrimmage.sql  schema_drive.sql    DDL (scrimmage fact, bridge, drives)
│   ├── schema_dim_athlete.sql                    DDL (the shared athlete dimension)
│   ├── load_1_stage.sql  load_2_insert.sql       ST backfill loader (TRUNCATE + INSERT)
│   ├── load_3_season.sql                         ST in-season loader (one season, atomic)
│   ├── load_scrimmage_{1_stage,2_insert}.sql     scrimmage backfill loader
│   ├── load_bridge_drive_{1_stage,2_insert}.sql  bridge + drives backfill loader
│   ├── load_scrimmage_3_season.sql               scrimmage in-season loader
│   ├── reparse.sql                               re-derive parser columns in place
│   ├── load_dims.sql                             the five dimension CSVs, FK order
│   ├── load_athletes_{1_stage,2_apply,3_season}.sql   athlete link: backfill / in-season
│   ├── enrich_game_context.sql                   the three columns the loaders leave NULL
│   └── verify.sql  verify_phase4.sql             assertion suites
│
├── web/               Dash instance explorer, over the snapshot
├── reports/           formatted Excel workbooks + the ERD output
│
└── data/              ~1 GB, all of it re-derivable from ESPN
    ├── espn/
    │   ├── games_<season>.json          13 files, the completed-game lists
    │   ├── summaries/<game_id>.json.gz  10,470 files, 182 MB — play text lives here
    │   ├── participants/<game_id>.json.gz  10,470 files, 41 MB — athlete ids
    │   ├── athletes.json.gz              1.7 MB, 62,872 athlete identities
    │   └── participants_pre20260831/    454 files kept from before the key fix
    └── out/
        ├── st_plays.csv                 the full special-teams extract
        ├── scrimmage_plays.csv          382 MB, the full scrimmage extract
        ├── scrimmage_athlete.csv  drives.csv
        ├── *_2026.csv                   the current in-progress season, per artifact
        ├── dim_*.csv  fact_game.csv  play_athlete*.csv
        ├── pbp.duckdb                   251 MB — what every app reads
        └── fg_by_distance.xlsx
```

### First run

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # first time only
.venv/bin/python scripts/build_snapshot.py                          # Postgres -> DuckDB
.venv/bin/python -m web.app                                         # the explorer, :8060
```

**Python 3.14 is required, and not by choice.** The pyenv 3.12 and 3.13 builds on this
machine were compiled without `blake2`, which breaks `hashlib` and therefore pip. The venv
runs 3.14.7.

Nothing in either app talks to Postgres at runtime. They open `data/out/pbp.duckdb`
read-only, so the explorer starts cold in about a second, any number of readers can share
the file without contending for it, and both keep working when the database is down. The
explorer's header shows how old the snapshot is.

### State of play

| | |
|---|---|
| **Built and trusted** | fetch → parse → load → enrich → snapshot for BOTH facts; both applications; the weekly in-season update; the ERD |
| **In progress right now** | the 2026 season, 99 games deep. `scripts/update_season.py 2026` pulls both facts forward |
| **Rollback tables in Postgres** | none. The four from the expansion were dropped on 2026-09-08 once the coverage was trusted; `reparse.sql` and `load_athletes_2_apply.sql` each recreate the one they own the next time they run |
| **Deferred by decision** | weather (Phase 5 — tabled, everything needed to start is in place); derived player stat lines and team box scores (both are `GROUP BY`s over the facts and need no reload); the apps reading the scrimmage fact |
| **Open follow-ups** | Reconsider PATs for the explorer now that they link at 98.8%; decide whether anything refits the two baselines that went with the console on 2026-09-09; 38 conversions on return touchdowns sit on the wrong team ([Known limits](#known-limits) §9). All in [Not built](#not-built) |

---

## What a row is

One row per **play**, in two tables. The split is not by convenience — it is where the data
itself changes character:

| | `pbp.special_teams_play` | `pbp.scrimmage_play` |
|---|---|---|
| rows | 316,397 | 1,510,679 |
| how the outcome is known | **parsed out of prose.** A kick's result exists nowhere but the play text, which is why `st_parser.py` is 497 lines of regex over four dialects | **read off structured fields.** `statYardage`, `down`, `distance`, `isTurnover` and `scoringPlay` are 100% populated. No parser is involved |
| built by | `build_table.py` | `build_scrimmage.py` |
| `play_kind` | `kickoff` `punt` `field_goal` `pat` `two_point` `defensive_conversion` | `rush` `pass` `sack` `penalty` `other` |

**They are disjoint by construction, not by convention.** `build_scrimmage.py` excludes
whatever `build_table.classify()` claims by *calling that function*, rather than
re-listing the play types it matches. That matters: 545 special-teams plays hide under
`playType='Penalty'` and are rescued by its text-hint table, so a type whitelist in the
second builder would have put them in both facts. `play_uid` is unique across both tables —
1,827,076 rows, 1,827,076 distinct ids — so the two `UNION` cleanly for whole-game questions.

Within special teams it is still one table rather than three, because the situational
context (down, distance, field position, score, clock, venue) is identical across kick types
and one table makes "kicks versus punts in the same conditions" a `WHERE` rather than a
`UNION`. Type-specific columns are nullable by kind.

| `play_kind` | rows | share | notes |
|---|---|---|---|
| `kickoff` | 114,396 | 36.2% | includes ~1,486 onside kicks, flagged separately |
| `punt` | 99,081 | 31.3% | includes blocked |
| `field_goal` | 31,460 | 9.9% | includes blocked, missed and penalty-negated (`fg_made IS NULL`) attempts |
| `pat` | 67,678 | 21.4% | extra points, **mostly derived from touchdown text** — see below |
| `two_point` | 3,707 | 1.2% | `two_point_type` is `pass` or `rush` where the text says |
| `defensive_conversion` | 75 | 0.02% | the **defence** returning a blocked PAT for two points — see below |

| `play_kind` | rows | share |
|---|---|---|
| `rush` | 730,551 | 48.4% |
| `pass` | 644,405 | 42.7% |
| `penalty` | 93,438 | 6.2% |
| `sack` | 39,917 | 2.6% |
| `other` | 2,368 | 0.2% |

**Conversions are second-class rows by construction, and it matters.** ESPN usually does not
emit an extra point as its own play: it folds it into the touchdown text
(`"Derrick Henry 37 Yd Run (Adam Griffith Kick)"`). `build_table.emit_pat` lifts it out and
writes a *second* row off the scoring play with `:pat` appended to the `play_uid`. So one
ESPN play can feed two fact rows — a kickoff-return touchdown produces both a `kickoff` row
and a `pat` row — and anything joining the participants feed has to route roles to the right
one. `build_dims.stage_athlete` does exactly that; it is the reason conversions link at all.

A minority of conversions *do* get their own play row, and those are read directly rather
than derived (`build_table.STANDALONE_CONV`). 48 of them are two-point attempts, almost all
in overtime. The other 75 are `defensive_conversion`, which is a different event entirely:
the defence returning a blocked or failed PAT for two points, emitted after the touchdown.
The touchdown has already produced a `pat` row recording the block; this row records who
scored off it. Filing them as `two_point` would claim the offence attempted something it did
not.

**`play_kind` on the scrimmage side is derived, not copied.** ESPN's play type names the most
notable *event*, not the play that was called — a rush that ended in a fumble is typed
`Fumble Recovery (Own)`. Typing off that column alone would take ~33,000 rushes and passes
out of their own populations and file them under their outcome. Where the type names an
outcome, the snap is recovered from the participant roles instead: a `passer` role means a
pass was thrown. That settles 33,122 plays; the 2,365 it cannot settle become `other` rather
than a guess. `play_type_espn` keeps ESPN's raw label on every row so the call is auditable.

### Corpus shape

**Special teams**

| season | kickoff | punt | FG | PAT | 2-pt | def conv | ST total | games |
|---|---|---|---|---|---|---|---|---|
| 2014 | 9,533 | 8,858 | 2,667 | 5,759 | 158 | 4 | 26,979 | 851 |
| 2015 | 9,850 | 8,995 | 2,708 | 5,962 | 256 | 6 | 27,777 | 863 |
| 2016 | 9,748 | 8,835 | 2,553 | 5,976 | 276 | 14 | 27,402 | 856 |
| 2017 | 9,623 | 8,918 | 2,643 | 5,855 | 248 | 10 | 27,297 | 869 |
| 2018 | 9,917 | 8,786 | 2,591 | 6,078 | 303 | 8 | 27,683 | 881 |
| 2019 | 9,845 | 8,554 | 2,698 | 5,902 | 288 | 6 | 27,293 | 887 |
| 2020 | 6,316 | 5,074 | 1,719 | 3,832 | 261 | 2 | 17,204 | 563 |
| 2021 | 9,455 | 7,858 | 2,577 | 5,481 | 290 | 5 | 25,666 | 839 |
| 2022 | 9,416 | 8,301 | 2,576 | 5,420 | 371 | 4 | 26,088 | 857 |
| 2023 | 9,696 | 8,110 | 2,738 | 5,596 | 394 | 5 | 26,539 | 903 |
| 2024 | 9,843 | 7,795 | 2,821 | 5,545 | 469 | 5 | 26,478 | 901 |
| 2025 | 10,086 | 8,116 | 2,900 | 5,628 | 375 | 5 | 27,110 | 932 |
| 2026 | 1,068 | 881 | 269 | 644 | 18 | 1 | 2,881 | 99 |

**Scrimmage**

| season | rush | pass | sack | penalty | scrimmage total |
|---|---|---|---|---|---|
| 2014 | 63,066 | 55,048 | 3,383 | 7,333 | 128,994 |
| 2015 | 64,948 | 55,412 | 3,299 | 7,528 | 131,374 |
| 2016 | 64,067 | 54,277 | 3,376 | 7,476 | 129,390 |
| 2017 | 63,639 | 54,005 | 3,363 | 7,516 | 128,700 |
| 2018 | 64,701 | 55,014 | 3,493 | 7,754 | 131,158 |
| 2019 | 63,237 | 55,257 | 3,558 | 7,883 | 130,089 |
| 2020 | 40,507 | 35,625 | 2,311 | 5,076 | 83,624 |
| 2021 | 58,143 | 52,343 | 3,400 | 8,322 | 122,428 |
| 2022 | 58,214 | 54,102 | 3,441 | 8,072 | 124,131 |
| 2023 | 59,880 | 55,060 | 3,454 | 8,140 | 126,803 |
| 2024 | 60,276 | 55,042 | 3,225 | 8,790 | 127,515 |
| 2025 | 63,164 | 57,434 | 3,305 | 8,625 | 132,718 |
| 2026 | 6,709 | 5,786 | 309 | 923 | 13,755 |

2020 is a COVID-shortened season — 563 games against ~880 either side. It is flagged
nowhere and deleted nowhere; any per-season rate that treats it as a normal year is
comparing a two-thirds season to full ones.

2026 is in progress. `season_status` in the snapshot marks it, on a self-maintaining rule:
a season is in progress while its most recent game is inside 30 days, so nothing has to be
unset in January.

### Scope decisions, made once and still standing

- **Every game with at least one FBS team is ingested**, FBS-vs-FCS included, and filtered
  at query time rather than at ingest. 274,629 rows are FBS-vs-FBS; 38,954 are not. The
  snapshot carries `fbs_vs_fbs` precomputed, so either cut is one `WHERE` — and
  kicker-quality work almost always wants the FBS-only one.
- **Extra points and two-point tries are in the table.** They are placekicks, they are
  71,385 rows, and they are cheap to carry. The Dash explorer excludes them from its own
  scope; the table does not.
- **169 of 10,470 games have no play-by-play at all** (1.6%), because ESPN carries none for
  them. Mostly FBS-vs-FCS. They are in `fact_game` and absent from both fact tables, which
  is why `count(DISTINCT game_id)` on plays is 10,301, not 10,470. The two facts agree
  exactly on which games those are.
- **Penalty plays are in the scrimmage fact**, 93,438 of them, as `play_kind='penalty'`
  rather than in a sibling table. A penalty is a real down-consuming event; filter at query
  time, matching the decision above.
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
   keeps only `drives`, `gameInfo` and a trimmed `header`. 182 MB for 10,470 games instead
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

The lesson is written into the checks: `kicking = receiving` and `null kicking_team` are
both counted by `sql/verify.sql` on every run and must both come back zero, and its
per-season counts by kind would still expose a source-shaped discontinuity at the old
boundary if one ever reappeared.

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

`pbp.play_athlete` is the full-fidelity bridge — one row per (play, role, athlete), 681,464
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

### Names come from ESPN, and the derived ones are kept as a cross-check

Until 2026-09-08 `dim_athlete.known_name` was voted out of play text, on the grounds that
the participants payload has no name in it. That worked, but only reached **25.9% of
athletes** (8,794 of 33,891) — dominated by tacklers, who are usually a bare jersey number,
and by one-play athletes. The scrimmage fact would have made it worse: of 698 distinct
passers in 2024, 107 had a name.

`scripts/fetch_athletes.py` now caches ESPN's own athlete records instead. **62,872 of
62,879 athletes, 100% named and 100% with a position**, the seven misses being records ESPN
has deleted. It also brings `full_name` and `jersey`.

The voted name was **not** thrown away. It lives in `text_name` beside `text_name_confidence`,
because it is derived from a completely different source than `known_name`: play text versus
a roster record. Where the two disagree, suspect the athlete-to-play *link*, not the
spelling. A fetched name would paper straight over a bad link; two independent names cannot.

That earned its place immediately. Of the 8,794 athletes with both, 8,018 agree exactly and
776 differ — and every disagreement inspected is benign:

| `known_name` | `text_name` | what it is |
|---|---|---|
| Andy Borregales | Andres Borregales | short form |
| A.J. Reed | AJ Reed | punctuation |
| Sieh Bangura | S.Bangura | gamebook initial |
| Jalen Moreno-Cropper | Jalen Cropper | added a name mid-career |
| Robbie Chosen | Robby Anderson | legal name change |
| JuJu Smith-Schuster | JuJu Smith | legal name change |

No mislinked athlete turned up anywhere, and the largest disagreement by volume is 43 plays.

`known_name` falls back to `text_name` where the fetch has no record, so the column is
always the best available name and `text_name` tells you which regime produced it.

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
the query side by side and prints them, and the same comparison is the first entry in
[Query recipes](#query-recipes).

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

316,397 rows, 109 MB. `play_uid text PRIMARY KEY`.

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
| `play` | 316,397 | the wide special-teams fact |
| `scrimmage` | 1,510,679 | the wide scrimmage fact, dimensions and athlete names flattened on the same way |
| `drive` | 258,795 | |
| `play_athlete` | 681,464 | the special-teams bridge, copied verbatim |
| `scrimmage_athlete` | 3,135,126 | the scrimmage bridge, copied verbatim |
| `dim_athlete` | 62,879 | |
| `dim_team` | 249 | |
| `dim_team_season` | 3,368 | |
| `dim_venue` | 201 | |
| `fact_game` | 10,470 | |
| `season_status` | 13 | one row per season: games, plays, `last_regular_week`, `last_kickoff`, `is_in_progress` |
| `snapshot_meta` | 1 | `built_at`, `source_db`, `rows`, `scrimmage_rows` — so an app shows the snapshot's age rather than pretending to be live |

251 MB in total. `dim_conference` is not copied; its content is already flattened onto both
facts.

The `scrimmage` table adds a few things the Postgres fact does not carry, computed once here
so no consumer recomputes them: `passer_name` / `rusher_name` / `receiver_name` /
`tackler_name` and their positions, `game_secs_remaining`, `is_clutch`, `distance_bucket`
(short / medium / long), `field_zone` (red zone / opponent half / own half) and
`fbs_vs_fbs`.

**A note on rebuilding it.** `build_snapshot.py` sets `pg_connection_limit = 4`. DuckDB's
Postgres scanner parallelises a table read by opening one COPY stream per ctid range, up to
64 by default; at 316k rows that is fine, but at 1.5M it exhausted the machine's socket
buffers and Postgres killed the transfer with *"No buffer space available"*. The extract is
disk-bound anyway, so the cap costs nothing measurable.

### `pbp.scrimmage_play` — the second fact table

One row per rush, pass, sack, penalty or unresolved scrimmage play. Built by
`scripts/build_scrimmage.py` in 50 seconds from the summaries already on disk, with no
fetching and no parser.

| column | type | notes |
|---|---|---|
| `play_uid` | text PK | `espn:<game_id>:<sequenceNumber>`. A `#n` suffix means ESPN reused one sequence number — see [Known limits](#known-limits) |
| `play_kind` | text | `rush` \| `pass` \| `sack` \| `penalty` \| `other`. **Derived**, not a copy of `play_type_espn` |
| `play_type_espn` | text | ESPN's raw label, kept so the derivation above is auditable |
| `drive_id`, `drive_number` | text, smallint | joins `pbp.drive` |
| `down`, `distance`, `yards_to_goal` | smallint | 100% populated. Out-of-range values are NULLed, never clamped |
| `offense_team_id`, `defense_team_id` | integer | from `teamParticipants`, 99.78% populated. **Not** `start.team.id`, which on a kick is the kicking team |
| `is_home_offense`, `score_diff_offense` | boolean, smallint | margin **before** the snap |
| `yards_gained` | smallint | `statYardage`. 100% populated |
| `end_down`, `end_distance`, `end_yards_to_goal`, `end_team_id` | | the state after the play. `end_yards_to_goal` is measured from **`end_team_id`'s** perspective, so on a turnover it flips to the other goal line |
| `first_down_gained` | boolean | possession is tested first: after a turnover ESPN writes `end.down = 1` for the side that took the ball away |
| `is_complete` | boolean | pass plays only. An interception is a pass attempt that was not completed, matching NCAA completion percentage. A sack is NULL — not a pass attempt in NCAA accounting |
| `is_touchdown`, `is_turnover`, `is_penalty`, `is_scoring_play` | boolean | |
| `points_scored` | smallint | signed, from the **offence's** perspective: a pick-six is negative |
| `passer_/rusher_/receiver_/tackler_athlete_id` | bigint | the first athlete in each role. `pbp.scrimmage_athlete` is the full truth |
| `play_text` | text | provenance. Nothing is parsed out of it |
| `venue_id`, `neutral_site`, `conference_game` | | filled by `enrich_game_context.sql` **after** the load |

### `pbp.scrimmage_athlete` — the people bridge

3,135,126 rows, PK `(play_uid, role, athlete_id)`. Same shape as `pbp.play_athlete`. Every
role ESPN reports is kept:

| role | rows | | role | rows |
|---|---|---|---|---|
| `rusher` | 719,143 | | `sackedBy` | 44,986 |
| `passer` | 682,107 | | `passDefender` | 41,503 |
| `receiver` | 547,486 | | `returner` | 13,832 |
| `tackler` | 482,325 | | `forcedBy` | 7,286 |
| `assistedBy` | 388,978 | | `recoverer` | 2,805 |
| `scorer` | 70,124 | | `patPasser` | 2,744 |
| `patScorer` | 68,162 | | `fumbler` | 909 |
| `penalized` | 62,548 | | `kicker`, `punter` | 188 |

The four id columns on the fact are a denormalised hot path. This is the truth: 193,215
plays have two tacklers and 1,156 have three.

### `pbp.drive` — 258,795 drives

23.8 a game. Spans **both** facts — a drive that ends in a punt contains the punt — so
`plays_total` counts every play in the drive and `plays_scrimmage` only those that reached
`pbp.scrimmage_play`.

| column | notes |
|---|---|
| `drive_uid` | PK, `espn:<game_id>:d<n>`. `drive_id` is ESPN's own and is what `scrimmage_play.drive_id` joins |
| `result` | `PUNT` 36.8% · `TD` 26.4% · `FG` 8.8% · `DOWNS` 6.2% · `INT` 5.9% · `FUMBLE` 4.3% |
| `start_yards_to_goal` | taken from the **first play's** `start.yardsToEndzone`, not `drive.start.yardLine`. That ESPN column is measured in a fixed direction rather than from the possessing team's own goal, so it reads 25 for one team's own 25 and 76 for the other's own 24 |
| `offensive_plays` | ESPN's own count, kept beside ours |

### The dimensions and the bridge

| table | rows | grain | columns |
|---|---|---|---|
| `pbp.fact_game` | 10,470 | one game | `game_id` PK · `season` · `week` · `season_type` · `kickoff_utc` · `home_team_id` · `away_team_id` · `venue_id` **(the only declared FK in the schema)** · `attendance` · `neutral_site` · `conference_game` |
| `pbp.play_athlete` | 681,464 | play × role × athlete | `play_uid` · `role` · `athlete_id` · `ordinal`; PK on the first three |
| `pbp.dim_athlete` | 62,879 | one athlete, **career, across BOTH facts** | `athlete_id` PK · `known_name` (ESPN) · `full_name` · `position` · `jersey` · `text_name` · `text_name_confidence` · `primary_role` · `primary_team_id` · `first_season` · `last_season` · `st_plays` · `scrimmage_plays` |
| `pbp.scrimmage_athlete` | 3,135,126 | play × role × athlete | same shape as `play_athlete`; see its own section above |
| `pbp.drive` | 258,795 | one drive | see its own section above |
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
The explorer makes `Unknown` a first-class, filterable outcome value with its own stat
tile, painted in muted ink rather than a categorical hue so it reads as *absence*. Treat any
season above ~12% as one whose denominator has to be quoted alongside the rate.

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

Whether that matters depends on what is reading it:

- **Anything fitted has to hold it out.** The retired `fg_exp` baseline entered season
  *linearly*, so a 2026 twenty-one attempts deep would have tugged the whole decade-long
  quality trend and every kicker in every season would have been measured against it. That
  is a correctness problem, and it is what `season_status` exists for.
- **The explorer filters nothing.** It fits no models, so a part-season is a *reading*
  problem there, not a correctness one. It carries a `2026 partial` badge whose tooltip
  gives games, kicks and week, and a note above every by-season grid.

### 6. `yards_to_goal = 0` is a null sentinel

ESPN omits the start yardline on some plays and it arrives as `0`, not NULL — 1,114 rows
(0.36%), spread across all five kinds. Exclude it from any field-position analysis or it
reads as a snap on the opponent's goal line. The retired punt baseline restricted itself to
`yards_to_goal BETWEEN 20 AND 100` for exactly this reason.

### 7. `wallclock_utc` is ~67% populated in 2017, in both facts

99%+ in every other season, 96.6% overall. It blocks nothing today, but it is a real hole
in the play-level weather join if that is ever built.

### 8. 2020 is two-thirds of a season

563 games against ~880 either side. Nothing flags it. Any per-season rate that treats 2020
as a normal year is comparing a COVID-shortened season to full ones.

### 9. ESPN's score column lags a play — repaired 2026-09-08, with residue

`homeScore`/`awayScore` on a play is the score **after** it, including the PAT folded into a
touchdown's text. Verified on clean games. But the feed is not consistent about it:

| | rows |
|---|---|
| rows reporting a **stale**, pre-scoring snapshot while the clock advances past them | 6,470 |
| rows simply **out of chronological order** | 765 |
| games where the running score therefore steps backward at least once | 3,553 of 10,470 (33.9%) |

Stale rows concentrate on `Timeout` (1,649) and `Penalty` (1,663), and the backward steps
cluster at 7, 3 and 6 points — touchdown-plus-PAT, field goal, touchdown.

**The repair** is one invariant: a score never goes down. `build_table.advance_score` clamps
each team's running total to its own maximum, so a stale snapshot is absorbed into the value
already known and the play after it shows no phantom gain. Both builders call it, so they
cannot drift.

Measured over 1,868,595 plays:

| | before | after |
|---|---|---|
| negative point deltas | 7,237 | **0** |
| illegal deltas (negative, both teams, or an impossible value) | 8,458 | 561 |
| plays credited with points they did not score | 14,002 | **584** |
| reconstructed final matches the official one in `games_<season>.json` | 10,021 of 10,301 | **10,053** |
| conversion on the correct team, against game-local ground truth | 99.814% | **99.846%** |

That last row uses an independent check worth knowing about: on punts, kickoffs and field
goals the kicking team is read off `start.team.id` and never depended on the scoreboard at
all, so a kicker who also took a real kick in the same game has his team named beyond doubt
there. 59,724 conversions can be checked that way.

**Two things were tried and rejected, both on measurement.** Sorting plays into clock order
before walking adds nothing on top of the clamp — the ordering fault is the smaller half, and
a sort aggressive enough to fix it broke as many games as it repaired (fixed 175, broke 143).
Feeding the raw unclamped deltas back in as a tiebreak made things markedly worse, 99.81% →
99.56%, flipping 182 correct conversions: once `prev_*` is a repaired running maximum, a raw
negative delta is a property of the repair, not a signal about who scored.

**What still remains, and it is small but real:**

- **584 plays** carry points ESPN does not call a scoring play, and **1,085** are scoring
  plays that came out with zero points. Both are cases where the feed's own `scoringPlay`
  flag and its score column disagree, and neither can be resolved from the data.
- **240 plays** have a `points_scored` magnitude outside {1,2,3,6,7,8}.
- **38 conversions** sit on the wrong team. All are return touchdowns — kickoff, punt, fumble
  and interception returns — where the row is stale, both clamped deltas are therefore zero,
  and `emit_pat` falls back to possession, which on a return touchdown is the side that was
  scored against. Fixing them needs a return-touchdown detector, and `build_table`'s own
  docstring is on record that a play-type whitelist does not separate those reliably.
- Negative `points_scored` is **correct** on 3,513 rows and should stay: the column is signed
  from the offence's perspective, so a pick-six is genuinely negative. Every remaining
  negative is an interception or fumble return touchdown, a safety, or a sack in the end
  zone.

**`score_diff_kicking` was fixed separately, right after.** It documents "the kicking team's
margin before the play" but was computed from the after-play scoreboard, so a made field goal
carried a margin that already included the three points it had just scored. It now comes from
the repaired running score entering the play, like `scrimmage_play.score_diff_offense`.

Two checks say it landed: **22,562 of 23,220 made field goals moved by exactly −3**, and
**7,813 of 8,044 missed field goals did not move at all** — a miss scores nothing, so its
margin must not change. The remainder in each case are rows the score-lag repair also touched.

Conversion rows needed their own rule. A `pat` row is DERIVED from the touchdown play, so
"before the play" would be the margin before the *touchdown* — not what the kicker faced. It
is instead the margin after the touchdown and before his own kick, computed as the score
after the whole play minus what the conversion itself was worth.

The one downstream consumer is `is_clutch` in the snapshot, which tests `abs(...) <= 8`. It
flips on **662 of 316,397 rows** — mostly field goals that put a team up by ten, previously
outside the one-score window and now inside it, and the reverse. No measured column moved:
field goal percentage is 74.27% before and after.

### 10. ESPN reuses one sequenceNumber for two different plays — 507 rows carry a `#n` suffix

`play_uid` is `espn:<game_id>:<sequenceNumber>`, and ESPN does not guarantee that
sequenceNumber is unique within a game. 281 collisions sit inside the scrimmage set, and 131
more put a scrimmage play on the same number as a kick. Two from 2024, unmistakably
different plays:

```
401643714 / 105292723   "Emmett Brown pass complete to Nick Nash for 7 yds"
                        "Floyd Chalk IV run for 6 yds"
401644776 / 101988002   a 9-yard run, and a 2-yard loss
```

Both builders used to **silently drop** the second play. They now suffix it — 464 rows in
`scrimmage_play`, 43 in `special_teams_play`, all findable with `play_uid LIKE '%#%'`. The
43 on the special-teams side are plays that had been missing from the warehouse entirely
until 2026-09-08.

Two consequences worth knowing:

- **Participants are keyed on sequenceNumber**, so both rows of a collision inherit the same
  athletes and at most one of them is right. 507 rows out of 1.83M.
- The two builders coordinate their numbering. `build_scrimmage` counts how many uids
  `build_table` will consume for each sequence number and starts after them; without that
  they both numbered from `#2` and collided with each other, which is how
  `espn:401752915:116#2` was briefly both a punt and a rush.

### 11. Remaining defects the build chose to keep visible

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
 dim_venue.csv         st_plays.csv                                    │
 dim_team.csv              │                                        │
 fact_game.csv             │   build_scrimmage.py  (no parser)      │
     │                     │        │                               │
     │                     │        v                               │
     │                     │   scrimmage_plays.csv                  │
     │                     │   scrimmage_athlete.csv                │
     │                     │   drives.csv                           │
     │                     │        │                               │
     │                     └────────┴──> build_dims.py athlete <────┘
     │                                        ^        │
     │                          athletes.json.gz       v      ids + names from ESPN;
     │                        (fetch_athletes.py)  dim_athlete.csv   text_name from st_plays
     │                                             play_athlete.csv
     │                                             play_athlete_wide.csv
     └──────────────┬──────────────────────────────────┘
                    │  sql/load_*.sql — \copy into all-text staging, then a typed INSERT
                    v
 ┌────────────────────────────────────────────────────────────────────────┐
 │ PostgreSQL  cfb.pbp.* — 11 tables, 1.34 GB, the system of record       │
 │ special_teams_play · play_athlete      (kicks)                         │
 │ scrimmage_play · scrimmage_athlete     (everything else)               │
 │ drive · fact_game                      (span both)                     │
 │ dim_athlete · dim_team_season · dim_team · dim_venue · dim_conference  │
 └────────────────────────────────────────────────────────────────────────┘
                    │  build_snapshot.py — DuckDB postgres extension,
                    │  read-only ATTACH, one CREATE TABLE AS per table
                    v
             data/out/pbp.duckdb  (251 MB)
                    │
          ┌────────┴────────┐
          v                 v
     web/app.py         reports/
    Dash explorer      xlsxwriter
        :8060           workbooks
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
| venues, teams, games | `build_dims.py venue` | every stored summary on disk | `dim_venue.csv`, `dim_team.csv`, `fact_game.csv` | the slow local stage — re-reads all 10,470 summaries |
| athlete identity | `fetch_athletes.py` | core-API `/athletes/<id>` | `athletes.json.gz` | 6 workers, ~20 min once, then incremental. **Do not raise it** — see runbook C3 |
| the kicks fact | `build_table.py` | summaries + `st_parser` | `st_plays.csv` — 40 columns, 80 MB | a few minutes for the full window |
| the scrimmage fact | `build_scrimmage.py` | summaries + participants, **no parser** | `scrimmage_plays.csv`, `scrimmage_athlete.csv`, `drives.csv` | 50 s for the full window, 8 workers |
| the athlete link | `build_dims.py athlete` | participants + both fact extracts + `athletes.json.gz` | `dim_athlete.csv`, `play_athlete.csv`, `play_athlete_wide.csv` | reads all participants files; the scrimmage half is rolled up in DuckDB |
| the snapshot | `build_snapshot.py` | Postgres | `pbp.duckdb` | ~15 s |

Two properties hold across every stage and are worth relying on:

- **Every fetch stage skips what it already has**, so re-running is cheap and an interrupted
  run is fixed by running it again. Refresh flags exist to override that for a live season.
- **The parser half is re-derivable from disk.** `play_text` is on every row, and every
  summary is on disk, so a parser change needs no re-fetching at all.
- **The scrimmage fact needs no parser to re-derive at all** — it is a projection of
  structured fields. 50 seconds from cold.

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
| `scrimmage_plays_<season>.csv`, `scrimmage_athlete_<season>.csv`, `drives_<season>.csv` | season | `build_scrimmage.py --seasons` |
| `pbp.special_teams_play`, `pbp.play_athlete`, the three id columns | season | `DELETE` + `INSERT` on that season only; earlier seasons are provably untouched, and both loaders print every season's coverage so you can see they did not move |
| `pbp.scrimmage_play`, `pbp.scrimmage_athlete`, `pbp.drive` | season | `sql/load_scrimmage_3_season.sql`, same pattern, enrichment in the same transaction |
| `data/espn/athletes.json.gz` | **global, incremental** | `fetch_athletes.py` only pulls ids it has never seen, so a weekly run is a few hundred calls |
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
- **Both athlete loaders refuse to run on EMPTY staging** — added 2026-09-08 after it went
  wrong. Every other guard passes vacuously on an empty set (no orphans, no strays), so
  `TRUNCATE pbp.dim_athlete` emptied the dimension, the bridge `DELETE` removed the season's
  rows, the apply put nothing back, and psql exited 0. One mistyped `\copy` path is enough
  to trigger it, which is exactly how it was found.

### B. Re-derive after a parser change

`play_text` is on every row, so no re-fetching is needed.

```bash
.venv/bin/python scripts/build_table.py --out /tmp/new.csv    # diff before going near Postgres
.venv/bin/python scripts/build_table.py                       # -> data/out/st_plays.csv
psql -d cfb -f sql/load_1_stage.sql
psql -d cfb -c "\copy pbp.stg_plays FROM 'data/out/st_plays.csv' WITH (FORMAT csv, HEADER true)"
psql -d cfb -f sql/reparse.sql                                # UPDATE, not TRUNCATE
.venv/bin/python scripts/build_snapshot.py
psql -d cfb -f sql/verify.sql                                 # read it season by season
```

`reparse.sql` updates the **25 columns the loader derives** — the 22 from the parser plus
`kicking_team_id`, `receiving_team_id` and `is_home_kicking`. Those last three are not
parser output but they *are* derived (`emit_pat` picks the conversion's team off the
scoreboard), so leaving them out would load a corrected CSV and change nothing. Everything
else — the athlete ids, the situation, `play_text` — is left alone. Pre-update values go to
`pbp.special_teams_play_prereparse`; drop that table once you are satisfied.

Judge the result **season by season, not on the pooled rate** — which is what `verify.sql`
prints counts by season and kind for. The 2025 punt gap was findable because it was 46% of
that season; it is invisible pooled, where it is 4.6% of the decade.

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

### C2. Rebuild the scrimmage fact, bridge and drives (backfill)

All three come out of one pass over the summaries, so there is one build command. 50
seconds to build, about a minute to load.

```bash
.venv/bin/python scripts/build_scrimmage.py              # -> 3 CSVs in data/out/
psql -d cfb -f sql/schema_scrimmage.sql                  # DROPs and recreates fact + bridge
psql -d cfb -f sql/schema_drive.sql
psql -d cfb -f sql/load_scrimmage_1_stage.sql
psql -d cfb -c "\copy pbp.stg_scrimmage FROM 'data/out/scrimmage_plays.csv' WITH (FORMAT csv, HEADER true)"
psql -d cfb -f sql/load_scrimmage_2_insert.sql
psql -d cfb -f sql/load_bridge_drive_1_stage.sql
psql -d cfb -c "\copy pbp.stg_scrimmage_athlete FROM 'data/out/scrimmage_athlete.csv' WITH (FORMAT csv, HEADER true)"
psql -d cfb -c "\copy pbp.stg_drive FROM 'data/out/drives.csv' WITH (FORMAT csv, HEADER true)"
psql -d cfb -f sql/load_bridge_drive_2_insert.sql
psql -d cfb -f sql/enrich_game_context.sql               # REQUIRED — covers both facts
# then runbook D (the athlete link, which spans both facts), then build_snapshot.py
```

`schema_scrimmage.sql` **drops the fact table**, so this is a backfill, not a top-up. For one
season use `sql/load_scrimmage_3_season.sql` instead — or just run `update_season.py`, which
does it.

Two checks worth running afterwards:

```sql
-- play_uid must be unique across BOTH facts
SELECT count(*), count(DISTINCT play_uid) FROM (
  SELECT play_uid FROM pbp.scrimmage_play
  UNION ALL SELECT play_uid FROM pbp.special_teams_play) x;   -- must be equal

-- no bridge row may point at a play that is not there
SELECT count(*) FROM pbp.scrimmage_athlete b
WHERE NOT EXISTS (SELECT 1 FROM pbp.scrimmage_play p WHERE p.play_uid = b.play_uid);  -- 0
```

### C3. Refresh athlete identity

Names, positions and jerseys come from ESPN, cached in `data/espn/athletes.json.gz`. The
fetch is incremental — it only pulls ids the store has never seen — so this is a one-time
20-minute cost and a few hundred calls a week thereafter.

```bash
.venv/bin/python scripts/fetch_athletes.py               # whatever is missing
.venv/bin/python scripts/fetch_athletes.py --refresh     # everything, after a transfer window
```

**Do not raise `--workers`.** The default is 6. At 24 the endpoint 403s the whole IP after
about 20,000 athletes, and the block outlasts the run — ids that succeeded minutes earlier
start failing too. 403 is this endpoint's throttle signal, not a missing record. A 403 now
parks every thread, and the store is rewritten every 5,000 athletes so an interrupted run
resumes rather than restarting.

### D. Re-derive the athlete link

Only needed when the participants feed or `build_dims.stage_athlete` changes, or after a
`load_2_insert.sql` / `load_scrimmage_2_insert.sql` backfill. Not after an ordinary parser
edit.

`dim_athlete` is **one dimension over both facts** — 62,879 athletes, of whom 29,819 appear
in both. A receiver who also returns kicks has to be one row or every cross-phase question
double-counts him. `build_dims.py athlete` reads the special-teams extract with `--plays` and
the scrimmage extract with `--scrim-fact` / `--scrim-bridge`; both accept several files so
the in-season run can pass the frozen global CSV alongside one season.

```bash
.venv/bin/python scripts/fetch_athletes.py               # top up identity first
.venv/bin/python scripts/build_dims.py athlete           # BOTH facts -> 3 CSVs
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
so it can run any time. It took `cfb` from 612 MB to 232 MB on 2026-08-31 (dropping five
rollback tables accounted for 173 MB of that). The in-season path does not create the bloat
and does not need this.

### G. Verify

```bash
psql -d cfb -f sql/verify.sql          # counts by season/kind, football sanity, integrity
psql -d cfb -f sql/verify_phase4.sql   # athlete coverage, the conference trap, surface, orphans
```

`verify.sql` and `verify_phase4.sql` print; they do not assert, and **nothing in the repository
grades them any more** — the 20 scored assertions lived in the Streamlit console and went with
it on 2026-09-09. Read the output season by season rather than pooled. Between them they cover
row counts by season and kind (which is how a new dialect gets caught — one season's PAT count
merely looks low), the monotonic field-goal curve, punt gross, the 2018 touchback step, parse
confidence, athlete coverage on the right denominators, the team-only versus team-season
conference comparison run side by side, field goals by surface (empty if the enrichment was
skipped), and four referential-integrity counts.

### Rollback points currently in Postgres

**None.** The four the 2026-09-08 expansion created — `dim_athlete_prestage3`,
`special_teams_play_preconvfix`, `special_teams_play_preathletefix` and
`special_teams_play_prereparse` — were dropped once the new coverage was trusted, taking the
database from 1,369 MB to 1,327 MB. `reparse.sql` and `load_athletes_2_apply.sql` each
recreate the one they own the next time they run, so this list refills itself as soon as
either is used. `stg_*` tables are transient — every load script drops them on the way out.

One rollback point is still on disk rather than in Postgres:
`data/espn/participants_pre20260831/` holds 454 participants files as they were before the
join-key re-fetch.

---

## The applications

Two front ends over the same snapshot, with deliberately different jobs, plus the ERD.

### `web/` — the Dash instance explorer

```bash
.venv/bin/python -m web.app          # http://127.0.0.1:8060
```

It answers "show me the actual instances and let me take them apart". Field goals, punts
and kickoffs only — 244,937 of the 316,397 rows — and profiles for the **kicking side
only**.

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

**Two sheets since 2026-09-08, one per fact family.** Eleven tables and two forty-column
facts do not fit on one page legibly. Sheet 1 is special teams, sheet 2 is scrimmage, and the
four dimensions plus `dim_athlete` appear on **both, in the same position** — that repetition
is the point, because the two facts hang off one shared set of dimensions.

Each sheet stands alone: both carry the legend, the whole-schema inventory (with a
`·1` / `·2` / `·1,2` marker saying where each table is drawn) and their own relationship
inventory with orphan counts measured live against the data. Sheet 2's relationship panel
drops the WHAT IT MEANS column because `drive` needs the width sheet 1 gives the legend, and
a note clipped mid-word is worse than no note; sheet 1 carries the explanations.

The layout is hand-authored — fixed coordinates and hand-routed edge waypoints — so a new
table needs coordinates in `ST_ENTITIES` or `SCRIM_ENTITIES`. Two guards keep it from
drifting behind the schema: any table with no box is named on stdout and listed greyed in the
inventory, and a wide table with a column missing from its `GROUPS` layout **aborts the
render** rather than silently dropping the column off the diagram.


```bash
.venv/bin/python scripts/build_erd.py        # -> reports/cfb_pbp_erd.pdf, 2 pages (+ 2 .png proofs)
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
are already flattened onto `play`.

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
| 2026-09-08 | **Scrimmage fact built** | the warehouse held only special teams; 1.5M plays already on disk were unused | `pbp.scrimmage_play` (1,510,679), `pbp.scrimmage_athlete` (3,135,126), `pbp.drive` (258,795). No fetching and no parser: `statYardage`, `down` and `isTurnover` are structured fields at 100% coverage. Disjoint from the kicks by calling `build_table.classify()` rather than re-listing its rules |
| 2026-09-08 | **`play_kind` derived from participants, not ESPN's type** | ESPN types a play by its most notable *event*, so a rush that ended in a fumble is `Fumble Recovery (Own)`. Typing off that column would have filed ~33,000 rushes and passes under their outcome | 33,122 plays reclassified from the participant roles — a `passer` role means a pass was thrown. 2,365 unresolvable become `other` rather than a guess. `play_type_espn` keeps the raw label |
| 2026-09-08 | **Duplicate sequenceNumbers stopped being dropped** | `play_uid` is `espn:<game>:<seq>` and ESPN does not guarantee uniqueness. Both builders silently `return`ed on a collision, losing the second play. Found when a standalone conversion displaced a real kickoff | 507 rows now carry a `#n` suffix instead — **43 special-teams plays recovered that had been missing from the warehouse entirely**. The two builders coordinate numbering; before that they both started at `#2` and `espn:401752915:116#2` was briefly both a punt and a rush |
| 2026-09-08 | **Positional sentinels and feed corruption nulled** | `end.down` is `-1` on any series-ending play, and `end.yardsToEndzone` ranges to **5300** and down to **-1135** | 153,533 out-of-range values across six columns set to NULL, never clamped — a clamped value is indistinguishable from a real one |
| 2026-09-08 | **`dim_athlete` rebuilt over both facts** | one dimension per fact would duplicate the 29,819 athletes who appear in both, and re-introduce exactly the name-matching problem athlete ids exist to eliminate | 33,891 → **62,879** athletes, one row each. Special-teams link rates unchanged season by season (97.5–99.1%). `st_plays` now counts distinct plays and gains a `scrimmage_plays` sibling |
| 2026-09-08 | **Names taken from ESPN instead of play text** | voting names out of the text named only **25.9%** of athletes, and would have done worse on scrimmage — 107 of 698 passers in 2024 | `fetch_athletes.py`; **100% named, 100% with a position**. The voted name is kept in `text_name` as an independent cross-check: 8,018 of 8,794 agree exactly, and all 776 disagreements are spelling variants, gamebook initials or real name changes |
| 2026-09-08 | **Standalone conversions recovered** | `emit_pat` fires on `scoringPlay or "kick attempt" in text`, read off the *touchdown* play, so it never saw the conversions ESPN emits as their own row | 48 two-point attempts (mostly overtime) and 75 `defensive_conversion` rows — the defence returning a blocked PAT, a different event from the offence's failed try. `emit_pat` also assigned the scoring team by *swapping* two ids, which fails when `start.team.id` is NULL: all three conversions in the 9OT Illinois–Penn State game were on Penn State |
| 2026-09-08 | **Empty-staging guard on both athlete loaders** | every other guard passes vacuously on an empty set, so `TRUNCATE pbp.dim_athlete` emptied the dimension, the bridge `DELETE` removed a season, the apply put nothing back — and psql exited **0**. One mistyped `\copy` path is enough | both loaders now refuse. Found by making that exact mistake |
| 2026-09-08 | **Renamed `st` → `pbp`, repo → `cfb-pbp`** | the schema was named for special teams and now holds every play | 374 references across 29 files, matched on table names rather than the bare `st.` prefix — `app.py`, then still in the tree, did `import streamlit as st`. The DuckDB view named `st` in `web/data.py` was deliberately left alone: it still means special teams |
| 2026-09-08 | **Score-lag repaired** | ESPN's score column reports a stale, pre-scoring snapshot on 6,470 rows and is out of order on 765 more, so the running score stepped backward in 33.9% of games. Read literally that produced 7,237 negative point deltas and credited 14,002 plays with points they did not score | one invariant — a score never goes down — clamps each team's running total to its own maximum, in a helper both builders call. Negative deltas 7,237 → **0**, phantom point rows 14,002 → **584**, reconstructed finals matching the official score 10,021 → **10,053** of 10,301, conversions on the correct team 99.814% → **99.846%** against game-local ground truth. Sorting into clock order first, and using raw deltas as a tiebreak, were both tried and both measured worse |
| 2026-09-08 | **`score_diff_kicking` means what it says** | it documented "the kicking team's margin before the play" but was computed from ESPN's after-play scoreboard, so a made field goal carried a margin that already included the three points it had just scored | now taken from the repaired running score entering the play, like `score_diff_offense`. **22,562 of 23,220 made field goals moved by exactly −3**; **7,813 of 8,044 missed field goals did not move at all**, which is the control. Conversion rows get the margin the KICKER faced — after the touchdown, before his own kick — since a `pat` row is derived from the touchdown play. `is_clutch` flips on 662 of 316,397 rows; no measured column moved, FG% is 74.27% before and after |
| 2026-09-08 | **Rollback tables dropped** | four tables from the expansion, kept until the new coverage was trusted | `cfb` 1,369 MB → 1,327 MB; the schema holds exactly the eleven tables the pipeline needs |
| 2026-09-09 | **The Streamlit console was removed** | it was no longer wanted; the Dash explorer and the Excel reports cover what is still read | `app.py` deleted (1,189 lines) and five pins dropped — `streamlit`, `scikit-learn`, and the `scipy` / `matplotlib` / `pytz` sitting beside them that nothing in the repository imports. Every reference was rewritten, `PLAN.md`'s design record included. **Two capabilities went with it and are replaced nowhere.** The **20 scored rule assertions**: `verify.sql` and `verify_phase4.sql` still print the same underlying counts, but nothing grades them or judges a check on its worst single season. The **two fitted baselines** `fg_exp` / `punt_exp`: what they measured is kept in [Not built](#not-built) so a rebuild starts from a known bar rather than from scratch. |

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

### Derived stat lines and team box scores — a `GROUP BY`, not a build

Scope was deliberately stopped at play-level facts (PLAN.md §10j.5). Per-player-per-game and
per-player-per-season stat lines (passing, rushing, receiving, defence) and per-game team
totals are all aggregations over `pbp.scrimmage_play` and its bridge, and can be added later
without reloading anything.

They are not free, though, and the cost is definitional rather than technical: NCAA charges
sack yardage against **rushing**, an interception counts as a pass attempt but not a
completion, and a two-point conversion counts in neither passing nor rushing totals. Those
rules have to be written down and agreed before a leaderboard means anything. The raw
material is all present — `play_kind`, `is_complete`, `yards_gained` and the role bridge.

Before building any of it, read [Known limits §9](#9-espns-score-column-lags-a-play--repaired-2026-09-08-with-residue).
`points_scored` was repaired on 2026-09-08 and is now sound on 99.9% of plays, but the
residue is enumerated there and matters for a scoring model.

### The apps do not read the scrimmage fact

`web/` is special-teams-only, by choice rather than by obstacle (PLAN.md §10j.4). The snapshot carries `scrimmage`, `drive` and `scrimmage_athlete`, so the
data is queryable offline today; what is deferred is the *interface* decision, on the
grounds that the shape of an offensive play page is not knowable before spending time with
the data.

Two concrete decisions are waiting whenever that is picked up:

- **One explorer or two.** Widening the existing phase chips from three kicking phases to
  include rush and pass means one grid over a `UNION` view, and `web/columns.py` would have
  to cope with a rush row and a punt row sharing almost no measured fields. A parallel
  offence explorer duplicates chrome but keeps what works untouched.
- **Player pages.** They are kicking-side only today. A role-aware unified page — sections
  shown by what the athlete actually did — is what the shared `dim_athlete` was built for,
  and `position` now makes "every QB season since 2014" a query rather than a guess.

### Two live follow-ups on the explorer

Both are the user's words and both are currently unstarted:

1. **Reconsider PATs for the explorer.** Part of why conversions were excluded was that they
   had no usable kicker identity — `kicker_athlete_id` was NULL on all 58,535 of them. That
   was fixed on 2026-08-31 and they now link at 98.8%, so the original reason no longer
   holds. The other reason — that PATs are ~67k attempts at one distance and would drown any
   distance-based view — still does.
2. **The baselines — a rebuild now, not a port.** The explorer deliberately carries no
   models, and that was framed as provisional ("for now"), making it the likeliest of these
   decisions to revisit. The two baselines it would have inherited went with the Streamlit
   console on 2026-09-09, so this is a rebuild. Both were fit on the **whole corpus**, never
   on a filtered subset — "above expected" needs a fixed league yardstick or a filter moves
   the goalposts along with the players — and both excluded any in-progress season. What
   they measured, kept so a rebuild starts from a known bar:

   - **`fg_exp.p_hat`** — P(make | distance, season). Cubic spline in distance, linear in
     season, 30,932 attempts. **AUC 0.7172, Brier 0.1694, log loss 0.5116.** AUC that low was
     the ceiling, not a weak fit: distance is very nearly the only observable signal in a
     field goal. Calibration is what a leaderboard depends on, and it was good — within
     **1.2 points** in every 5-yard bucket holding 2,000+ attempts, drifting only in the two
     thin long buckets (55–59, 403 attempts, 2.4 points optimistic; 60+, 41 attempts, 1.7
     pessimistic). A "points above expected" figure built mostly on 55+ kicks was never
     readable.
   - **`punt_exp.exp_net`** — E[net | yards to goal at the snap]. 85,040 punts. **R² 0.0374,
     RMSE 10.97 yd.** Low *by construction*: one punt's net is decided by the returner, not
     by the line of scrimmage. It was a conditional mean, not a predictor, and its residual
     only meant something averaged over 100+ punts.

   Carrying no models at all is what lets every number on the explorer's pages trace
   directly to a column, with nothing to calibrate or defend.

### An MCP server

The original Phase 6 was a read-only MCP server over the schema, or a browsable view over
it. The view half was built and has since been retired; the MCP server was never started.
The pattern is already proven elsewhere on this machine against a different warehouse, so
this is a small job whenever it is wanted.

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
12. **Verify after any load, and read the result season by season rather than pooled.** A
    defect living in one season disappears when thirteen are averaged — the 2025 punt gap
    was 46% of that season and 4.6% of the decade. Judge a check on its worst single season,
    and state a tolerance as a *share* so it means the same thing on one conference-season as
    on the whole corpus.

**On the apps**

13. **Neither app touches Postgres.** If a new surface needs a column, add it to
    `build_snapshot.py` and rebuild the snapshot — do not open a second connection.
14. **Derived columns are computed once, in the snapshot.** `game_secs_remaining`,
    `is_clutch`, `fg_dist_bucket`, `fbs_vs_fbs` are there so no two queries can disagree
    about their definition.
15. **Anything fitted goes on the whole corpus and excludes any in-progress season.** No
    model ships here today; this is the rule if one comes back. "Above expected" needs a
    fixed yardstick; a filtered fit moves the goalposts along with the players, and a
    part-season would tug the decade-long trend every kicker is measured against.
16. **A 100% stacked bar carries its denominator.** A 67/33 split on three attempts otherwise
    reads as confidently as one on three thousand. The explorer enforces this; so should
    anything new.

**Regenerating the figures in this document.** Every number above came from the snapshot at
`data/out/pbp.duckdb`, and the queries are the ones in [Query recipes](#query-recipes) plus
`sql/verify.sql` and `sql/verify_phase4.sql`. `snapshot_meta` records when the snapshot was
built and how many rows it holds; `season_status` records what was finished at that moment.
If a figure here disagrees with the database, the database is right and this file is stale —
say so, and fix the file.
