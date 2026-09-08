"""Fetch per-play athlete participants from ESPN's core API, 2014 onward.

The site-API summaries we already have carry play text but no athlete identity. The core API
does: each play has participants[] with a role ('kicker', 'returner', 'blocker', 'tackler')
and an athlete $ref whose trailing path segment is the ESPN athlete id. That id is the fix
for name-string matching, which is unreliable across twelve seasons and 130+ teams.

Athlete ids begin in 2014: 2013 and earlier return participants[] with no athlete $ref at
all (0% of plays), 2014 onward ~91%. 2014 is therefore the floor for this feed.

Join key: the core-API play id is the game id concatenated with sequenceNumber, which is
exactly how the fact table's play_uid is built (espn:<game_id>:<sequenceNumber>).

    python fetch_participants.py     -> data/espn/participants/<game_id>.json.gz

For a season still being played the default "fetch what has no file yet" rule never
revisits a game, so a late box-score correction is missed. Two flags cover that:

    --seasons 2026        restrict the sweep to those seasons instead of SEASONS below
    --refresh-days 21     also re-pull games that kicked off inside that window

scripts/update_season.py passes both.
"""
import os, sys, json, gzip, re, time, random, urllib.request
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

CORE = "https://sports.core.api.espn.com/v2/sports/football/leagues/college-football"
HOME = os.path.expanduser("~/projects/cfb-pbp")
ESPN = f"{HOME}/data/espn"
OUT = f"{ESPN}/participants"
SEASONS = [2014, 2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]
WORKERS = 6
PAGE = 400          # core-API page size; one() follows pages beyond the first
ATH = re.compile(r"/athletes/(\d+)")


def recent(iso, days):
    """True if an ESPN kickoff timestamp falls inside the last `days` days."""
    if not iso or days <= 0:
        return False
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    return t >= datetime.now(timezone.utc) - timedelta(days=days)


def get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=45) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep((2 ** i) + random.random())


def one(gid):
    """Fetch one game's per-play participants, keyed by SEQUENCE NUMBER.

    Originally keyed by the core-API play `id`, on the assumption that it is always the
    game id concatenated with the sequence number. For most games it is -- but from week 9
    of 2025 the two endpoints diverge: the core API hands out its own `id` (401752753103)
    while the site-API summary that builds play_uid uses a different sequenceNumber (105).
    That silently cost 7,101 of 2025's kicks their athlete identity, 34% of the season.

    `sequenceNumber` is present on every core-API play and is the same value the summary
    reports, so it is the join key. build_dims reads either shape: a key that starts with
    the game id is the old id-based form and gets the prefix stripped; anything else is a
    bare sequence number.
    """
    out = {}
    page = 1
    while True:
        d = get(f"{CORE}/events/{gid}/competitions/{gid}/plays"
                f"?limit={PAGE}&page={page}")
        items = d.get("items") or []
        for p in items:
            parts = p.get("participants") or []
            if not parts:
                continue
            rows = []
            for x in parts:
                ref = ((x.get("athlete") or {}).get("$ref")) or ""
                m = ATH.search(ref)
                if m:
                    rows.append([x.get("type"), m.group(1)])
            if not rows:
                continue
            # Fall back to `id` only if the feed omits sequenceNumber, so a key is never lost.
            seq = p.get("sequenceNumber")
            key = str(seq) if seq is not None else str(p.get("id"))
            out[key] = rows
        # The endpoint reports its own paging; follow it rather than trusting one page.
        if len(items) < PAGE or page * PAGE >= int(d.get("count") or 0):
            break
        page += 1
    with gzip.open(f"{OUT}/{gid}.json.gz", "wt") as f:
        json.dump(out, f)
    return gid


def main():
    """Fetch every game with no participants file yet.

    With --games <file.json> instead re-fetch exactly the game ids in that file, even if
    they already have one. That is the path for repairing games whose stored file uses the
    old id-based key and cannot be joined; there is no need to re-pull all 10,379.
    """
    os.makedirs(OUT, exist_ok=True)
    a = sys.argv[1:]
    if "--games" in a:
        todo = [int(g) for g in json.load(open(a[a.index("--games") + 1]))]
        print(f"re-fetching {len(todo):,} named games", flush=True)
    else:
        seasons = ([int(x) for x in a[a.index("--seasons") + 1].split(",")]
                   if "--seasons" in a else SEASONS)
        days = int(a[a.index("--refresh-days") + 1]) if "--refresh-days" in a else 0
        todo, restale = [], 0
        for s in seasons:
            gp = f"{ESPN}/games_{s}.json"
            if not os.path.exists(gp):
                continue
            for gid, g in json.load(open(gp)).items():
                if not os.path.exists(f"{OUT}/{gid}.json.gz"):
                    todo.append(gid)
                elif recent(g.get("date"), days):
                    todo.append(gid); restale += 1
        stale = f", {restale:,} of them re-pulls inside {days}d" if restale else ""
        print(f"{len(todo):,} games to fetch ({WORKERS} workers){stale}", flush=True)
    if not todo:
        return
    ok = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(one, g): g for g in todo}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                f.result(); ok += 1
            except Exception as e:
                fail += 1
                if fail <= 5:
                    print(f"  FAIL {futs[f]}: {e}", flush=True)
            if i % 500 == 0:
                el = time.time() - t0
                print(f"  {i:,}/{len(todo):,} ok={ok:,} fail={fail:,} "
                      f"{el/i:.2f}s/game eta {(len(todo)-i)*el/i/60:.1f}m", flush=True)
    print(f"done: ok={ok:,} fail={fail:,} in {(time.time()-t0)/60:.1f}m")


if __name__ == "__main__":
    main()
