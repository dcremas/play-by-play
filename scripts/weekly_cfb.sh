#!/usr/bin/env bash
# The Sunday-morning college run: pull the finished week in, verify it, mirror it, ship it.
#
#     bash scripts/weekly_cfb.sh            # season defaults to the current year
#     bash scripts/weekly_cfb.sh 2026
#
# Runbook A (college only) then runbook H, with the checks that each one needs and neither
# runs on its own. Stops at the first failure; every stage is idempotent, so the fix for an
# interrupted run is to run it again. The full log lands in data/logs/.
#
# The NFL is deliberately not pulled forward -- its week runs Thu->Mon -- but the sync still
# covers BOTH leagues. It copies local Postgres, so the NFL simply stays at the last week
# loaded, and a one-league sync is what left the mirror a week behind on 2026-10-05.
set -euo pipefail

SEASON="${1:-$(date +%Y)}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
LOG="$REPO/data/logs/weekly_cfb_$(date +%Y%m%d_%H%M%S).log"

cd "$REPO"                             # sql/load_dims.sql resolves CSV paths from here
mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1

step() { printf '\n\033[1m==== [%s] %s\033[0m\n' "$(date +%H:%M:%S)" "$*"; }
trap 'printf "\n\033[31mFAILED at: %s\033[0m  (log: %s)\n" "$BASH_COMMAND" "$LOG"' ERR

step "Preflight"
psql -d pbp -Atc 'select 1' >/dev/null           || { echo "local Postgres is not up"; exit 1; }
ssh -o BatchMode=yes -o ConnectTimeout=15 awsvm true || { echo "cannot ssh to awsvm"; exit 1; }

step "1/6  ESPN -> local Postgres -> snapshot (college $SEASON)"
"$PY" scripts/update_season.py "$SEASON"

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
