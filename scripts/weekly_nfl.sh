#!/usr/bin/env bash
# The NFL run: pull the finished week in, verify it, mirror it, ship it.
#
#     bash scripts/weekly_nfl.sh            # season defaults to the current year
#     bash scripts/weekly_nfl.sh 2026
#
# scripts/weekly_cfb.sh with --league nfl on the first step; read that one for the why. The
# NFL week runs Thu->Mon, so the natural time is Tuesday morning. Run before the Monday game
# ends and the week loads without it: only completed games are fetched, and the next run
# picks the rest up (anything inside --refresh-days is re-pulled). The preflight lists any
# game in ESPN's current week that is not final, so a partial week is never a surprise.
#
# The sync still covers BOTH leagues, for the same reason the college script's does.
set -euo pipefail

SEASON="${1:-$(date +%Y)}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
LOG="$REPO/data/logs/weekly_nfl_$(date +%Y%m%d_%H%M%S).log"

cd "$REPO"                             # sql/load_dims.sql resolves CSV paths from here
mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1

step() { printf '\n\033[1m==== [%s] %s\033[0m\n' "$(date +%H:%M:%S)" "$*"; }
trap 'printf "\n\033[31mFAILED at: %s\033[0m  (log: %s)\n" "$BASH_COMMAND" "$LOG"' ERR

step "Preflight"
psql -d pbp -Atc 'select 1' >/dev/null           || { echo "local Postgres is not up"; exit 1; }
ssh -o BatchMode=yes -o ConnectTimeout=15 awsvm true || { echo "cannot ssh to awsvm"; exit 1; }
"$PY" - <<'PY' || echo "(could not read ESPN's scoreboard; carrying on)"
import json, urllib.request
url = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
d = json.load(urllib.request.urlopen(url, timeout=20))
ev = d["events"]
left = [e for e in ev if not e["status"]["type"]["completed"]]
print(f"ESPN week {d['week']['number']}: {len(ev) - len(left)} of {len(ev)} games final")
for e in left:
    print(f"  NOT FINAL, will be missed this run: {e['shortName']}  {e['date']}  "
          f"{e['status']['type']['description']}")
PY

step "1/6  ESPN -> local Postgres -> snapshot (NFL $SEASON)"
"$PY" scripts/update_season.py "$SEASON" --league nfl

step "2/6  Verify the cfb/nfl split, local"
"$PY" scripts/verify_split.py --local

step "3/6  Sync the EC2 mirror (both leagues, $SEASON)"
"$PY" scripts/sync_ec2.py --season "$SEASON"

step "4/6  Verify the mirror row by row"
"$PY" scripts/verify_mirror.py

step "5/6  Verify the cfb/nfl split, mirror"
"$PY" scripts/verify_split.py --mirror

step "6/6  Ship the snapshot to the Play Explorer"
bash web/deploy/push.sh --data

step "Done in ${SECONDS}s  (log: $LOG)"
