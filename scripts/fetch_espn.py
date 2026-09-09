"""Fetch 2022-2025 FBS play-by-play from ESPN.

The bulk archive this project started from was parsed ESPN data (verified: identical
column shapes and identical play-text dialect), so pulling ESPN directly for the seasons
it never covered means st_parser.py works unchanged -- no CFBD key, no new dialect.

Stages (each resumable; already-downloaded files are skipped):
    python fetch_espn.py games       -> data/espn/games_<season>.json
    python fetch_espn.py summaries   -> data/espn/summaries/<game_id>.json.gz

For a season still being played, the skip-if-cached rule is wrong in both stages: the
games list would freeze at whatever had finished the first time it ran, and a game that
ESPN later corrects would keep its original box score forever. Two flags cover that:

    python fetch_espn.py games     --refresh          rebuild the games list from the API
    python fetch_espn.py summaries --refresh-days 21  re-pull games that kicked off recently

scripts/update_season.py passes both. A finished season should be fetched without them.

ESPN's API is undocumented. Concurrency is kept low and every call retries with backoff;
this is a background bulk pull, not something to hammer.
"""
import sys, os, json, gzip, time, random, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
OUT = os.path.expanduser("~/projects/cfb-pbp/data/espn")
SUMS = f"{OUT}/summaries"
SEASONS = [int(x) for x in os.environ.get("SEASONS", "2022,2023,2024,2025").split(",")]
WEEKS = [(2, w) for w in range(1, 18)] + [(3, w) for w in range(1, 6)]
WORKERS = 6
# ESPN 403s browser-like User-Agent strings on this endpoint but serves urllib's default.
# Counterintuitive, but do not "fix" this by adding a realistic UA -- it breaks the fetch.


def get(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep((2 ** i) + random.random())
    return None


def recent(iso, days):
    """True if an ESPN kickoff timestamp falls inside the last `days` days."""
    if not iso or days <= 0:
        return False
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    return t >= datetime.now(timezone.utc) - timedelta(days=days)


def stage_games(refresh=False):
    os.makedirs(OUT, exist_ok=True)
    for season in SEASONS:
        path = f"{OUT}/games_{season}.json"
        if os.path.exists(path) and not refresh:
            print(f"{season}: cached ({len(json.load(open(path))):,} games)", flush=True); continue
        before = len(json.load(open(path))) if os.path.exists(path) else 0
        games = {}
        for stype, wk in WEEKS:
            url = f"{BASE}/scoreboard?dates={season}&seasontype={stype}&week={wk}&groups=80&limit=400"
            try:
                d = get(url)
            except Exception as e:
                print(f"  {season} t{stype} w{wk}: FAILED {e}"); continue
            for e in d.get("events", []):
                # 2014 t2 w1 carries one empty {} event; the feed does this occasionally.
                if not e.get("id") or not e.get("competitions"):
                    continue
                if not (e.get("status") or {}).get("type", {}).get("completed"):
                    continue
                c = e["competitions"][0]
                games[e["id"]] = {
                    "game_id": e["id"], "season": season, "season_type": stype, "week": wk,
                    "date": e.get("date"), "name": e.get("name"),
                    "neutral_site": c.get("neutralSite"),
                    "conference_competition": c.get("conferenceCompetition"),
                    "venue_id": (c.get("venue") or {}).get("id"),
                    # The ONLY place ESPN states a roof. `summary.gameInfo.venue` carries
                    # `grass` but not `indoor`, and build_dims reads venue from the summary,
                    # so without capturing it here dim_venue can record surface and not
                    # roof -- which is what blocked the weather phase. Venue-grain, and a
                    # retractable roof reads true whether or not it was open on the day.
                    "venue_indoor": (c.get("venue") or {}).get("indoor"),
                    "teams": [{"id": t["team"]["id"], "name": t["team"].get("displayName"),
                               "home_away": t.get("homeAway"), "score": t.get("score")}
                              for t in c.get("competitors", [])],
                }
            time.sleep(0.15)
        json.dump(games, open(path, "w"))
        delta = f"  (+{len(games) - before:,})" if refresh else ""
        print(f"{season}: {len(games):,} completed games{delta}", flush=True)


def stage_summaries(refresh_days=0):
    os.makedirs(SUMS, exist_ok=True)
    todo, restale = [], 0
    for season in SEASONS:
        p = f"{OUT}/games_{season}.json"
        if not os.path.exists(p):
            print(f"missing {p}; run `games` first"); return
        for gid, g in json.load(open(p)).items():
            if not os.path.exists(f"{SUMS}/{gid}.json.gz"):
                todo.append(gid)
            elif recent(g.get("date"), refresh_days):
                todo.append(gid); restale += 1
    stale = f", {restale:,} of them re-pulls inside {refresh_days}d" if restale else ""
    print(f"{len(todo):,} summaries to fetch ({WORKERS} workers){stale}")
    if not todo:
        return
    ok = fail = 0
    t0 = time.time()

    def one(gid):
        d = get(f"{BASE}/summary?event={gid}")
        # Keep only what we need; a full summary is ~1 MB and we are storing thousands.
        slim = {"drives": d.get("drives", {}).get("previous", []),
                "gameInfo": d.get("gameInfo", {}),
                "header": {"season": d.get("header", {}).get("season"),
                           "week": d.get("header", {}).get("week"),
                           "competitions": [{"date": c.get("date"),
                                             "neutralSite": c.get("neutralSite"),
                                             "conferenceCompetition": c.get("conferenceCompetition"),
                                             "competitors": [{"id": x.get("id"),
                                                              "homeAway": x.get("homeAway"),
                                                              "winner": x.get("winner")}
                                                             for x in c.get("competitors", [])]}
                                            for c in d.get("header", {}).get("competitions", [])]}}
        with gzip.open(f"{SUMS}/{gid}.json.gz", "wt") as f:
            json.dump(slim, f)
        return gid

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(one, g): g for g in todo}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                f.result(); ok += 1
            except Exception as e:
                fail += 1
                if fail <= 5:
                    print(f"  FAIL {futs[f]}: {e}")
            if i % 250 == 0:
                el = time.time() - t0
                print(f"  {i:,}/{len(todo):,}  ok={ok:,} fail={fail:,}  "
                      f"{el/i:.2f}s/game  eta {(len(todo)-i)*el/i/60:.1f}m", flush=True)
    print(f"done: ok={ok:,} fail={fail:,} in {(time.time()-t0)/60:.1f}m")


if __name__ == "__main__":
    a = sys.argv[1:]
    days = int(a[a.index("--refresh-days") + 1]) if "--refresh-days" in a else 0
    if a[0] == "games":
        stage_games(refresh="--refresh" in a)
    else:
        stage_summaries(refresh_days=days)
