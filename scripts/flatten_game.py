"""Flatten one ESPN game summary into a CSV of plays, with athlete ids joined on.

A quick look-at-one-game tool, not part of the build. `build_table.py` reads the same
summaries but keeps only special teams and parses the text into 40 measured columns; this
keeps every play and parses nothing, so it is the right thing to eyeball when you want to
see what the raw feed actually said before the parser touched it.

Two files back one game id and this reads both:

    summaries/<gid>.json.gz      drives -> plays, the play text
    participants/<gid>.json.gz   sequenceNumber -> [[role, athlete_id], ...]

They join on sequenceNumber, which is also how play_uid is built (espn:<gid>:<seq>), so a
row here lines up with a row in st_plays.csv. Participants only exist from 2014 on; for an
older game the column is simply empty.

One asymmetry against st_plays.csv: its `<uid>:pat` rows are DERIVED from the touchdown
play, not read off a play of their own, so they have no counterpart here. Checked on
400547640 -- all 29 kick rows join, the 5 that do not are exactly those PATs.

    python flatten_game.py 400547640                 -> stdout
    python flatten_game.py 400547640 --out game.csv
    python flatten_game.py 400547640 --long          one row per play-athlete instead

Grain: one row per play by default. --long explodes to one row per (play, role, athlete),
which is the shape to group by when you are counting a player's touches; plays with no
athlete attached drop out of it.
"""
import os, sys, gzip, json, csv, argparse

HOME = os.path.expanduser("~/projects/cfb-pbp")
ESPN = f"{HOME}/data/espn"

COLUMNS = ["play_uid", "game_id", "drive_number", "drive_id", "drive_team_id",
           "drive_result", "sequence_number", "period", "clock", "clock_secs_period",
           "wallclock_utc", "play_type_id", "play_type", "offense_team_id",
           "defense_team_id", "start_team_id", "down", "distance", "yard_line",
           "yards_to_endzone", "end_down", "end_distance", "end_yard_line",
           "end_yards_to_endzone", "stat_yardage", "home_score", "away_score",
           "scoring_play", "is_penalty", "is_turnover", "participants", "play_text"]
LONG_DROP = ["participants"]
LONG_ADD = ["role", "athlete_id"]


def clock_secs(disp):
    """'14:55' -> seconds remaining in the period. Same rule build_table.py uses."""
    if not disp or ":" not in disp:
        return None
    m, s = disp.split(":")[:2]
    try:
        return int(m) * 60 + int(s)
    except ValueError:
        return None


def load(kind, gid):
    path = f"{ESPN}/{kind}/{gid}.json.gz"
    if not os.path.exists(path):
        return None
    with gzip.open(path, "rt") as f:
        return json.load(f)


def side(play, which):
    """teamParticipants carries offense/defense per play; start.team.id does not.

    On a kick, start.team.id is the KICKING team, which is the defense on a kickoff -- see
    the team-convention note in build_table.py. Read offense off teamParticipants instead
    so the two columns mean what they say.
    """
    for t in play.get("teamParticipants") or []:
        if t.get("type") == which:
            return t.get("id")
    return None


def rows(gid, summary):
    # Participants are absent for 2013 and earlier, and for any game not fetched yet; the
    # column just comes out empty rather than the run failing.
    parts = load("participants", gid) or {}

    for i, dr in enumerate(summary.get("drives") or [], start=1):
        team = ((dr.get("team") or {}).get("id"))
        for p in dr.get("plays") or []:
            seq = p.get("sequenceNumber") or p.get("id")
            st, en = p.get("start") or {}, p.get("end") or {}
            pairs = parts.get(str(seq)) or []
            yield {
                "play_uid": f"espn:{gid}:{seq}",
                "game_id": gid,
                "drive_number": i,
                "drive_id": dr.get("id"),
                "drive_team_id": team,
                "drive_result": dr.get("result"),
                "sequence_number": seq,
                "period": (p.get("period") or {}).get("number"),
                "clock": (p.get("clock") or {}).get("displayValue"),
                "clock_secs_period": clock_secs((p.get("clock") or {}).get("displayValue")),
                "wallclock_utc": p.get("wallclock"),
                "play_type_id": (p.get("type") or {}).get("id"),
                "play_type": (p.get("type") or {}).get("text"),
                "offense_team_id": side(p, "offense"),
                "defense_team_id": side(p, "defense"),
                "start_team_id": (st.get("team") or {}).get("id"),
                "down": st.get("down"),
                "distance": st.get("distance"),
                "yard_line": st.get("yardLine"),
                "yards_to_endzone": st.get("yardsToEndzone"),
                "end_down": en.get("down"),
                "end_distance": en.get("distance"),
                "end_yard_line": en.get("yardLine"),
                "end_yards_to_endzone": en.get("yardsToEndzone"),
                "stat_yardage": p.get("statYardage"),
                "home_score": p.get("homeScore"),
                "away_score": p.get("awayScore"),
                "scoring_play": p.get("scoringPlay"),
                "is_penalty": p.get("isPenalty"),
                "is_turnover": p.get("isTurnover"),
                "participants": ";".join(f"{r}:{a}" for r, a in pairs),
                "play_text": p.get("text"),
                "_pairs": pairs,
            }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("game_id", help="ESPN game id, e.g. 400547640")
    ap.add_argument("--out", help="write here instead of stdout")
    ap.add_argument("--long", action="store_true",
                    help="one row per play-athlete; drops plays with no athlete")
    a = ap.parse_args()

    gid = os.path.basename(a.game_id).split(".")[0]   # a path works as well as a bare id
    summary = load("summaries", gid)                  # fail before opening the output file
    if summary is None:
        sys.exit(f"no summary for game {gid} under {ESPN}/summaries/")
    cols = [c for c in COLUMNS if c not in LONG_DROP] + LONG_ADD if a.long else COLUMNS

    f = open(a.out, "w", newline="") if a.out else sys.stdout
    try:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        n = 0
        for r in rows(gid, summary):
            if not a.long:
                w.writerow(r); n += 1
                continue
            # A role repeats on a play -- two receivers, three tacklers -- so each pair is
            # its own row rather than being folded into one.
            for role, athlete in r["_pairs"]:
                w.writerow({**r, "role": role, "athlete_id": athlete}); n += 1
    finally:
        if a.out:
            f.close()
    if a.out:
        print(f"{n} rows -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
