"""Which finished games ESPN has that local Postgres does not. Read-only; changes nothing.

    .venv/bin/python scripts/new_games.py               both leagues, the last 10 days
    .venv/bin/python scripts/new_games.py --days 3
    .venv/bin/python scripts/new_games.py --ids         "<league> <game_id>" per new game

This is the trigger for scripts/refresh.sh. Games are no longer a Saturday-and-Sunday thing:
college plays Tuesday through Saturday from October on, and the NFL Thursday, Sunday and
Monday. A run on a fixed day leaves each midweek game invisible for most of a week, and a
run on every day re-pulls the whole season for nothing on the days nobody played.

The comparison is ESPN's completed games against pbp.fact_game, by game id. fact_game is
written from the games list itself rather than from the plays, so a finished game is in it
once it has been loaded at all -- that is what makes "missing from fact_game" mean "never
loaded" and not "loaded, but ESPN had no plays for it".

ESPN is asked one DAY at a time. The scoreboard's `dates=` range form returns HTTP 400 for
both leagues (checked 2026-10-15), and a per-week sweep is what `fetch_espn.py games` already
does at 22 calls a league. Days are US Eastern, which is how ESPN files them: a 9:30pm
Pacific kickoff is the previous day's game, not the next.

The filters are fetch_espn.stage_games's, because a game it would never load must never
count as missing here -- or every run would see it, run the whole pipeline, and see it again:
completed only, regular season and postseason only (the NFL preseason is seasontype 1),
FBS only for college (groups=80), and no Pro Bowl squads.
"""
import argparse, json, os, subprocess, sys, time, urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import league as lg


def get(url, tries=3):
    # urllib's default User-Agent, deliberately -- see the note in fetch_espn.py.
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=30) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def espn_days(league, days):
    """Every event ESPN files on the last `days` Eastern dates, today included."""
    today = datetime.now(ZoneInfo("America/New_York")).date()
    out = {}
    for k in range(days, -1, -1):
        d = (today - timedelta(days=k)).strftime("%Y%m%d")
        url = (f"{lg.site_base(league)}/scoreboard?dates={d}"
               f"{lg.spec(league)['groups']}&limit=400")
        for e in get(url).get("events", []):
            if e.get("id") and e.get("competitions"):
                out[e["id"]] = e
    return out


def loaded(db, league, seasons):
    sql = (f"SELECT game_id FROM pbp.fact_game WHERE league = '{league}' "
           f"AND season IN ({','.join(str(s) for s in seasons)})")
    r = subprocess.run(["psql", "-d", db, "-Atc", sql], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"cannot read pbp.fact_game: {r.stderr.strip()}")
    return set(r.stdout.split())


def check(league, days, db):
    """(finished-but-not-loaded events, events not yet final) for one league."""
    drop = lg.spec(league)["exclude_teams"]
    final, pending = [], []
    for e in espn_days(league, days).values():
        if (e.get("season") or {}).get("type") not in (2, 3):
            continue
        teams = {str(t.get("team", {}).get("id"))
                 for t in e["competitions"][0].get("competitors", [])}
        if drop and teams & drop:
            continue
        (final if e["status"]["type"].get("completed") else pending).append(e)
    seasons = sorted({e["season"]["year"] for e in final}) or [0]
    have = loaded(db, league, seasons)
    new = [e for e in final if e["id"] not in have]
    return sorted(new, key=lambda e: e["date"]), sorted(pending, key=lambda e: e["date"])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=10,
                    help="how far back to look, in Eastern calendar days (default 10)")
    ap.add_argument("--league", action="append", choices=sorted(lg.SPEC),
                    help="check only this league (repeatable; default both)")
    ap.add_argument("--db", default="pbp")
    ap.add_argument("--ids", action="store_true",
                    help="print only '<league> <game_id>' per game to load, for "
                         "scripts/refresh.sh")
    a = ap.parse_args()

    for league in a.league or list(lg.SPEC):
        new, pending = check(league, a.days, a.db)
        if a.ids:
            for e in new:
                print(league, e["id"])
            continue
        label = lg.spec(league)["label"]
        print(f"{label}: {len(new)} finished game(s) not loaded yet")
        for e in new:
            print(f"  NEW      {e['id']}  {e['date']}  {e['shortName']}")
        for e in pending:
            print(f"  not final {e['id']}  {e['date']}  {e['shortName']}  "
                  f"({e['status']['type']['description']})")


if __name__ == "__main__":
    main()
