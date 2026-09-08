"""Fetch authoritative athlete identity from ESPN's core API.

The participants feed gives an athlete id and nothing else, so until now every name in
st.dim_athlete was voted out of play text -- and that only ever named 25.9% of the athletes
special teams knew about (8,794 of 33,891). The scrimmage fact makes that worse rather than
better: of 698 distinct passers in 2024, 107 had a name.

This endpoint has the name outright, plus position and jersey:

    /athletes/<id>  ->  displayName, fullName, shortName, jersey, position.abbreviation

A 119-id sample returned 119 names and 119 positions, at 9 ms each with 20 workers. Do NOT
read that as a licence to parallelise: 24 workers sailed through 20,000 athletes and then
403ed the whole IP, and the block outlasted the run -- ids fetched successfully minutes
earlier started 403ing too. The default is 6, and a 403 parks every thread rather than one.
Budget roughly half an hour for ~63,000 athletes.

Cached on disk exactly like summaries/ and participants/, so it is a one-time cost and the
in-season update only ever fetches players it has not seen. The store is rewritten every
5,000 athletes, so an interrupted run resumes instead of starting over.

Ids come from the two bridges rather than the database, keeping this runnable offline and
before any load:

    data/out/play_athlete*.csv        special teams
    data/out/scrimmage_athlete.csv    scrimmage

    python fetch_athletes.py                 fetch whatever is missing
    python fetch_athletes.py --refresh       re-fetch everything, e.g. after a transfer window
    python fetch_athletes.py --workers 4    slower still, if 6 gets throttled
"""
import os, sys, csv, gzip, json, time, random, argparse, threading
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

CORE = "https://sports.core.api.espn.com/v2/sports/football/leagues/college-football"
HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HOME, "data", "out")
STORE = os.path.join(HOME, "data", "espn", "athletes.json.gz")
# 24 workers got this endpoint to 403 the whole IP after ~20,000 athletes, and the block
# outlasted the run -- an id fetched successfully minutes earlier started 403ing too. 403 is
# ESPN's throttle signal here, not a missing record (fetch_espn.py notes the same endpoint
# family 403s browser-like User-Agent strings). 6 workers matches fetch_participants.py,
# which has swept this API for 10,470 games without tripping it.
WORKERS = 6
FLUSH_EVERY = 5000          # a 20-minute run must not lose everything to one interruption

# When a 403 lands, every thread waits -- backing off one request while 5 others keep
# hammering just extends the block.
_COOLDOWN_UNTIL = 0.0
_COOLDOWN_LOCK = threading.Lock()
THROTTLE_BACKOFF = (30, 90, 180, 300)


def _cooldown(seconds):
    global _COOLDOWN_UNTIL
    with _COOLDOWN_LOCK:
        _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, time.time() + seconds)


def _wait_out_cooldown():
    while True:
        with _COOLDOWN_LOCK:
            left = _COOLDOWN_UNTIL - time.time()
        if left <= 0:
            return
        time.sleep(min(left, 5))

# One dict rather than 63,000 small files: the payload is ~150 bytes an athlete, so the whole
# store is a few MB gzipped and loads in one read. participants/ is per-game because games
# arrive one at a time; athletes do not.
SOURCES = ["play_athlete.csv", "play_athlete_2026.csv", "scrimmage_athlete.csv"]

# Everything else the endpoint returns -- headshots, links, birth place, draft, statistics
# refs -- is noise for a dimension table and would multiply the store by twenty.
KEEP = ("displayName", "fullName", "shortName", "jersey", "weight", "height")


def load_store():
    if not os.path.exists(STORE):
        return {}
    with gzip.open(STORE, "rt") as f:
        return json.load(f)


def save_store(store):
    tmp = STORE + ".tmp"
    with gzip.open(tmp, "wt") as f:
        json.dump(store, f)
    os.replace(tmp, tmp.removesuffix(".tmp"))   # atomic; a killed run never leaves a partial


def wanted_ids(sources):
    ids = set()
    for name in sources:
        path = name if os.path.isabs(name) else os.path.join(OUT, name)
        if not os.path.exists(path):
            print(f"  {os.path.basename(path)}: absent, skipped", flush=True)
            continue
        n = 0
        with open(path) as f:
            for r in csv.DictReader(f):
                a = (r.get("athlete_id") or "").strip()
                if a:
                    ids.add(a); n += 1
        print(f"  {os.path.basename(path)}: {n:,} bridge rows", flush=True)
    return ids


def get(url, tries=5):
    for i in range(tries):
        _wait_out_cooldown()
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                # Throttled. Park every worker, not just this one, and wait it out.
                _cooldown(THROTTLE_BACKOFF[min(i, len(THROTTLE_BACKOFF) - 1)])
                if i == tries - 1:
                    raise
                continue
            if i == tries - 1:
                raise
            time.sleep((2 ** i) + random.random())
        except Exception:
            if i == tries - 1:
                raise
            time.sleep((2 ** i) + random.random())


def one(aid):
    d = get(f"{CORE}/athletes/{aid}")
    rec = {k: d.get(k) for k in KEEP if d.get(k) is not None}
    pos = d.get("position") or {}
    if pos.get("abbreviation"):
        rec["position"] = pos["abbreviation"]
    if pos.get("displayName"):
        rec["position_name"] = pos["displayName"]
    return aid, rec


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--refresh", action="store_true", help="re-fetch ids already stored")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--sources", nargs="*", default=SOURCES)
    a = ap.parse_args()

    store = load_store()
    print(f"store holds {len(store):,} athletes", flush=True)
    ids = wanted_ids(a.sources)
    todo = sorted(ids if a.refresh else (ids - store.keys()), key=int)
    print(f"{len(ids):,} distinct athletes across the bridges, {len(todo):,} to fetch "
          f"({a.workers} workers)", flush=True)
    if not todo:
        return

    ok = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(one, i): i for i in todo}
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                aid, rec = fut.result()
                store[aid] = rec
                ok += 1
            except Exception as e:
                fail += 1
                if fail <= 5:
                    print(f"  FAIL {futs[fut]}: {type(e).__name__} {e}", flush=True)
            if n % FLUSH_EVERY == 0:
                save_store(store)
                el = time.time() - t0
                print(f"  {n:,}/{len(todo):,} ok={ok:,} fail={fail:,} "
                      f"eta {(len(todo)-n)*el/n/60:.1f}m", flush=True)
    save_store(store)

    named = sum(1 for r in store.values() if r.get("displayName"))
    posed = sum(1 for r in store.values() if r.get("position"))
    print(f"\ndone in {(time.time()-t0)/60:.1f}m: ok={ok:,} fail={fail:,}")
    print(f"{STORE}  ({os.path.getsize(STORE)/1e6:.1f} MB)")
    print(f"  {len(store):,} athletes, {named:,} named ({100*named/len(store):.1f}%), "
          f"{posed:,} with a position ({100*posed/len(store):.1f}%)")


if __name__ == "__main__":
    main()
