"""Build the scrimmage-play fact table from ESPN play-by-play, 2014 onward.

The mirror image of build_table.py. That script had to parse prose because a kick's outcome
exists nowhere else; this one barely parses at all, because a scrimmage play's outcome is
structured. Measured over the whole corpus: statYardage 100.00%, start.down 100.00%,
isTurnover/scoringPlay 100.00%, offense/defense team ids 99.78%. st_parser_cfb.py is not
imported here and is not needed.

Scope, and how it stays disjoint from the special-teams fact:

    all plays in data/espn/summaries/        1,868,595
    - administrative (timeouts, period ends)   112,860
    - whatever build_table.classify() claims    244,933
    = scrimmage plays                        1,510,802

The second subtraction calls `classify()` itself rather than re-listing the play types it
matches. That matters: 545 special-teams plays hide under playType='Penalty' and are rescued
by its TEXT_HINT table, so a type whitelist here would put them in both facts. One
definition, two callers.

play_uid follows the special-teams convention exactly -- 'espn:<game_id>:<sequenceNumber>' --
so the two facts UNION cleanly and both join the same participants files. (PLAN.md §10c
writes it without the second colon; that is a typo in the sketch, not a second convention.)
The ':pat' rows build_table derives from touchdown plays are siblings of these rows, never
duplicates: a touchdown play itself is never claimed by classify().

    python build_scrimmage.py                             -> data/out/scrimmage_plays.csv
    python build_scrimmage.py --seasons 2026 --out x.csv  one season, for the in-season update
    python build_scrimmage.py --league nfl                -> data/out/scrimmage_plays_nfl.csv

This is the half of the pipeline that ports to the NFL for free. Nothing here parses prose,
and every structured field it reads -- statYardage, start/end down and distance,
yardsToEndzone, isTurnover, scoringPlay, teamParticipants -- is present and populated in the
NFL feed. The league selects a raw directory and stamps a column; no rule below changes.
"""
import sys, os, gzip, json, csv, argparse, collections, time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import league as lg
from build_table import classify, as_team, same_team, clock_secs, advance_score

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEAGUE = lg.CFB
ESPN, OUT = lg.data_dir(LEAGUE), f"{HOME}/data/out"
SEASONS = [2014, 2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]
WORKERS = 8


def use(name):
    """Point the module at one league's raw directory.

    rows_for_game() does NOT read this. It runs in a worker process, and on macOS a
    ProcessPoolExecutor SPAWNS rather than forks -- the child re-imports this module fresh
    and would come up as college no matter what the parent set. The league travels in the
    job tuple instead, which is explicit and correct on both start methods.
    """
    global LEAGUE, ESPN
    LEAGUE, ESPN = name, lg.data_dir(name)

# Not plays. They carry no down, no yardage and no participants, and counting them would
# deflate every per-play rate in the table.
ADMIN = {"Timeout", "End Period", "End of Half", "End of Game", "Coin Toss",
         "End of Regulation", "Official Timeout",
         # NFL-only. 6,631 rows corpus-wide -- a clock stoppage with no down, no yardage
         # and no participants. Left in, it would sit in `other` and deflate every
         # per-play rate in the table by about 1%.
         "Two-minute warning"}

# Conversion attempts, not scrimmage downs -- no down, no distance, and one fixed yard line.
# PLAN.md §10j.3 puts the conversion family in pbp.special_teams_play and says not to
# duplicate it here, so these 123 corpus-wide plays are excluded.
#
# CAVEAT, and it is a real one: build_table.emit_pat DERIVES conversion rows from the
# touchdown play's text and only fires on `scoringPlay or "kick attempt" in text`, so a
# FAILED two-point try that ESPN also emits as its own play row is caught by neither
# builder. Checked against the live fact: 0 of the 22 such plays in 2024 have a ':pat'
# sibling. Excluding them here is still right -- they belong to the conversion family, and
# the fix belongs in build_table.py -- but until that fix lands these rows are in no table.
CONVERSION = {"Two Point Pass", "Two Point Rush", "Defensive 2pt Conversion"}

# ---------------------------------------------------------------- play_kind
# ESPN's play type names the most NOTABLE EVENT on the play, not the play that was called.
# "Fumble Recovery (Opponent)" is a rush or a pass that ended in a fumble; "Pass Interception
# Return" is a pass attempt. Typing off that column alone would take ~33,000 rushes and passes
# out of the rush and pass populations and file them under their outcome instead.
#
# So: the unambiguous types map directly, and the outcome-named types are resolved by asking
# the participants feed what actually happened at the snap -- a `passer` role means a pass was
# thrown, a `rusher` role means the ball was carried. Measured over a 400-game sample, that
# resolves 'Pass Interception Return' 99.8% of the time, 'Interception' 100%, and the fumble
# recoveries 89-95%; what it cannot resolve falls through to 'other' rather than guessing.
#
# The raw label is kept in play_type_espn on every row, so any of this is auditable and
# reversible without a rebuild.
KIND_BY_TYPE = {
    "Rush": "rush", "Rushing Touchdown": "rush",
    "Pass Reception": "pass", "Pass Incompletion": "pass", "Passing Touchdown": "pass",
    "Pass Completion": "pass",
    "Sack": "sack",
    "Penalty": "penalty",
    # NFL spellings for the same three things. 'Pass' is the bare label the NFL feed uses
    # on 61 plays where college always qualifies it; 'Sack Opp Fumble Recovery' is a sack
    # whose most notable event was the fumble, and it is a sack at the snap either way.
    "Pass": "pass",
    "Sack Opp Fumble Recovery": "sack",
}
# Typed by outcome; the snap is recovered from participant roles instead.
BY_PARTICIPANT = {
    "Interception", "Pass Interception Return", "Interception Return Touchdown",
    "Fumble", "Fumble Recovery (Own)", "Fumble Recovery (Opponent)",
    "Fumble Return Touchdown", "Safety", "Defensive 2pt Conversion", "",
    # NFL-only, and typed by outcome exactly like the college entries above.
    "Muffed Punt Recovery (Opponent)", "Fumble Recovery (Opponent) Touchdown",
}
# Completion state, for pass plays only. An interception is a pass attempt that was not
# completed, which is how NCAA completion percentage treats it (att = comp + inc + int).
# A sack is not a pass attempt in NCAA accounting at all, so it stays NULL.
COMPLETE_BY_TYPE = {
    "Pass Reception": True, "Passing Touchdown": True, "Pass Completion": True,
    "Pass Incompletion": False, "Interception": False,
    "Pass Interception Return": False, "Interception Return Touchdown": False,
}

ROLE_COLUMN = {"passer": "passer_athlete_id", "rusher": "rusher_athlete_id",
               "receiver": "receiver_athlete_id", "tackler": "tackler_athlete_id"}

# The full-fidelity people bridge. The four id columns on the fact are a denormalised
# convenience for the hot path; this is the truth, because a play has many tacklers and
# `assistedBy` alone is hundreds of thousands of rows. Every role ESPN reports is kept --
# filtering to "interesting" roles here would be a decision the query layer can make and
# this one cannot undo.
BRIDGE_COLUMNS = ["play_uid", "role", "athlete_id", "ordinal"]

# One row per drive. Drives span BOTH facts -- a drive that ends in a punt contains the punt
# -- so this table is deliberately not scrimmage-only, and play counts here cover every play
# in the drive, not just the ones in pbp.scrimmage_play.
DRIVE_COLUMNS = ["drive_uid", "league", "drive_id", "game_id", "season", "week", "season_type",
                 "drive_number", "offense_team_id", "defense_team_id", "result",
                 "display_result", "description", "is_score", "offensive_plays",
                 "plays_total", "plays_scrimmage", "yards", "time_elapsed_secs",
                 "start_period", "start_clock_secs", "start_yards_to_goal", "start_text",
                 "end_period", "end_clock_secs", "end_yards_to_goal", "end_text"]

COLUMNS = ["play_uid", "league", "source", "game_id", "season", "week", "season_type",
           "play_kind", "play_type_espn", "drive_id", "drive_number", "period", "clock_secs_period",
           "wallclock_utc", "down", "distance", "yards_to_goal", "offense_team_id",
           "defense_team_id", "is_home_offense", "score_diff_offense", "yards_gained",
           "end_down", "end_distance", "end_yards_to_goal", "end_team_id",
           "first_down_gained", "is_complete", "is_touchdown", "is_turnover", "is_penalty",
           "is_scoring_play", "points_scored", "passer_athlete_id", "rusher_athlete_id",
           "receiver_athlete_id", "tackler_athlete_id", "play_text"]


# Every positional field ESPN reports has a legal range, and every one of them is violated
# somewhere in 1.5M plays. Two distinct causes, both handled the same way -- NULL, never a
# clamp, because a clamped value is indistinguishable from a real one:
#
#   sentinels   a play that ends the series leaves 'down': -1, 'distance': -1 rather than
#               omitting them. 68,401 rows carry end_distance = -1 this way, and end.down
#               shows a 5th down that does not exist in the sport.
#   corruption  end.yardsToEndzone ranges to 5300 and down to -1135 on 10,363 rows. There is
#               no interpretation of a football field where either is a position.
#
# Left raw, "average yards to go" and any field-position model built on the end state read
# as nonsense, and the cause is invisible because the column looks populated.
RANGE = {"down": (1, 4), "distance": (0, 99), "yards_to_goal": (0, 100),
         "end_down": (1, 4), "end_distance": (0, 99), "end_yards_to_goal": (0, 100)}


def bounded(field, v, stats):
    lo, hi = RANGE[field]
    if not isinstance(v, int) or isinstance(v, bool):
        return None
    if lo <= v <= hi:
        return v
    stats[f"out_of_range:{field}"] += 1
    return None


def participants_for(gid, espn=None):
    """Load one game's participants, normalising the two key shapes that exist on disk.

    fetch_participants.one() documents the split: files written before the 2025 core-API
    id/sequenceNumber divergence was found key on '<game_id><sequenceNumber>', files written
    after key on the bare sequence number. build_dims.py already strips the prefix; anything
    that does not sees roughly a fifth of the athletes vanish.
    """
    path = f"{espn or ESPN}/participants/{gid}.json.gz"
    if not os.path.exists(path):
        return {}
    with gzip.open(path, "rt") as f:
        raw = json.load(f)
    out = {}
    for k, v in raw.items():
        out[k[len(gid):] if k.startswith(gid) and len(k) > len(gid) else k] = v
    return out


def elapsed_secs(disp):
    """'6:16' -> 376. Drive clocks run mm:ss and occasionally h:mm:ss on a reviewed drive."""
    if not disp or ":" not in str(disp):
        return None
    bits = str(disp).split(":")
    try:
        return sum(int(b) * 60 ** i for i, b in enumerate(reversed(bits)))
    except ValueError:
        return None


def kind_of(play_type, roles):
    k = KIND_BY_TYPE.get(play_type)
    if k:
        return k
    if play_type in BY_PARTICIPANT:
        # A play with both a passer and a rusher is a pass that was lateralled or scrambled
        # after the throw; the throw is the snap, so passer wins.
        if "passer" in roles:
            return "pass"
        if "rusher" in roles:
            return "rush"
    return "other"


def first_down(st_team, en_team, st_down, en_down, touchdown):
    """Did the offense keep the ball and reach a new first down?

    Possession is the first test, not the down. After a turnover ESPN writes end.down = 1
    for the side that took the ball away, which reads as a first down for the offense that
    just lost it unless the team ids are compared first.
    """
    if st_down is None:
        return None                        # not a normal scrimmage snap
    if st_team is None or en_team is None:
        return None
    if not same_team(st_team, en_team):
        return False                       # turnover, or a safety handing the ball over
    if touchdown:
        return True                        # end.down is the -1 sentinel; the drive scored
    return en_down == 1


def rows_for_game(job):
    """One game -> (fact rows, bridge rows, drive rows). Runs in a worker process.

    All three come out of one read of the summary and one read of the participants file.
    Splitting them into separate scripts would triple the I/O over 10,470 gzipped games for
    no gain, and drive_id has to be stamped on the fact rows anyway.
    """
    gid, g, season, league = job
    espn = lg.data_dir(league)
    spath = f"{espn}/summaries/{gid}.json.gz"
    if not os.path.exists(spath):
        return gid, [], [], collections.Counter()
    try:
        with gzip.open(spath, "rt") as f:
            summary = json.load(f)
    except Exception:
        return gid, [], [], collections.Counter({"unreadable": 1})

    parts = participants_for(gid, espn)
    home = as_team(next((t["id"] for t in g["teams"] if t["home_away"] == "home"), None))
    away = as_team(next((t["id"] for t in g["teams"] if t["home_away"] == "away"), None))
    season_type = "postseason" if g["season_type"] == 3 else "regular"

    # Which sequenceNumbers will build_table claim for the special-teams fact? ESPN reuses
    # one sequenceNumber for two different plays often enough that 129 of them land on a
    # scrimmage play AND a kick -- espn:400547726:102907001 is both "Max DeLorenzo 46 Yd Run"
    # here and "Bobby Puyol kickoff for 52 yds" there. Both builders would emit the same
    # play_uid, which breaks the one property §10c asks of it: that the two facts UNION
    # cleanly and a bridge row points at exactly one play.
    #
    # Detected from the summary alone rather than by reading st_plays.csv or the database, so
    # this script keeps build_table's property of depending on nothing but data/espn/.
    # A COUNT, not a set. build_table writes the first play on a given sequenceNumber under
    # the bare uid and suffixes the rest '#2', '#3'... exactly as this script does. If both
    # number their collisions from 2 independently they collide with each other -- which they
    # did: espn:401752915:116#2 was simultaneously a punt and a rush. Counting how many uids
    # build_table will consume for each sequenceNumber lets this script start numbering after
    # them, so the two facts share one namespace without sharing any uid.
    #
    # CONVERSION types count here even though they are excluded below: build_table emits them
    # as standalone conversion rows on the same bare uid. Its ':pat' rows do not count -- they
    # carry their own suffix and cannot collide.
    st_seq_counts = collections.Counter()
    for dr in summary.get("drives") or []:
        for p in dr.get("plays") or []:
            pt = (p.get("type") or {}).get("text") or ""
            if pt in ADMIN:
                continue
            if pt in CONVERSION or classify(pt, p.get("text"), league):
                st_seq_counts[str(p.get("sequenceNumber") or p.get("id"))] += 1

    out, drives, stats = [], [], collections.Counter()
    prev_home = prev_away = 0
    for dnum, dr in enumerate(summary.get("drives") or [], start=1):
        dplays = dr.get("plays") or []
        d_scrim = 0
        d_first_ytg = d_last_ytg = None
        d_defense = None
        for p in dplays:
            ptype = (p.get("type") or {}).get("text") or ""
            text = p.get("text")
            hs, aw = p.get("homeScore"), p.get("awayScore")
            # Repaired once per play, before any branch, so the running total advances
            # identically whether or not this play ends up in this fact. See
            # build_table.advance_score for what it repairs and why.
            nh, na, dh, da = advance_score(prev_home, prev_away, hs, aw)

            # Drive field position comes from the plays, not from drive.start.yardLine.
            # That column is measured in a fixed direction rather than from the possessing
            # team's own goal, so it reads 25 for one team's own 25 and 76 for the other's
            # own 24. start.yardsToEndzone on the play is offense-relative and 100%
            # populated, so the drive's endpoints are taken from its first and last play.
            _st, _en = p.get("start") or {}, p.get("end") or {}
            if ptype not in ADMIN:
                if d_first_ytg is None:
                    d_first_ytg = _st.get("yardsToEndzone")
                if _en.get("yardsToEndzone") is not None:
                    d_last_ytg = _en.get("yardsToEndzone")
                if d_defense is None:
                    for t in p.get("teamParticipants") or []:
                        if t.get("type") == "defense":
                            d_defense = as_team(t.get("id"))

            if ptype in ADMIN:
                stats["admin"] += 1
            elif ptype in CONVERSION:
                stats["conversion"] += 1
            elif classify(ptype, text, league):
                stats["special_teams"] += 1
            else:
                seq = p.get("sequenceNumber") or p.get("id")
                pairs = parts.get(str(seq)) or []
                roles = {r for r, _ in pairs}
                st, en = p.get("start") or {}, p.get("end") or {}

                # teamParticipants is the only place offense and defense are stated. On a
                # kick, start.team.id is the KICKING team and would be the defense on a
                # kickoff -- see build_table.py's team-convention note. It is 99.78%
                # populated; the rest keep NULL rather than a guess from possession.
                off = defn = None
                for t in p.get("teamParticipants") or []:
                    if t.get("type") == "offense":
                        off = as_team(t.get("id"))
                    elif t.get("type") == "defense":
                        defn = as_team(t.get("id"))
                if off is None:
                    off = as_team((st.get("team") or {}).get("id"))
                    stats["offense_from_start_team"] += 1

                off_is_home = same_team(off, home) if off is not None else None
                # The margin BEFORE the snap. homeScore/awayScore are the score AFTER the
                # play, so they are wrong for any play that scored; prev_* carries the
                # repaired running score into the play. (pbp.special_teams_play.score_diff_kicking
                # documents "before the play" but is still computed from the after-play
                # columns -- a separate, older inconsistency, noted in README "Known limits".)
                diff = (None if off_is_home is None
                        else int(prev_home - prev_away if off_is_home else prev_away - prev_home))

                td = "touchdown" in ptype.lower() or " for a td" in (text or "").lower()
                st_down = bounded("down", st.get("down"), stats)
                en_down = bounded("end_down", en.get("down"), stats)
                st_team = (st.get("team") or {}).get("id")
                en_team = (en.get("team") or {}).get("id")

                # Points the OFFENSE gained on this play, signed: a pick-six is negative.
                pts = None if off_is_home is None else int(dh - da if off_is_home else da - dh)

                ath = {}
                seen_pair, my_bridge = set(), []
                for i, (role, aid) in enumerate(pairs):
                    col = ROLE_COLUMN.get(role)
                    if col and col not in ath:        # first named in the role wins
                        ath[col] = aid
                    # (play_uid, role, athlete_id) is the bridge's primary key, matching
                    # pbp.play_athlete. ESPN occasionally repeats a pair on one play; the
                    # repeat is dropped here rather than failing the load.
                    if (role, aid) in seen_pair:
                        stats["bridge_dup_pair"] += 1
                        continue
                    seen_pair.add((role, aid))
                    my_bridge.append((role, aid, i))
                d_scrim += 1

                kind = kind_of(ptype, roles)
                stats[f"kind:{kind}"] += 1
                if ptype in BY_PARTICIPANT:
                    stats["resolved_by_participant" if kind != "other"
                          else "unresolved_outcome_type"] += 1

                out.append({
                    "play_uid": f"espn:{gid}:{seq}", "league": league,
                    "source": "espn", "game_id": gid,
                    "season": season, "week": g["week"], "season_type": season_type,
                    "play_kind": kind, "play_type_espn": ptype,
                    "drive_id": dr.get("id"), "drive_number": dnum,
                    "period": (p.get("period") or {}).get("number"),
                    "clock_secs_period": clock_secs((p.get("clock") or {}).get("displayValue")),
                    "wallclock_utc": p.get("wallclock"),
                    "down": st_down,
                    "distance": bounded("distance", st.get("distance"), stats),
                    "yards_to_goal": bounded("yards_to_goal", st.get("yardsToEndzone"), stats),
                    "offense_team_id": off, "defense_team_id": defn,
                    "is_home_offense": off_is_home, "score_diff_offense": diff,
                    "yards_gained": p.get("statYardage"),
                    "end_down": en_down,
                    "end_distance": bounded("end_distance", en.get("distance"), stats),
                    # yardsToEndzone is always measured from the perspective of whoever holds
                    # the ball at that moment, so on a turnover the end value flips to the
                    # OTHER team's goal line. end_team_id is carried so the number is
                    # interpretable; without it, 3.4% of rows read as a 60-yard swing.
                    "end_yards_to_goal": bounded("end_yards_to_goal",
                                                 en.get("yardsToEndzone"), stats),
                    "end_team_id": as_team(en_team),
                    "first_down_gained": first_down(st_team, en_team, st_down, en_down, td),
                    "is_complete": COMPLETE_BY_TYPE.get(ptype),
                    "is_touchdown": td, "is_turnover": p.get("isTurnover"),
                    "is_penalty": p.get("isPenalty"),
                    "is_scoring_play": p.get("scoringPlay"), "points_scored": pts,
                    "play_text": text, **ath,
                    # consumed by main(); extrasaction='ignore' keeps them out of the CSV.
                    # The bridge rides on its fact row rather than being accumulated
                    # separately, so a '#n' suffix applied below cannot leave the bridge
                    # pointing at a play_uid the fact table does not have.
                    "_st_seq_n": st_seq_counts.get(str(seq), 0),
                    "_bridge": my_bridge,
                })

            prev_home, prev_away = nh, na

        dstart, dend = dr.get("start") or {}, dr.get("end") or {}
        drives.append({
            "drive_uid": f"espn:{gid}:d{dnum}", "league": league,
            "drive_id": dr.get("id"), "game_id": gid,
            "season": season, "week": g["week"], "season_type": season_type,
            "drive_number": dnum,
            "offense_team_id": as_team((dr.get("team") or {}).get("id")),
            "defense_team_id": d_defense,
            "result": dr.get("result"), "display_result": dr.get("displayResult"),
            "description": dr.get("description"), "is_score": dr.get("isScore"),
            "offensive_plays": dr.get("offensivePlays"),
            "plays_total": len(dplays), "plays_scrimmage": d_scrim,
            "yards": dr.get("yards"),
            "time_elapsed_secs": elapsed_secs((dr.get("timeElapsed") or {}).get("displayValue")),
            "start_period": ((dstart.get("period") or {}).get("number")),
            "start_clock_secs": clock_secs((dstart.get("clock") or {}).get("displayValue")),
            "start_yards_to_goal": d_first_ytg, "start_text": dstart.get("text"),
            "end_period": ((dend.get("period") or {}).get("number")),
            "end_clock_secs": clock_secs((dend.get("clock") or {}).get("displayValue")),
            "end_yards_to_goal": d_last_ytg, "end_text": dend.get("text"),
        })
    return gid, out, drives, stats


def main(path=None, seasons=None, workers=WORKERS, bridge_path=None, drives_path=None):
    os.makedirs(OUT, exist_ok=True)
    seasons = seasons or SEASONS
    # Per-league defaults, so an NFL run cannot silently overwrite the college extracts.
    sfx = "" if LEAGUE == lg.CFB else f"_{LEAGUE}"
    path = path or f"{OUT}/scrimmage_plays{sfx}.csv"
    bridge_path = bridge_path or f"{OUT}/scrimmage_athlete{sfx}.csv"
    drives_path = drives_path or f"{OUT}/drives{sfx}.csv"
    stats = collections.Counter()
    per_season = collections.Counter()
    seen = set()
    nfact = nbridge = ndrives = 0
    drive_ids = set()
    t0 = time.time()

    with open(path, "w", newline="") as fh, \
         open(bridge_path, "w", newline="") as bh, \
         open(drives_path, "w", newline="") as dh:
        wr = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        wr.writeheader()
        bw = csv.DictWriter(bh, fieldnames=BRIDGE_COLUMNS, extrasaction="ignore")
        bw.writeheader()
        dw = csv.DictWriter(dh, fieldnames=DRIVE_COLUMNS, extrasaction="ignore")
        dw.writeheader()
        for season in seasons:
            gpath = f"{ESPN}/games_{season}.json"
            if not os.path.exists(gpath):
                print(f"  {season}: NO GAME LIST", flush=True)
                continue
            games = json.load(open(gpath))
            jobs = [(gid, g, season, LEAGUE) for gid, g in games.items()]
            ngames = 0
            with ProcessPoolExecutor(max_workers=workers) as ex:
                for gid, rows, dvs, s in ex.map(rows_for_game, jobs, chunksize=25):
                    stats.update(s)
                    if not rows and not s:
                        continue
                    ngames += 1
                    for d in dvs:
                        if d["drive_id"] in drive_ids:
                            stats["duplicate_drive_id"] += 1
                        elif d["drive_id"] is not None:
                            drive_ids.add(d["drive_id"])
                        dw.writerow(d)
                        ndrives += 1
                    for r in rows:
                        # ESPN sometimes gives two genuinely different plays in the same game
                        # the same sequenceNumber -- 281 collisions over 170 games, costing
                        # 335 real plays. Two examples from 2024, both unmistakably distinct:
                        #
                        #   401643714 / 105292723  "Emmett Brown pass complete to Nick Nash
                        #                           for 7 yds" and "Floyd Chalk IV run for 6"
                        #   401644776 / 101988002  a 9-yard run and a 2-yard loss
                        #
                        # Dropping the second one keeps play_uid perfectly seq-shaped at the
                        # cost of losing a play that happened, which is the wrong trade for a
                        # fact table of plays. The collision gets a '#2' suffix instead.
                        #
                        # Consequence to know: participants are keyed on sequenceNumber, so
                        # both rows of a collision inherit the same athletes and at most one
                        # of them is right. 335 rows out of 1.5M, flagged rather than hidden
                        # -- find them with  play_uid LIKE '%#%'.
                        uid = r["play_uid"]
                        st_n = r.pop("_st_seq_n", 0)
                        if st_n:
                            stats["collides_with_special_teams"] += 1
                            # Reserve every uid build_table will take for this sequence
                            # number: the bare one, plus '#2'..'#n' if it saw n such plays.
                            seen.add(uid)
                            for i in range(2, st_n + 1):
                                seen.add(f"{uid}#{i}")
                        st_hit = bool(st_n)
                        if uid in seen:
                            n = 2
                            while f"{uid}#{n}" in seen:
                                n += 1
                            r["play_uid"] = f"{uid}#{n}"
                            if not st_hit:         # counted once, under one heading
                                stats["duplicate_seq"] += 1
                        seen.add(r["play_uid"])
                        wr.writerow(r)
                        nfact += 1
                        per_season[season] += 1
                        for role, aid, ordinal in r.pop("_bridge", ()):
                            bw.writerow({"play_uid": r["play_uid"], "role": role,
                                         "athlete_id": aid, "ordinal": ordinal})
                            nbridge += 1
                            stats[f"role:{role}"] += 1
            print(f"  {season}: {ngames:,} games, {per_season[season]:,} plays", flush=True)

    print(f"\nwrote {nfact:,} rows -> {path}")
    print(f"      {nbridge:,} rows -> {bridge_path}")
    print(f"      {ndrives:,} rows -> {drives_path}   ({time.time() - t0:.0f}s)")
    print(f"\n{'season':8s} {'plays':>10s}")
    for s in seasons:
        print(f"{s:<8} {per_season[s]:>10,}")
    print(f"\n{'play_kind':<12s} {'rows':>10s}")
    for k in ("rush", "pass", "sack", "penalty", "other"):
        print(f"{k:<12s} {stats['kind:' + k]:>10,}")
    print(f"\nexcluded: {stats['admin']:,} administrative, "
          f"{stats['special_teams']:,} claimed by build_table.classify(), "
          f"{stats['conversion']:,} conversion attempts")
    print(f"outcome-typed plays resolved by participant role: "
          f"{stats['resolved_by_participant']:,} "
          f"(unresolved -> 'other': {stats['unresolved_outcome_type']:,})")
    if stats["offense_from_start_team"]:
        print(f"offense fell back to start.team.id on {stats['offense_from_start_team']:,} plays")
    print(f"\n{'bridge role':<18s} {'rows':>10s}")
    for k in sorted((k for k in stats if k.startswith("role:")), key=lambda x: -stats[x]):
        print(f"{k[5:]:<18s} {stats[k]:>10,}")
    if stats["bridge_dup_pair"]:
        print(f"  (dropped {stats['bridge_dup_pair']:,} repeated (role, athlete) pairs)")
    if stats["duplicate_drive_id"]:
        print(f"\nWARNING: ESPN drive_id repeats on {stats['duplicate_drive_id']:,} drives; "
              f"drive_uid is the key, drive_id is not unique")

    oor = {k: v for k, v in stats.items() if k.startswith("out_of_range:")}
    if oor:
        print("\nout-of-range values nulled (ESPN sentinels and feed corruption):")
        for k in sorted(oor, key=lambda x: -oor[x]):
            lo, hi = RANGE[k.split(":")[1]]
            print(f"  {k.split(':')[1]:<20s} {oor[k]:>8,}   legal {lo}-{hi}")
    if stats["collides_with_special_teams"]:
        print(f"sequenceNumber also claimed by a kick in pbp.special_teams_play, suffixed: "
              f"{stats['collides_with_special_teams']:,}")
    if stats["duplicate_seq"]:
        print(f"duplicate sequenceNumber, kept with a '#n' suffix: {stats['duplicate_seq']:,} "
              f"(find them with  play_uid LIKE '%#%')")
    if stats["unreadable"]:
        print(f"WARNING: unreadable summaries = {stats['unreadable']:,}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", help="write here instead of data/out/scrimmage_plays.csv")
    ap.add_argument("--out-bridge", help="default data/out/scrimmage_athlete.csv")
    ap.add_argument("--out-drives", help="default data/out/drives.csv")
    ap.add_argument("--seasons", help="comma-separated, e.g. 2026")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--league", default=lg.CFB, choices=sorted(lg.SPEC))
    a = ap.parse_args()
    use(a.league)
    main(a.out, [int(x) for x in a.seasons.split(",")] if a.seasons else None, a.workers,
         a.out_bridge, a.out_drives)
