#!/usr/bin/env bash
# The EC2 box's morning job: load every game that has finished since the last load, in either
# league, rebuild the serving schemas with dbt, and refresh the Play Explorer's snapshot.
#
#     sudo systemctl start pbp-daily      # what the timer runs, as the pbpetl user
#     sudo journalctl -u pbp-daily -e     # its output
#
# The box's Postgres is the system of record (since 2026-11). This is refresh.sh run where
# the data lives: no mirror to sync and no snapshot to ship over ssh, because the data, the
# database and both apps are all on this box.
#
# What it does NOT do, and who does instead:
#   * install the snapshot where pbp-web reads it and restart the app -- that needs root, so
#     it is pbp-daily.service's ExecStartPost (deploy/install-snapshot.sh), which runs only
#     after this exits 0;
#   * email on failure -- pbp-daily.service's OnFailure= does, through the box's existing
#     pg-backup-alert@ template;
#   * notice that this stopped running at all -- deploy/heartbeat.sh publishes the age of
#     the last success hourly, and the pbp-daily-stale alarm watches it.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
SEASON="$(date +%Y)"
STATE="$REPO/data/state"
cd "$REPO"                             # the SQL files resolve CSV paths from here
mkdir -p "$STATE"

step() { printf '\n==== [%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

# The heartbeat reads this. Written ONLY at the end of a run that got there, including a day
# with nothing to load: the alarm means "the job stopped working", not "nobody played".
succeeded() { date -u +%s > "$STATE/last_success"; }

step "What finished since the last load"
# "<league> <game_id>" per finished game not yet loaded. Kept for the re-check at the end.
NEW="$("$PY" scripts/new_games.py --ids)"
LEAGUES="$(cut -d' ' -f1 <<<"$NEW" | sort -u | xargs)"
# One-shot override for a stat correction or a test run: `touch data/state/force` and the
# next run reloads both leagues' current season whether or not anything new has finished.
if [[ -e "$STATE/force" ]]; then
    rm -f "$STATE/force"
    LEAGUES="cfb nfl"
    echo "forced by $STATE/force: reloading both leagues"
fi
if [[ -z "$LEAGUES" ]]; then
    echo "nothing new in either league"
    succeeded
    exit 0
fi
"$PY" scripts/new_games.py || true

step "ESPN -> Postgres -> dbt -> snapshot ($LEAGUES $SEASON)"
# $LEAGUES unquoted on purpose: one argument per league.
"$PY" scripts/update_season.py "$SEASON" --league $LEAGUES --transform dbt

step "Verify the cfb/nfl split against pbp.*"
"$PY" scripts/verify_split.py --local

# A game final when this run started and still missing after a clean load is one the pipeline
# cannot take; it would re-trigger every morning until someone looks. Fail so the email goes.
step "Re-check"
LEFT="$(comm -12 <(sort <<<"$NEW") <("$PY" scripts/new_games.py --ids | sort) | xargs)"
if [[ -n "$LEFT" ]]; then
    echo "final before this run and still not loaded: $LEFT"
    exit 1
fi

succeeded
step "Done in ${SECONDS}s"
