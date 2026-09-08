# D-I FBS Special Teams Database — Build Plan

**Goal:** every placekick (FG + PAT), kickoff, and punt in FBS football for the last ten
seasons (2016–2025), one row per instance, with enough context attached to answer
questions nobody has bothered to ask yet.

**Status:** v2 — 2026-08-30. Source recon verified. Scope decisions made (§8). Phase 1
profiling in progress.

**Decisions (2026-08-30):**
1. **Window:** 2016–2025, built *phased* — land 2016–2021 from bulk first, evaluate, then
   decide on the 2022–2025 CFBD + parser effort.
2. **Scope:** all games with at least one FBS team, including FBS-vs-FCS. Flag
   `is_conference_game` and `opponent_division`; filter at query time, never at ingest.
3. **PATs:** in scope, as `play_kind IN ('pat','two_point')`.
4. **Home:** local Postgres first, EC2 later if it earns it.

---

## 1. Feasibility verdict

Yes, and it is a smaller job than it looks. Every probe below was actually run, not assumed.

| Check | Result |
|---|---|
| Play-level FBS data exists back to 2015 | Confirmed — ESPN scoreboard `groups=80` returns 63 FBS games for 2015-09-05 |
| Kicks/punts are typed, not just free text | Confirmed — ESPN play `type.id` 52=Punt, 53=Kickoff, 59=FG Good, 60=FG Missed |
| Per-play UTC timestamps exist | Confirmed — `wallclock` e.g. `2021-09-04T23:09:52Z` → enables **play-level** weather joins |
| Kicker identity is structured | Partly — ESPN core API has `participants[].athlete` IDs; the name columns in play text have gaps |
| Volume is manageable | Confirmed — see §2 |

## 2. Size of the thing

Measured directly from the 2021 season (840 games, 137,046 total plays):

| Event | 2021 count | ×10 seasons (est.) |
|---|---|---|
| Punts | 7,638 | ~76,000 |
| Kickoffs | 6,958 | ~70,000 |
| Field goal attempts | 2,545 (1,908 made) | ~25,000 |
| PATs / 2-pt attempts | 3,219 | ~32,000 |
| **Total special teams plays** | **~20,400** | **~200,000** |

Rare events are still well-populated across ten years: 86 onside kicks and 92 blocked
punts in 2021 alone → roughly 850 and 900 respectively over the full window.

**200k rows is nothing.** This lives comfortably in the local Postgres or on EC2 alongside
`weatherdata`. Storage is not a design constraint; data quality and parsing are.

## 3. Source evaluation

### 3a. Pre-parsed bulk archives
**Evaluated 2026-08-30 over 833,544 plays / 4,896 games, 2016-2021.** Every pre-parsed
source shares the same shape of gap, and the shape matters more than the source:

**This is the single most important finding in the project.** Field goal distance and
kicker identity — the two fields any placekick analysis is built on — are *absent* from
pre-parsed feeds (`yds_fg` 0.0% populated in all six seasons, `fg_kicker_player_name`
0.0-0.1%). So is PAT data, for five of six seasons. Punts and kickoffs are well covered;
placekicks are useless out of the box.

**But the raw `text` is present and parses cleanly.** A first-attempt regex recovers FG
distance + kicker at **99.1-99.5% every season**:

```
2016  2,492/2,512 = 99.2%      2019  2,633/2,653 = 99.2%
2017  2,578/2,601 = 99.1%      2020  1,696/1,705 = 99.5%
2018  2,532/2,556 = 99.1%      2021  2,522/2,545 = 99.1%
```

The ~20 misses per season are not random noise — they are two enumerable alternate formats:
1. Penalty variant: `"McCord, Nolan field goal attempt from 29 GOOD, clock 03:25, PENALTY MISSSTATE holding"`
2. Blocked-FG-return-TD: `"Nehemiah Pritchett 80 Yd Return of Blocked Field Goal (Anders Carlson Kick)"`

Two more patterns should take this to ~99.9%. PATs are likewise recoverable from touchdown
text — `"(Adam Griffith Kick)"` yields 5,300-5,800 per season, *far more complete than the
structured column* — though the failure formats (`"(Matt Ruffolo PAT blocked)"`,
`"(kick failed)"`) still need their own patterns.

**Other caveats found:**
- `yds_punt_return` is only ~32% populated, and `yds_kickoff_return` ~81-92%. These are
  **legitimate nulls**, not missing data — most punts are fair-caught, downed, or touchbacks.
  Worth stating explicitly so nobody "fixes" them later by imputing zeros.
- `punt_return_player_name` (~56%) is populated more often than `yds_punt_return` (~32%).
  That inconsistency is unexplained and should be reconciled during Phase 2.
- **`wallclock` is only 67.1% populated in 2017** (fine elsewhere). Play-level weather joins
  will have a real hole that season.
- Bulk archives of this era stop at 2021 and are effectively abandoned, so none of them can
  carry a pipeline that has to refresh.

### 3b. CollegeFootballData API (CFBD)
- Requires a free registered API key. Verified: unauthenticated calls return
  `401 Unauthorized ... register for your free API key`.
- Covers current seasons, so it is the **only viable source for 2022–2025**.
- Gives `play_type` ("Punt", "Field Goal Good", "Blocked Punt", "Extra Point Good", …) plus
  `play_text`, down/distance/yard line, and clean team + conference metadata.
- Kick distance, touchback flags, return yards, blocks are **not structured fields** — they
  must be regex-parsed out of `play_text`.

**Verdict:** primary source for 2022–2025. Also the source of truth for team/conference
dimension tables across realignment.

### 3c. ESPN (site + core API)
- No key, works today, back to at least 2015.
- `site.api` summary endpoint → drives → plays: type IDs, `start`/`end` yard line and
  yards-to-endzone, `statYardage`, `wallclock`, plus `gameInfo.venue` (name, city, state, zip,
  **`grass` boolean**) and `attendance`.
- `sports.core.api` plays endpoint → **`participants[]` with athlete IDs, positions, and
  per-play statistics refs.** This is the clean fix for the missing FG kicker name — an
  athlete ID, not a name string to fuzzy-match.
- **Gotcha:** ESPN does *not* emit extra points as separate plays in drive data. A PAT is
  folded into the touchdown text: `"Derrick Henry 37 Yd Run (Adam Griffith Kick)"`. PATs must
  come from CFBD's `pointAfterAttempt.*`, or be regex-lifted out of TD text.
- Undocumented/unofficial API. Rate-limit politely, cache aggressively, don't build anything
  that breaks loudly if it changes.

**Verdict:** enrichment layer — athlete IDs, venue, surface, attendance.

## 4. Recommended architecture

```
                 2016–2021                       2022–2025
          bulk archive (6 files)           CFBD API (/games, /plays)
                    |                                |
                    |         our own play_text parser (validated
                    |            against an answer key on 2016–2021)
                    +----------------+---------------+
                                     v
                          staging.raw_play  (source-tagged)
                                     v
                      transform + conform to one grain
                                     v
                        st.special_teams_play  (~200k rows)
                                     ^
                      ESPN enrichment: athlete_id, venue, surface
                                     ^
                      weather join on wallclock + venue (§7)
```

The parser is the heart of this. Building it against a **6-season labeled answer key** is
what makes the 2022–2025 half trustworthy instead of hopeful.

## 5. Phases

**Phase 0 — Decide (§8).** Nothing gets built until source strategy and scope are settled.

**Phase 1 — Land the back catalog. [DOWNLOAD + PROFILE DONE 2026-08-30]**
Six seasons of bulk archive, profiled into the completeness matrix in §3a. Confirmed counts 2016–2021: 47,619 punts, 43,262 kickoffs, 14,572 FG attempts —
**108,677 special-teams rows** before PATs are recovered from text (which will add ~32k).
Remaining: load into `staging` once the Phase 2 parser exists, since FG rows are not usable
until then.

**Phase 2 — Build and prove the parser. [COMPLETE 2026-08-30]**
`scripts/st_parser.py`, proved against the archive's parsed columns as an answer key.

*Coverage — every special-teams play now matches a known format:*

| | matched | total | |
|---|---|---|---|
| punts | 47,619 | 47,619 | **100.000%** |
| kickoffs | 43,262 | 43,262 | **100.000%** |
| field goals | 14,572 | 14,572 | **100.000%** |

*Agreement vs the archive's answer key (~105k plays).* "raw" is literal match; "net" is
after subtracting documented bugs in the answer key. Worst net field: **99.58%**; most are >=99.9%.

| field | raw | net | unexplained |
|---|---|---|---|
| `yds_punted` | 99.93% | 99.93% | 35 |
| `yds_punt_return` | 83.24% | **99.85%** | 68 |
| `punt_tb` / `punt_oob` / `kickoff_downed` / `kickoff_onside` | — | **100.00%** | 0 |
| `punt_blocked` | 99.99% | 100.00% | 2 |
| `punter_player_name` | 99.37% | 99.79% | 97 |
| `yds_kickoff` | 100.00% | 100.00% | 2 |
| `yds_kickoff_return` | 64.73% | **99.58%** | 160 |
| `kickoff_player_name` | 99.79% | 99.89% | 47 |
| `fg_made` | 99.99% | 99.99% | 2 |

**The gap between raw and net is the finding.** 20,904 disagreements were run down
individually and classified. They are not our errors — they are the answer key's:

| count | cause |
|---|---|
| 15,918 | the key records a touchback as a 25-yard (KO) / 20-yard (punt) *return* — that is resulting field position, not a return |
| 2,362 | the key reads the trailing **yard line** as return yardage: `"returns for no gain to the IowSt 19"` → it stores 19 |
| 1,567 | the key stores return **losses as positive**: `"a loss of 34 yards"` → +34 |
| 812 | feed self-conflict — the text says out-of-bounds *and* lists a return; we null it and flag `ambiguous` |
| 239 | the key keeps `N/A` / `TEAM` placeholders where no kicker is named |
| 216 | outcome flags unreliable on penalty-bearing NCAA-dialect rows; we flag `ambiguous` |
| 6 | the key truncated the player name (`"milian Schulze-Geisthovel"`) |

The ~450 genuinely unexplained rows (0.4%) are dominated by two further errors in the key:
it labels the *returner* as the punter on punt-return touchdowns, and it encodes the
40-yard out-of-bounds penalty placement as return yardage.

*What the parser recovers that the archive never had:*
- **FG distance and kicker on all 14,572 attempts** (archive: 0).
- **28,955 conversions** — 27,757 PATs + 1,198 two-point attempts — lifted out of touchdown
  text (archive: 3,224, a 9x improvement).
- Three new dialects handled: ESPN, NCAA-official, and a third
  `"field goal attempt from 26 yards NO GOOD (wide left) ... score nullified by penalty"` form.
- Two number traps that would have silently corrupted the data: `"72 Yd Return of Blocked
  Field Goal"` and `"55 Yd Punt Return"` lead with *return* yardage, not kick distance.
- Kicks wiped out by penalty now record `fg_made = NULL`, not `made`.

*Sanity check — does the output behave like football?*

```
FG accuracy by distance     94.6% (15-19) -> 34.1% (55-59), monotonic, no reversals
Punt gross average          41.24 -> 42.98 yds across 2016-2021   [FBS reality ~41-43]
Kickoff touchback rate      27.5% (2017) -> 31.9% (2018)          [2018 fair-catch rule]
PAT conversion              96.86%                                [FBS reality ~96-97%]
Longest made FG             62 yds (Garibay 2021, Hintze 2019)    [both real; record is 69]
```

The touchback jump landing exactly on the 2018 rule change is independent confirmation the
parser is reading the field it thinks it is.

**Phase 3 — Fetch, unify, load. [COMPLETE 2026-08-30]**
All 8,634 games 2016–2025 fetched from ESPN; `st.special_teams_play` loaded with
**258,614 rows / 102 MB**. 99.6% of rows parse `exact`.

*The CFBD API key was never needed.* The bulk archive turned out to be parsed ESPN data —
verified by running the parser against live ESPN text, which returned `exact` on every play
with no changes. So ESPN supplies every season, in a dialect already validated.

*Verification then forced an architecture change.* The kickoff touchback rate appeared to jump
34.7% (2021) → 50.3% (2022). That was the source boundary, not a rule change: refetching the
**same** 2016–2021 game ids from ESPN returns more plays every time. The archive is an
**incomplete extract of ESPN** — missing 78 whole games, ~6% of plays within the games it does
have, and 10–22% of kickoffs. Touchbacks are the cheapest plays to drop, so every pre-2022
kickoff rate was depressed.

Rebuilt on ESPN as the single spine. The archive was dropped from the pipeline entirely.

| Measure | On the archive | On ESPN |
|---|---|---|
| Rows | 240,636 | **258,614** |
| Games | 8,556 | **8,634** |
| Touchback rate 2021 → 2022 | 34.7% → 50.3% | **50.9% → 50.3%** |
| Rows with null kicking team | 531 | **0** |
| Rows where kicking = receiving | 24,676 | **0** |

The 2018 fair-catch rule still shows as a real step (41.6% → 46.6%) — the signal survived, the
artifact did not.

**Other Phase 3 findings:**
- **A fourth dialect appeared mid-corpus.** From 2025 ESPN concatenates an NCAA-official
  rendering onto the same text field, where the conversion sits *outside* any parenthetical:
  `#15 B.McAlister kick attempt good (H: ..., LS: ...)`. 383 of 934 games in 2025, 1,949
  conversions, against 14 in all of 2024. Nothing errored — the only symptom was one season's
  PAT count looking low. This is why `sql/verify.sql` checks per-season counts.
- **The archive's `start.team.id` is typed `double` in 2016 and 2018** but `int64` elsewhere, so
  a string comparison silently made the receiving team equal the kicking team on 24,676 rows.
  Ids are now coerced numerically.
- **ESPN 403s browser-like User-Agents** on these endpoints but serves urllib's default.
- **67 games (1.8%) have no play-by-play** in ESPN at all, mostly FBS-vs-FCS.
- **8 rows carry impossible values** (`kickoff for 127 yds`, `field goal attempt from 0 yards`)
  — faithful parses of bad source text, left visible rather than dropped.

**Phase 2b — Third dialect: the gamebook rendering. [COMPLETE 2026-08-30]**

Found by the validation console (`app.py`), which asks a question `verify.sql` never did:
*does every punt and kickoff land in one of its outcome buckets?* It did not. 49% of 2025
punts and 25% of 2025 kickoffs had every outcome flag `false` — and because those columns
have no NULL state, that is indistinguishable from a kick that genuinely had none of those
outcomes, so every fair-catch, downed and return rate after 2020 was understated.

Phase 3 spotted this dialect arriving in *PAT* text in 2025 and handled it there. It was
also arriving on punts and kickoffs, from 2021, and nothing was matching it:

```
(07:53) #33 C.Brown punt 48 yards to the USU37 #7 K.Davis return 1 yard to the USU38 (#84 N.Elksnis)
```

Four independent breakages: a leading `(MM:SS)` clock (which also landed in `kicker_name`),
`#NN` jersey prefixes, `Last,First` / `F.Last` names, and outcome clauses missing the
connecting words the patterns required — `return 9 yards` not `returns for 9 yds`,
`fair catch by X at UNT06` not `at the UNT06`. Two adjacent defects were fixed with it: the
kickoff scoring form (`"Deebo Samuel 97 Yd Kickoff Return"`) had no trap pattern and was
filing the *returner* as the kicker on 170 plays, and `N/A` — the feed's null — was arriving
glued to real names.

| unclassified punts + kickoffs | before | after |
|---|---|---|
| 2025 punts | 45.8% | **21.9%** |
| 2025 kickoffs | 24.6% | **6.7%** |
| all seasons | 24,900 | **19,716** |

*Agreement against the answer key improved on every field it touched and regressed on
none:*

| field | before | after |
|---|---|---|
| `yds_punt_return` | 99.846% | **99.973%** (68 unexplained → 12) |
| `yds_kickoff_return` | 99.582% | **99.760%** (160 → 92) |
| `kickoff_fair_catch` | 99.986% | **100.000%** (6 → 0) |
| `punt_fair_catch` | 99.994% | **99.998%** (3 → 1) |
| worst field overall | 99.582% | **99.760%** |

The football sanity checks are unchanged: FG accuracy still declines monotonically
94.6% → 34.1%, punt gross still 41.24 → 42.98, the 2018 touchback step still lands on 2018.

Landed with `sql/reparse.sql`, which UPDATEs only the 22 parser-derived columns in place
rather than the TRUNCATE + INSERT of `load_2_insert.sql` — that path would have wiped the
Phase 4 enrichment. Old values kept in `st.special_teams_play_prereparse`.

**What is left, and why a regex cannot fix it.** 18,329 of the 19,716 remaining rows read
`"Joshua Brown punt for 34 yds"` and state no outcome anywhere in the text. The fix is to
give the outcome flags a NULL state so "no outcome" and "outcome unknown" stop sharing a
value. Worst in 2023 (29.6% of punts, 28.0% of kickoffs). Until then, per-season rates on
those flags are trustworthy only for 2016–2020.

> **[DONE 2026-08-31]** — and it needed no schema change. Every one of those columns was
> already nullable in Postgres; the parser was simply writing `false` where it should have
> written nothing. `st_parser._mark_outcome_unknown` now writes NULL on the five "how did
> it end" flags, `kick_blocked` and `onside` stay `false` because both are knowable from
> what the text does say, and `returned IS NULL` is the single-column test. The 19,716 rows
> remain unreadable — that is a property of the feed — but they no longer deflate any rate,
> because `avg(flag::int)` drops them from numerator and denominator alike. See the README.

**Phase 4 — Enrichment. [COMPLETE 2026-08-30]**
Four dimensions + a bridge table, all in schema `st`. `scripts/fetch_participants.py`,
`scripts/build_dims.py`, `sql/dims.sql`, `sql/enrich.sql`, `sql/verify_phase4.sql`.

| Table | Rows | Notes |
|---|---|---|
| `dim_team_season` | 2,584 | conference BY SEASON; FBS 129 (2016) → 136 (2025) |
| `dim_conference` | 30 | |
| `dim_team` | 245 | free, from the game lists |
| `dim_venue` | 197 | 72 grass / 125 turf, 4 non-US; free, already in the saved summaries |
| `fact_game` | 8,634 | venue, attendance, neutral-site, conference-game |
| `dim_athlete` | 23,403 | |
| `play_athlete` | 338,004 | full bridge: play × role × athlete |

`special_teams_play` gained `kicker_athlete_id`, `returner_athlete_id`,
`tackler_athlete_id`, `venue_id`, `conference_game`, `neutral_site`.

**Conference realignment is handled, and it matters more than it sounds.** 67 of 267 teams
changed conference in the window; the Pac-12 went 12 teams → 2. The same 2018 punt count
grouped two ways:

| 2018 punts | team-only join (wrong) | team-season join (right) |
|---|---|---|
| Big Ten | 1,128 | 881 |
| Pac-12 | **108** | **730** |

A team-only join understates the 2018 Pac-12 by 85% because it backdates 2024 realignment
across the whole decade. `dim_team_season` carries a comment saying so.

**Athlete identity.** ESPN's core API tags each play's participants with a role. Coverage:

- kicker id: **97.4–99.1%** for 2016–2024, but **58.2% in 2025**.
- returner id, measured only on plays that were actually returned: **97.8–99.8% every season**,
  2025 included. (Unconditional returner coverage looks like 20–50% purely because most kicks
  are not returned — the wrong denominator.)
- **There is no `blocker` role in ESPN.** Blocker identity stays a parsed name string only.

The 2025 kicker gap is an ESPN source limit, not a join bug — only 65.7% of 2025 ST plays
appear in the participants payload at all, though the key matches perfectly where they do.

**`dim_athlete.known_name` needed a confidence rule.** Names come from play text, so a player
tagged in a kick role on a play naming someone else inherits the wrong name — harmless for a
kicker with 242 plays, dominant for one with 2. Five distinct athletes came out as "Bennett
Moehring". Requiring the modal name to be seen twice AND hold ≥60% cut shared names from 228
to **71** (those remaining are genuinely different players with the same name — the reason
athlete ids exist at all). `name_confidence` is stored so it can be filtered harder.

**Referential integrity: zero orphans** on all four checks. Sanity holds — 50+ yard leaders
come out as Cade York, Will Reichard, Harrison Mevis; FG% by conference for 2025 sits in a
77–81% band; grass 75.0% vs turf 74.4% at the same average distance.

**Phase 5 — Weather. [TABLED 2026-08-30 at the user's request.]**
Not started, and not blocking anything. Everything needed to start it later is already in
place, so picking it up is a fresh decision rather than a re-derivation:

*Already done — both sides of the join key exist:*
- `special_teams_play.wallclock_utc` — a real UTC timestamp on 248,819 of 258,614 rows
  (96.2%), so the join can be at the moment of the kick, not the game.
- `dim_venue` — 197 venues with city, state, zip, country; `fact_game.venue_id` links every
  game, and `special_teams_play.venue_id` is denormalised onto every play.

*Three things to decide before writing any code:*
1. **Source.** The existing `weatherdata` warehouse starts in 2019 and has 112 stations
   against 197 venues, so it covers neither the full window nor the geography. NCEI ISD/LCD
   (free, ~2,000 US airport stations, decades of history) is the likely better fit. Whichever
   is chosen, `dim_venue` has no lat/lon yet — only city/state/zip — so venue geocoding is
   the first task.
2. **Indoor venues must be flagged first.** `dim_venue` records surface (grass/turf) but not
   roof. Assigning outdoor conditions to a dome is worse than assigning nothing, and there is
   no roof field in the ESPN payload — it needs a separate source or a manual list. There are
   also 4 non-US venues (Sydney, Dublin, and two others) that need their own handling.
3. **Grain.** Nearest-station-at-kick-time is the obvious default, but it silently varies in
   quality with station distance. Store the station id and its distance from the venue on
   every row so the weak joins stay filterable, the same way `parse_confidence` works today.

*Why it is still worth doing:* per-play wind against ~200k kicks is analysis that essentially
does not exist publicly — most kicking data carries game-level weather at best.

**Phase 6 — Serve it.** Same pattern already proven with `weatherdata`: a read-only MCP
server over the schema, and/or a Streamlit view. Not scoped until the data is trusted.

## 6. Schema sketch

One fact table, not three. The situational context (down, distance, field position, score,
clock, wind) is identical across all three kick types, and one table makes
"kicks vs. punts in the same conditions" a `WHERE`, not a `UNION`. 200k rows means there is
no performance reason to split. Type-specific columns are nullable; typed views on top.

```sql
CREATE SCHEMA st;

CREATE TABLE st.special_teams_play (
  play_uid           text PRIMARY KEY,        -- source-prefixed, e.g. 'espn:401520281101849906'
  source             text NOT NULL,           -- 'espn'
  game_id            bigint NOT NULL,
  season             smallint NOT NULL,
  week               smallint,
  season_type        text,                    -- regular | postseason
  play_kind          text NOT NULL,           -- kickoff | punt | field_goal | pat | two_point
  -- situation
  period             smallint,
  clock_secs_rem     integer,
  wallclock_utc      timestamptz,             -- play-level, enables weather join
  down               smallint,
  distance           smallint,
  yards_to_goal      smallint,
  kicking_team_id    integer,
  receiving_team_id  integer,
  is_home_kicking    boolean,
  score_diff_kicking smallint,
  -- outcome (nullable by kind)
  fg_distance_yds    smallint,
  fg_made            boolean,
  kick_blocked       boolean,
  punt_gross_yds     smallint,
  punt_net_yds       smallint,
  kickoff_yds        smallint,
  return_yds         smallint,
  touchback          boolean,
  onside             boolean,
  fair_catch         boolean,
  out_of_bounds      boolean,
  downed             boolean,
  returned_for_td    boolean,
  -- people
  kicker_athlete_id  bigint,
  returner_athlete_id bigint,
  blocker_athlete_id bigint,
  -- provenance
  play_text          text NOT NULL,           -- always keep the raw string
  parse_confidence   text,                    -- exact | parsed | ambiguous
  loaded_at          timestamptz DEFAULT now()
);
```

`play_text` is retained on every row forever. When the parser improves, everything is
re-derivable without re-fetching. `parse_confidence` makes the shaky rows filterable rather
than invisible.

## 7. Weather — real opportunity, honest caveat

The `weatherdata` warehouse (112 US stations, hourly, 2019→present) plus per-play
`wallclock` timestamps means every kick could carry temperature, wind speed, wind
direction, and precipitation **at the moment it was struck**. Wind at a play level against
200k kicks is genuinely novel analysis — most public kicking data has game-level weather at best.

Two honest gaps before promising it:
1. **Time.** The warehouse starts in 2019. Seasons 2016–2018 have no coverage.
2. **Geography.** 112 stations vs. ~135 FBS stadiums. Station density near stadiums is
   unverified — the warehouse was built for a different purpose and may cluster badly.

Fix options, to decide at Phase 5: backfill GHCNh for stadium-nearest stations from 2016,
or pull NCEI ISD/LCD hourly (free, ~2,000 US airport stations, decades of history) which
almost certainly has better stadium proximity. Either way, indoor stadiums need flagging so
they are excluded rather than assigned meaningless outdoor conditions.

## 8. Open decisions

1. **Ten years = which years?** 2016–2025 assumed. Note 2020 is a COVID-shortened outlier
   that will distort any per-season rate — keep it flagged, not deleted.
2. **Scope of "FBS":** all games involving ≥1 FBS team (includes FBS-vs-FCS), or FBS-vs-FBS
   only, or conference games only? This changes row counts by ~10–15% and materially changes
   any kicker-quality analysis. Recommend: ingest everything, flag `is_conference_game` and
   `opponent_division`, filter at query time.
3. **PAT scope:** include extra points and two-point attempts as rows? They are placekicks
   (mostly), they are ~32k rows, and they are cheap to carry. Recommend yes, with
   `play_kind='pat'`.
4. **Where it lives:** local Postgres first, or straight onto EC2 next to `weatherdata`?

## 9. Risks

- **ESPN API is unofficial.** It can change without notice. Never make it the spine — only
  the enrichment layer. Everything it provides must be nullable.
- **Bulk archives of this era are abandoned at 2021.** Do not build a pipeline that
  depends on one refreshing.
- **Parsing is where the errors hide.** A kick logged with the wrong distance looks exactly
  like a real kick. Phase 2's diff-against-answer-key is not optional polish; it is the
  reason to trust the second half of the dataset.
- **Conference realignment** will silently corrupt any grouped analysis if conference is
  stored as a team attribute instead of a team-season attribute.
- **Player name collisions** across 10 seasons and 130+ teams are guaranteed. Athlete IDs
  from ESPN, not name strings, wherever possible.

## 10. Scrimmage plays (offense/defense) — design

**Not built. Design only, agreed 2026-08-31.** Numbers below are measured against the local
files, not estimated.

### 10a. Verdict

Buildable from `data/espn/summaries/` and `data/espn/participants/` with **no new fetching**,
and it is a *cheaper* build than special teams was. The reason is a reversal of the problem
that made §5 hard: kick outcomes exist only in prose, but scrimmage outcomes are structured
fields.

| field | coverage over 1,497,044 scrimmage plays |
|---|---|
| `statYardage` (yards gained) | **100.00%** |
| `start.down`, `start.distance`, `start.yardsToEndzone` | **100.00%** |
| `isTurnover`, `scoringPlay` | **100.00%** |
| `sequenceNumber` (participants join key) | **100.00%** |
| offense + defense team ids (`teamParticipants`) | 99.77% |
| play text | 99.97% |
| athlete participants present | 96.3% |

Flat across all twelve seasons — no 2014-style cliff, and no equivalent of the 19,716 kicks
that state no outcome. **`st_parser.py` is not needed here.** The core table is a projection
of structured fields; the play type does the classification that ESPN so often gets wrong on
kicks. Text parsing becomes optional enrichment (§10f), not the spine.

### 10b. Grain and scope

One row per scrimmage play. **1,497,044 rows**, 2014–2025, after two exclusions:

- **111,510 administrative rows** — `Timeout`, `End Period`, `End of Half`, `Coin Toss`.
  Not plays; they would corrupt any per-play rate.
- **2,294 plays the special-teams classifier already claims.** These are typed `Penalty`
  (1,356) or `Punt Return` (847) but rescued into `st.special_teams_play` by `TEXT_HINT`.
  They must be excluded here or they live in both tables. Reuse `build_table.classify()` as
  the exclusion filter rather than re-deriving the rule — one definition, two callers.

Play-type mix: Rush 678,929 · Pass Reception 346,454 · Pass Incompletion 237,197 ·
Penalty 93,871 · Sack 39,608 · Rushing TD 33,997 · Passing TD 32,628 · Interception Return
12,541 · fumble recoveries 16,922 · a long tail below 3,000.

### 10c. Schema

Mirrors `st.special_teams_play` deliberately: same `play_uid` convention, same source column,
same denormalised game-context columns, so the two facts can be `UNION`ed for whole-game
questions and so `enrich_game_context.sql` works on both.

```sql
CREATE TABLE st.scrimmage_play (
  play_uid            text PRIMARY KEY,      -- 'espn:<game_id><sequenceNumber>'
  source              text NOT NULL,         -- 'espn'
  game_id             bigint NOT NULL,
  season              smallint NOT NULL,
  week                smallint,
  season_type         text,
  play_kind           text NOT NULL,         -- rush | pass | sack | penalty | return | other
  play_type_espn      text NOT NULL,         -- ESPN's raw label, kept so misclassification is auditable
  drive_id            text,
  -- situation (100% populated upstream)
  period              smallint,
  clock_secs_period   integer,
  wallclock_utc       timestamptz,           -- play-level, enables the weather join
  down                smallint,
  distance            smallint,
  yards_to_goal       smallint,
  offense_team_id     integer,
  defense_team_id     integer,
  is_home_offense     boolean,
  score_diff_offense  smallint,
  -- outcome: read off structured fields, NOT parsed
  yards_gained        smallint,              -- statYardage
  end_yards_to_goal   smallint,
  first_down_gained   boolean,               -- derived from end.down / possession
  is_complete         boolean,               -- pass plays only; from type, not text
  is_touchdown        boolean,
  is_turnover         boolean,               -- isTurnover
  is_penalty          boolean,               -- isPenalty
  is_scoring_play     boolean,               -- scoringPlay
  points_scored       smallint,              -- from the home/away score delta
  -- people: hot path, mirroring the ST table's three id columns
  passer_athlete_id   bigint,
  rusher_athlete_id   bigint,
  receiver_athlete_id bigint,
  tackler_athlete_id  bigint,
  -- provenance and game context
  play_text           text,
  loaded_at           timestamptz DEFAULT now(),
  venue_id            integer,
  neutral_site        boolean,
  conference_game     boolean
);
```

Plus `st.scrimmage_athlete`, the full-fidelity bridge, identical in shape to
`st.play_athlete` (`play_uid`, `role`, `athlete_id`, `ordinal`). **~3.27M rows.** The four id
columns on the fact are a denormalised convenience; the bridge is the truth, because a play
has many tacklers and `assistedBy` is 383,400 rows on its own.

Roles available: `rusher` 712,787 · `receiver` 701,702 · `passer` 676,197 · `tackler` 477,137
· `assistedBy` 383,400 · `scorer` · `penalized` · `sackedBy` 44,619 · `passDefender` 40,796 ·
`forcedBy` · `recoverer` · `fumbler`.

### 10d. dim_athlete becomes shared — decided

**Rebuild `st.dim_athlete` as the union of both fact tables.** 23,327 distinct athletes appear
on scrimmage plays; **13,790 are already in it** from special teams. The dimension grows
33,139 → **42,676** (+29%).

This is the whole reason for using ESPN athlete ids over name strings: a receiver who also
returns kicks must be *one* row, or every cross-phase question silently double-counts him. A
separate `dim_athlete_off` would duplicate those 13,790 players and re-introduce exactly the
name-matching problem §5 spent its effort eliminating.

Consequence to plan for: this **mutates a table the ST fact and the app already depend on**.
It is the one part of this build that is not purely additive. `primary_role` and
`primary_team_id` are derived from play counts, so they will shift for two-phase players once
offensive plays are in scope. Take a rollback point first, and re-run
`sql/load_athletes_2_apply.sql` for the ST side afterwards so both facts point at the rebuilt
dimension.

### 10e. Optional third table: drives

The summaries already carry a drive object per drive — `result`, `yards`, `offensivePlays`,
`timeElapsed`, `start`, `end`, `isScore`. Measured at **23.8 drives/game in 2024**, so
**~246,500 rows** over 10,371 games. Cheap, and it is the natural home for
"what did this drive end in" (`PUNT` 7,481 · `TD` 5,730 · `FG` 2,088 · `DOWNS` 1,489 ·
`INT` 1,291 · `FUMBLE` 796 in 2024). Recommend building it *with* the fact, not after, since
`drive_id` is on every play and backfilling a key later is more work than carrying it now.

### 10f. What still needs text parsing — all optional

- Air yards vs yards-after-catch, and pass direction. Not structured anywhere.
- Penalty type and yardage across the 93,871 penalty plays. `isPenalty` and `statYardage`
  carry the fact and the yards; only the *reason* needs text.
- Sack yardage needs nothing — `statYardage` is already negative.
- 127 plays carry an empty type string; route to `play_kind='other'` rather than dropping.

### 10g. Load sequence

Same staged pattern as §5, and the same trap: `load_2_insert.sql`-style TRUNCATE loaders clear
the game-context columns, so the enrichment step is not optional.

1. `scripts/build_scrimmage.py` → `data/out/scrimmage_plays.csv` (+ bridge CSVs)
2. `sql/load_scrimmage_1_stage.sql` → all-text staging → `\copy` → typed insert
3. `sql/enrich_game_context.sql` — extend to cover the new table
4. `scripts/build_dims.py athlete` — rebuilt over **both** facts → union dimension
5. `sql/load_athletes_2_apply.sql` for both facts
6. `scripts/build_snapshot.py`
7. `VACUUM (FULL, ANALYZE)` — see "Reclaiming space after a reload" in README.md

### 10h. Size

Extrapolating from the current tables' measured bytes-per-row (388 for the ST fact, 160 for
`play_athlete`): fact ~450–580 MB, bridge ~520 MB, drives ~40 MB. **`cfb` goes from 232 MB to
roughly 1.3 GB.** Manageable, but it moves `VACUUM FULL` from housekeeping to necessary — the
three-rewrites-per-load behaviour documented in the README costs ~1 GB of bloat at this scale,
not ~170 MB.

### 10i. Naming debt

The schema is called `st` because special teams was all it held. A scrimmage fact makes that a
misnomer. Renaming is one statement (`ALTER SCHEMA st RENAME TO cfb`) plus a sweep of every
script and SQL file — mechanical but wide. Recommend **keeping `st` and accepting the
misnomer**: a rename touches working code for cosmetic gain, and the README can carry the
one-line explanation instead.

> **Overridden 2026-09-08 — see §10j.6.** The rename is happening: repo, schema and snapshot
> filename all move to `cfb-pbp` / `pbp`. The blast radius was measured rather than assumed
> and is smaller than this section supposed, but it has two traps worth reading before
> touching anything — §10l.

### 10j. Decisions — settled 2026-09-08

The four questions below were open when §10 was written. All are now decided. Numbers were
re-measured against the local files on 2026-09-08, so where they differ from §10a–§10h the
figure here is the current one: §10 counted 2014–2025, this counts 2014–2026-to-date and
uses `build_table.classify()` itself as the special-teams exclusion rather than a
hand-listed type set.

| | 2026-08-31 (§10) | 2026-09-08 (re-measured) |
|---|---|---|
| scrimmage plays | 1,497,044 | **1,510,802** |
| drives | ~246,500 | **258,795** |
| penalty plays | 93,871 | **93,438** |
| `statYardage` coverage | 100.00% | **100.00%** |
| `start.down` coverage | 100.00% | **100.00%** |
| offense/defense team ids | 99.77% | **99.78%** |
| athlete participants | 96.3% | **~99% every season** |

The participant figure moved because the earlier measure did not normalise the two key
shapes documented in `fetch_participants.one()`. Keys written before the 2025 id/sequence
divergence was found carry the game id as a prefix; stripping it, as `build_dims.py`
already does, lifts coverage on rush/pass/sack plays to 98–100% in every season from 2014
to 2026. Any new reader of `data/espn/participants/` must do the same normalisation or it
will silently see about a fifth of the athletes.

**1. `play_kind` keeps `is_complete` as a flag.** Recommendation of §10j.1 accepted. The
kind vocabulary stays short and matches how the ST table treats `fg_made`.

**2. Penalty plays stay in the fact** as `play_kind='penalty'`, 93,438 rows, filtered at
query time. Recommendation of §10j.2 accepted; it matches decision §8.2.

**3. Two-point conversions stay in `st.special_teams_play`.** Not duplicated. Unchanged.

**4. The apps are out of scope this round.** `app.py` and `web/` stay special-teams-only,
and player profile pages stay kicking-side only. The UI question is deferred *deliberately*
until the new tables can be queried directly — the shape of an offensive play page is not
knowable before looking at the data. §10j.4 asked whether the app was in scope; the answer
is "not yet, and not because it is hard".

**5. Scope stops at play-level facts.** `scrimmage_play`, the athlete bridge, and drives.
No derived player stat lines (per-game / per-season passing, rushing, receiving, defence)
and no team box scores. Both are `GROUP BY`s over the fact and can be added later without
reloading anything.

**6. Full rename to `cfb-pbp`, schema `pbp`** — repo directory, schema, and the DuckDB
snapshot filename all move. **This overrides §10i's recommendation to keep `st`.**

**7. Build first, rename last.** The rename is a single sweep over a finished thing, so the
documentation is rewritten once instead of twice and a rename bug can never masquerade as a
build bug.

**8. The weekly in-season loader takes the new fact in the same round.**
`scripts/update_season.py` gains the scrimmage path, so 2026 stays current on both facts
rather than the new one freezing on the day it ships.

**9. `git init` first.** The project had no version control of any kind. This is what makes
step 3 of the sequence below revertible.

### 10k. Build sequence

Stages are ordered so that each one leaves both existing apps working.

0. **`git init`**, `.gitignore` for `.venv/`, `__pycache__/`, `data/`, commit the current
   state. Nothing else moves until this exists.
1. **`scripts/build_scrimmage.py`** → `data/out/scrimmage_plays.csv`, 1,510,802 rows.
   Call `build_table.classify()` as the exclusion filter rather than re-deriving the rule;
   the ~2,300 plays it rescues into the ST fact must not appear in both tables.
2. **Bridge and drives**, built in the same pass — `scrimmage_athlete` (~3.3M rows, all
   twelve roles) and the drives table (258,795 rows). `drive_id` is on every play, so
   carrying it now is cheaper than backfilling a key later.
3. **Rebuild `dim_athlete` over both facts**, 33,891 → ~42.7k. The one non-additive step:
   it mutates a table both facts and both apps already depend on, and `primary_role` will
   move for two-phase players. Re-run `sql/load_athletes_2_apply.sql` for the ST side
   afterwards.
4. **`build_snapshot.py`** gains the second table; **`update_season.py`** gains the weekly
   scrimmage path.
5. **Rename** to `cfb-pbp` / schema `pbp`, and rewrite the documentation.

### 10l. Rename traps — measured, not guessed

Blast radius is smaller than §10i assumed, but two of these will cause real damage if the
sweep is done with a naive substitution.

- **`app.py` does `import streamlit as st`.** A blanket `s/st\./pbp./` would rewrite 150+
  Streamlit calls — `st.subheader`, `st.sidebar`, `st.dataframe` and the rest. The sweep
  must target the table names (`st.special_teams_play`, `st.dim_athlete`, `st.play_athlete`,
  `st.fact_game`, `st.dim_team_season`, `st.dim_team`, `st.dim_venue`, `st.dim_conference`,
  `st.stg_*`), never the bare prefix.
- **`web/data.py` creates a DuckDB view literally named `st`.** Unrelated to the Postgres
  schema and independently named; decide it separately rather than letting the sweep catch it.
- **The repo path is hardcoded in six files, one line each**: `scripts/fetch_espn.py`,
  `fetch_participants.py`, `build_table.py`, `build_dims.py`, `flatten_game.py`, and
  `README.md`.
- **`data/out/st.duckdb` has seven references** across the two apps and the snapshot builder.
- **~180 prose mentions of "special teams"**, concentrated in `scripts/build_erd.py` (32),
  `PLAN.md` (18), `README.md` (16) and the verification SQL.
