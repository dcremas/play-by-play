"""Build the unified special-teams fact table from ESPN play-by-play, 2014 onward.

ESPN is the single spine for all twelve seasons. An earlier build drew 2016-2021 from a
bulk archive that proved to be an incomplete extract of ESPN -- it dropped 78 whole games,
~6% of plays within the games it did have, and 10-22% of kickoffs, which depressed every
pre-2022 kickoff rate and made cross-era comparison invalid. Nothing outside ESPN feeds
the table now.

Team convention, verified against 105k plays with zero exceptions:
  start.team.id is ALWAYS the KICKING team -- on punts and field goals it equals the
  offense, on kickoffs it equals the defense. Do not "simplify" this to pos_team.

That verification covers the three kicks, which are read straight off the play. Most PAT and
two-point rows are DERIVED from the touchdown play instead, and the team there cannot be
taken from start.team.id in either direction: on an ordinary touchdown the scorer is the
offense that already holds it, on a pick-six or a punt return it is the other side, and
ESPN types both as "Punt" or "Sack" often enough that a type whitelist will not separate
them. `emit_pat` reads the scorer off the scoreboard instead -- see the note there.

A minority of conversions get their own play row from ESPN rather than living inside the
touchdown text, and those are read directly -- see STANDALONE_CONV. That is also where
play_kind='defensive_conversion' comes from: the defence returning a blocked PAT for two is
a scoring event of its own, not something the offence attempted.

Output is a CSV for `\\copy` into Postgres via sql/load.sql, which casts through an all-text
staging table.

    python build_table.py                              every season -> data/out/st_plays.csv
    python build_table.py --seasons 2026 --out x.csv   one season, for the in-season update
"""
import sys, os, gzip, json, csv, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from st_parser import parse_field_goal, parse_punt, parse_kickoff, parse_pat

HOME = os.path.expanduser("~/projects/cfb-pbp")
ESPN, OUT = f"{HOME}/data/espn", f"{HOME}/data/out"
SEASONS = [2014, 2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]

KIND_BY_TYPE = {
    "Punt": "punt", "Blocked Punt": "punt", "Punt Return Touchdown": "punt",
    "Blocked Punt Touchdown": "punt",
    "Kickoff": "kickoff", "Kickoff Return (Offense)": "kickoff",
    "Kickoff Return Touchdown": "kickoff",
    "Field Goal Good": "field_goal", "Field Goal Missed": "field_goal",
    "Blocked Field Goal": "field_goal", "Blocked Field Goal Touchdown": "field_goal",
    "Missed Field Goal Return": "field_goal", "Missed Field Goal Return Touchdown": "field_goal",
}
# 545 special-teams plays hide under playType='Penalty'; type alone would miss them.
TEXT_HINT = [("punt", "punt"), ("kickoff", "kickoff"), ("kick for", "kickoff"),
             ("onside kick", "kickoff"), ("on-side kick", "kickoff"),
             ("field goal", "field_goal"), (" fg ", "field_goal")]
PARSER = {"punt": parse_punt, "kickoff": parse_kickoff, "field_goal": parse_field_goal}

COLUMNS = ["play_uid", "source", "game_id", "season", "week", "season_type", "play_kind",
           "period", "clock_secs_period", "wallclock_utc", "down", "distance",
           "yards_to_goal", "kicking_team_id", "receiving_team_id", "is_home_kicking",
           "score_diff_kicking", "fg_distance_yds", "fg_made", "punt_gross_yds",
           "punt_net_yds", "kickoff_yds", "return_yds", "returned", "touchback", "onside",
           "fair_catch", "downed", "out_of_bounds", "kick_blocked", "returned_for_td",
           "converted", "two_point_type", "miss_reason", "negated_by_penalty",
           "kicker_name", "returner_name", "blocker_name", "play_text", "parse_confidence"]


def same_team(a, b):
    if a is None or b is None:
        return False
    try:
        return int(float(a)) == int(float(b))
    except (TypeError, ValueError):
        return str(a) == str(b)


def as_team(x):
    if x is None:
        return None
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return x


def clock_secs(disp):
    if not disp or ":" not in str(disp):
        return None
    m, _, s = str(disp).partition(":")
    try:
        return int(m) * 60 + int(s)
    except ValueError:
        return None


def advance_score(prev_home, prev_away, hs, aw):
    """Carry the running score forward one play. Returns (home, away, d_home, d_away).

    ESPN's homeScore/awayScore is the score AFTER the play -- verified on clean games, where
    a touchdown row already carries the points and the PAT folded into its text. But the feed
    is not consistent about it: 6,470 rows report a STALE, pre-scoring snapshot while the game
    clock advances past them, concentrated on Timeout and Penalty rows. A further 765 rows
    are simply out of chronological order. Read literally, the running score steps BACKWARD
    at least once in 3,553 of 10,470 games.

    Taken at face value that produced 7,237 negative point deltas and 14,002 plays credited
    with points they did not score -- the stale row's deficit reappearing as a phantom gain on
    whatever followed it.

    The repair is one invariant: a score never goes down. Clamping each team's running total
    to its own maximum absorbs a stale snapshot into the value already known, so the play that
    follows it shows no phantom gain. Measured over 1,868,595 plays:

        negative deltas          7,237  ->      0
        illegal deltas           8,458  ->    561
        phantom point rows      14,002  ->  1,589

    and the reconstructed final score matches the official one in games_<season>.json for
    10,053 of 10,301 games, against 10,021 before. Sorting the plays into clock order first
    was tried and adds nothing on top of this -- the ordering fault is the smaller half, and
    a sort large enough to fix it broke as many games as it repaired.

    What remains: ~3,200 rows (0.17%) where the delta still disagrees with ESPN's own
    scoringPlay flag in one direction or the other. See README "Known limits".

    One thing clamping costs, measured rather than assumed. On a return touchdown whose row
    is stale, both clamped deltas are 0, and `emit_pat` then falls back to possession -- which
    on a return touchdown is the team that was scored AGAINST. 38 conversions land on the
    wrong team that way. Feeding the raw (unclamped) deltas in as a tiebreak was tried to
    recover them and made things considerably worse -- 99.81% -> 99.56% against game-local
    ground truth, flipping 182 correct rows -- because once `prev_*` is a repaired running
    maximum, a raw negative delta is a property of the repair rather than a signal about who
    scored. Clamped-only is the better answer and the 38 stay wrong. See README "Known limits".
    """
    nh = max(prev_home, hs) if hs is not None else prev_home
    na = max(prev_away, aw) if aw is not None else prev_away
    return nh, na, nh - prev_home, na - prev_away


def classify(play_type, text):
    k = KIND_BY_TYPE.get(play_type)
    if k:
        return k
    lo = (text or "").lower()
    for needle, kind in TEXT_HINT:
        if needle in lo:
            return kind
    return None


def blank():
    return {c: None for c in COLUMNS}


def finish(row, kind, text, parsed):
    row["play_kind"] = kind
    row["play_text"] = text
    for k, v in parsed.items():
        if k in row:
            row[k] = v
    row["parse_confidence"] = parsed.get("parse_confidence")
    return row


def emit_pat(base, uid, text, scoring_team):
    p = parse_pat(text)
    if not p:
        return None
    r = dict(base)
    r["play_uid"] = f"{uid}:pat"
    r["play_kind"] = p["play_kind"]
    r["converted"] = p.get("converted")
    r["two_point_type"] = p.get("two_point_type")
    r["kick_blocked"] = p.get("kick_blocked")
    r["kicker_name"] = p.get("kicker_name")
    r["parse_confidence"] = p.get("parse_confidence")
    r["play_text"] = text
    # A conversion belongs to the team that just scored, which is read off the scoreboard:
    # `scoring_team` is whichever side's points went up on this play. On an ordinary
    # touchdown that is already start.team.id and nothing moves; on a defensive or return
    # touchdown it is the other side, so the two ids swap. Deriving it from possession
    # instead -- swapping unconditionally, as this did until 2026-08-30 -- put ~94% of all
    # 58,535 conversions on the opponent, because the offense that scores a normal
    # touchdown is exactly the offense that had the ball at the snap.
    if scoring_team is not None and not same_team(scoring_team, base["kicking_team_id"]):
        r["kicking_team_id"], r["receiving_team_id"] = base["receiving_team_id"], base["kicking_team_id"]
    r["is_home_kicking"] = (None if r["kicking_team_id"] is None or base["_home"] is None
                            else same_team(r["kicking_team_id"], base["_home"]))
    for f in ("fg_distance_yds", "punt_gross_yds", "kickoff_yds", "return_yds", "touchback",
              "onside", "fair_catch", "downed", "out_of_bounds", "returned", "punt_net_yds",
              "fg_made", "returned_for_td", "miss_reason"):
        r[f] = None
    return r


# ESPN sometimes emits a conversion as its OWN play row rather than only folding it into the
# touchdown text. emit_pat above fires on `scoringPlay or "kick attempt" in text`, which is
# read off the TOUCHDOWN play, so it never sees these -- 123 plays that were in neither fact
# table until 2026-09-08. Two unrelated families, and they must not be merged:
#
#   Two Point Pass / Two Point Rush   48 rows, 17 games, overwhelmingly overtime. A real
#                                     two-point attempt with its own sequenceNumber. 42 of
#                                     the 48 have no derived sibling anywhere near them.
#   Defensive 2pt Conversion          75 rows. NOT a conversion attempt -- it is the DEFENCE
#                                     returning a blocked or failed PAT for two points, a
#                                     separate scoring event that follows the touchdown:
#                                       [Rushing Touchdown] "... (Z. Gonzalez BLOCKED)"
#                                       [Defensive 2pt Conversion] "Brandon Branch return..."
#                                     The touchdown already produced a 'pat' row recording the
#                                     block; this row records who scored off it. Filing it as
#                                     two_point would claim the offence attempted something it
#                                     did not.
#
# These carry their own sequenceNumber, so their play_uid cannot collide with the ':pat' rows
# derived from the touchdown play, and build_scrimmage.CONVERSION excludes the same three
# types so nothing lands in both facts.
STANDALONE_CONV = {"Two Point Pass": "two_point", "Two Point Rush": "two_point",
                   "Defensive 2pt Conversion": "defensive_conversion"}
_2PT_TYPE = {"Two Point Pass": "pass", "Two Point Rush": "rush"}


def emit_standalone_conv(base, uid, play_type, text, scoring, scorer, home, away):
    """A conversion ESPN gave its own play row. Returns a fact row, or None."""
    kind = STANDALONE_CONV[play_type]
    r = dict(base)
    r["play_uid"] = uid
    r["play_kind"] = kind
    r["play_text"] = text
    lo = (text or "").lower()
    if kind == "two_point":
        # Ten of these read only "Two-Point Conversion failed" and three carry no text at
        # all, so the scoreboard is the better witness than the prose: two points moving is
        # the definition of a successful try.
        r["converted"] = bool(scoring) and scorer is not None
        if "fail" in lo or "no good" in lo or "intercepted" in lo:
            r["converted"] = False
        r["two_point_type"] = _2PT_TYPE.get(play_type)
    else:
        # The defence converted; the offence did not. `converted` describes the team on the
        # row, and the team on the row is whoever scored.
        r["converted"] = True
        r["two_point_type"] = None
    # The team on the row is the one that scored. emit_pat SWAPS the two ids to achieve that,
    # which works there because a touchdown play always has a start.team.id to swap against.
    # These do not: start.team.id is NULL on every overtime conversion ESPN emits standalone,
    # and a swap against NULL silently assigns all of them to the same team -- all three
    # conversions in the 9OT Illinois-Penn State game came out as Penn State before this.
    # So assign outright rather than swapping.
    if scorer is not None:
        r["kicking_team_id"] = scorer
        r["receiving_team_id"] = away if same_team(scorer, home) else home
    # else: no points moved and start.team.id stands -- the attempting offence, which is
    # right for a failed try.
    r["is_home_kicking"] = (None if r["kicking_team_id"] is None or base["_home"] is None
                            else same_team(r["kicking_team_id"], base["_home"]))
    r["parse_confidence"] = "exact" if text else "partial"
    return r


def main(path=None, seasons=None):
    os.makedirs(OUT, exist_ok=True)
    seasons = seasons or SEASONS
    stats = collections.Counter()
    seen = set()
    path = path or f"{OUT}/st_plays.csv"

    with open(path, "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        wr.writeheader()

        def w(row):
            # ESPN reuses one sequenceNumber for two different plays -- 281 collisions inside
            # the scrimmage set alone, and they reach here too. This used to `return`, which
            # silently dropped the second play; a real kickoff vanished that way the first
            # time the standalone-conversion rows were added, because the conversion sits in
            # an out-of-order drive and got written first. Suffix instead, matching
            # build_scrimmage.py, so no play is ever lost. Find them with play_uid LIKE '%#%'.
            uid = row["play_uid"]
            if uid in seen:
                n = 2
                while f"{uid}#{n}" in seen:
                    n += 1
                row["play_uid"] = f"{uid}#{n}"
                stats["duplicate_seq"] += 1
            seen.add(row["play_uid"])
            wr.writerow(row)

        for season in seasons:
            gpath = f"{ESPN}/games_{season}.json"
            if not os.path.exists(gpath):
                print(f"  {season}: NO GAME LIST", flush=True)
                continue
            games = json.load(open(gpath))
            ngames = 0
            for gid, g in games.items():
                sp = f"{ESPN}/summaries/{gid}.json.gz"
                if not os.path.exists(sp):
                    continue
                try:
                    with gzip.open(sp, "rt") as f:
                        d = json.load(f)
                except Exception:
                    continue
                home = as_team(next((t["id"] for t in g["teams"] if t["home_away"] == "home"), None))
                away = as_team(next((t["id"] for t in g["teams"] if t["home_away"] == "away"), None))
                prev_home = prev_away = 0
                deferred = []
                for dr in d.get("drives") or []:
                    for p in dr.get("plays") or []:
                        text = p.get("text")
                        kind = classify((p.get("type") or {}).get("text"), text)
                        st = p.get("start") or {}
                        kick = as_team((st.get("team") or {}).get("id"))
                        recv = away if same_team(kick, home) else home
                        hs, as_score = p.get("homeScore"), p.get("awayScore")
                        kick_is_home = same_team(kick, home) if kick is not None else None
                        diff = None
                        if hs is not None and as_score is not None and kick_is_home is not None:
                            diff = int((hs - as_score) if kick_is_home else (as_score - hs))
                        seq = p.get("sequenceNumber") or p.get("id")
                        uid = f"espn:{gid}:{seq}"
                        base = {**blank(),
                                "source": "espn", "game_id": gid, "season": season,
                                "week": g["week"],
                                "season_type": "postseason" if g["season_type"] == 3 else "regular",
                                "period": (p.get("period") or {}).get("number"),
                                "clock_secs_period": clock_secs((p.get("clock") or {}).get("displayValue")),
                                "wallclock_utc": p.get("wallclock"), "down": st.get("down"),
                                "distance": st.get("distance"),
                                "yards_to_goal": st.get("yardsToEndzone"),
                                "kicking_team_id": kick, "receiving_team_id": recv,
                                "is_home_kicking": kick_is_home, "score_diff_kicking": diff,
                                "_home": home}
                        if kind:
                            r = finish({**base, "play_uid": uid}, kind, text, PARSER[kind](text))
                            w(r); stats[(season, kind)] += 1
                        # One repaired advance per play, shared by both branches below and
                        # by the running total at the bottom of the loop.
                        nh, na, dh, da = advance_score(prev_home, prev_away, hs, as_score)
                        scorer = home if dh > da else (away if da > dh else None)

                        ptype = (p.get("type") or {}).get("text") or ""
                        if ptype in STANDALONE_CONV:
                            sc = scorer
                            # Held back and written after every kick in this game. A kick and
                            # a conversion can share a sequenceNumber, and the kick must keep
                            # the bare play_uid it already has in the warehouse.
                            deferred.append(emit_standalone_conv(
                                base, uid, ptype, text, p.get("scoringPlay"), sc, home, away))
                        if p.get("scoringPlay") or "kick attempt" in (text or "").lower():
                            # homeScore / awayScore are the score AFTER the play, so the
                            # side that gained points on it is the side that scored. A
                            # conversion that scores nothing (a failed try, a penalty on
                            # the attempt) leaves both deltas at 0 and falls through to
                            # start.team.id, which is the kicker's own offense there.
                            # 193 of 58,542 conversions take that fallback because ESPN's
                            # own scoreboard does not move on the play; they keep whatever
                            # start.team.id says, right or wrong.
                            r = emit_pat(base, uid, text, scorer)
                            if r:
                                w(r); stats[(season, r["play_kind"])] += 1
                        prev_home, prev_away = nh, na
                for r in deferred:
                    w(r); stats[(season, r["play_kind"])] += 1
                ngames += 1
            print(f"  {season}: {ngames:,} games", flush=True)

    if stats["duplicate_seq"]:
        print(f"\nduplicate sequenceNumber, kept with a '#n' suffix: "
              f"{stats['duplicate_seq']:,}")
    print(f"\nwrote {len(seen):,} rows -> {path}")
    kinds = ("kickoff", "punt", "field_goal", "pat", "two_point", "defensive_conversion")
    print(f"{'season':8s} {'kickoff':>9s} {'punt':>8s} {'field_goal':>11s} {'pat':>8s} "
          f"{'two_point':>10s} {'def_conv':>9s} {'TOTAL':>9s}")
    for s in seasons:
        row = [stats[(s, k)] for k in kinds]
        print(f"{s:<8} {row[0]:9,} {row[1]:8,} {row[2]:11,} {row[3]:8,} {row[4]:10,} "
              f"{row[5]:9,} {sum(row):9,}")


if __name__ == "__main__":
    # `--out somewhere.csv` writes elsewhere, so a change can be diffed against the live
    # extract before anything goes near Postgres.
    a = sys.argv[1:]
    main(a[a.index("--out") + 1] if "--out" in a else None,
         [int(x) for x in a[a.index("--seasons") + 1].split(",")] if "--seasons" in a else None)
