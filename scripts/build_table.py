"""Build the unified special-teams fact table from ESPN play-by-play, 2014 onward.

ESPN is the single spine for all twelve seasons. An earlier build drew 2016-2021 from a
bulk archive that proved to be an incomplete extract of ESPN -- it dropped 78 whole games,
~6% of plays within the games it did have, and 10-22% of kickoffs, which depressed every
pre-2022 kickoff rate and made cross-era comparison invalid. Nothing outside ESPN feeds
the table now.

Team convention, verified against 105k plays with zero exceptions:
  start.team.id is ALWAYS the KICKING team -- on punts and field goals it equals the
  offense, on kickoffs it equals the defense. Do not "simplify" this to pos_team.

That verification covers the three kicks, which are read straight off the play. PAT and
two-point rows are DERIVED from the touchdown play instead, and the team there cannot be
taken from start.team.id in either direction: on an ordinary touchdown the scorer is the
offense that already holds it, on a pick-six or a punt return it is the other side, and
ESPN types both as "Punt" or "Sack" often enough that a type whitelist will not separate
them. `emit_pat` reads the scorer off the scoreboard instead -- see the note there.

Output is a CSV for `\\copy` into Postgres via sql/load.sql, which casts through an all-text
staging table.

    python build_table.py                              every season -> data/out/st_plays.csv
    python build_table.py --seasons 2026 --out x.csv   one season, for the in-season update
"""
import sys, os, gzip, json, csv, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from st_parser import parse_field_goal, parse_punt, parse_kickoff, parse_pat

HOME = os.path.expanduser("~/projects/cfb-special-teams")
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
            if row["play_uid"] in seen:
                return
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
                        if p.get("scoringPlay") or "kick attempt" in (text or "").lower():
                            # homeScore / awayScore are the score AFTER the play, so the
                            # side that gained points on it is the side that scored. A
                            # conversion that scores nothing (a failed try, a penalty on
                            # the attempt) leaves both deltas at 0 and falls through to
                            # start.team.id, which is the kicker's own offense there.
                            # 193 of 58,542 conversions take that fallback because ESPN's
                            # own scoreboard does not move on the play; they keep whatever
                            # start.team.id says, right or wrong.
                            dh = (hs - prev_home) if hs is not None else 0
                            da = (as_score - prev_away) if as_score is not None else 0
                            scorer = home if dh > da else (away if da > dh else None)
                            r = emit_pat(base, uid, text, scorer)
                            if r:
                                w(r); stats[(season, r["play_kind"])] += 1
                        if hs is not None:
                            prev_home = hs
                        if as_score is not None:
                            prev_away = as_score
                ngames += 1
            print(f"  {season}: {ngames:,} games", flush=True)

    print(f"\nwrote {len(seen):,} rows -> {path}")
    hdr = f"{'season':8s} {'kickoff':>9s} {'punt':>8s} {'field_goal':>11s} {'pat':>8s} {'two_point':>10s} {'TOTAL':>9s}"
    print(hdr)
    for s in seasons:
        row = [stats[(s, k)] for k in ("kickoff", "punt", "field_goal", "pat", "two_point")]
        print(f"{s:<8} {row[0]:9,} {row[1]:8,} {row[2]:11,} {row[3]:8,} {row[4]:10,} {sum(row):9,}")


if __name__ == "__main__":
    # `--out somewhere.csv` writes elsewhere, so a change can be diffed against the live
    # extract before anything goes near Postgres.
    a = sys.argv[1:]
    main(a[a.index("--out") + 1] if "--out" in a else None,
         [int(x) for x in a[a.index("--seasons") + 1].split(",")] if "--seasons" in a else None)
