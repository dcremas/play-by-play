#!/usr/bin/env bash
# Load every game that has finished since the last load, in either league, and ship it.
#
#     bash scripts/refresh.sh                 # whatever is new; does nothing if nothing is
#     bash scripts/refresh.sh --check         # list what is new and what is still playing
#     bash scripts/refresh.sh --force nfl     # run a league even with nothing new (a late
#                                             # stat correction); --force alone means both
#
# The replacement for choosing a day. College plays Tuesday through Saturday from October on
# and the NFL Thursday, Sunday and Monday, so "run it Sunday" left every midweek game out of
# both apps for most of a week. This asks ESPN first (scripts/new_games.py, ~4 seconds) and
# runs the pipeline only for a league with a finished game local Postgres lacks -- so it is
# safe to run any day, as often as you like, and it is what the daily launchd job runs.
#
# The pipeline is weekly_cfb.sh / weekly_nfl.sh's: update_season.py, then ONE pass of both
# verifiers, the both-league mirror sync and the explorer snapshot. When both leagues have
# new games they share all of the both-league work -- update_season.py --league cfb nfl
# builds the athletes, splits the schemas and writes the snapshot once, not once per league.
#
# --notify posts a macOS notification at the end (the launchd job passes it).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
SEASON="$(date +%Y)"
STATE="$REPO/data/state"
cd "$REPO"                             # sql/load_dims.sql resolves CSV paths from here

MODE=run NOTIFY=0 FORCE=()
while (($#)); do
    case "$1" in
        --check)  MODE=check ;;
        --notify) NOTIFY=1 ;;
        --force)  MODE=force
                  while [[ "${2:-}" == cfb || "${2:-}" == nfl ]]; do FORCE+=("$2"); shift; done ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

notify() {
    ((NOTIFY)) || return 0
    osascript -e "display notification \"$2\" with title \"pbp refresh\" subtitle \"$1\"" \
        >/dev/null 2>&1 || true
}

if [[ $MODE == check ]]; then
    exec "$PY" scripts/new_games.py
fi

# One run at a time. Two overlapping runs would interleave DELETE+INSERTs on the same season
# and race each other's CSVs in data/out. mkdir is atomic; the pid makes a lock left by a
# killed run recognisable as stale rather than blocking every run after it.
mkdir -p "$STATE"
LOCK="$STATE/refresh.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
    if kill -0 "$(cat "$LOCK/pid" 2>/dev/null)" 2>/dev/null; then
        echo "$(date '+%F %T') another refresh is running (pid $(cat "$LOCK/pid")); skipping"
        exit 0
    fi
    echo "$(date '+%F %T') clearing a stale lock from pid $(cat "$LOCK/pid" 2>/dev/null)"
    rm -rf "$LOCK"; mkdir "$LOCK"
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

psql -d pbp -Atc 'select 1' >/dev/null || { notify FAILED "local Postgres is not up";
                                           echo "local Postgres is not up"; exit 1; }

# "<league> <game_id>" per finished game not yet loaded. Kept, so the re-check at the end can
# tell a game the load failed to take from one that merely finished while it ran.
NEW="$("$PY" scripts/new_games.py --ids)"
if [[ $MODE == force ]]; then
    LEAGUES="${FORCE[*]:-cfb nfl}"
else
    LEAGUES="$(cut -d' ' -f1 <<<"$NEW" | sort -u | xargs)"
    if [[ -z "$LEAGUES" ]]; then
        echo "$(date '+%F %T') nothing new in either league"
        exit 0
    fi
fi

LOG="$REPO/data/logs/refresh_$(date +%Y%m%d_%H%M%S).log"
mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1

step() { printf '\n\033[1m==== [%s] %s\033[0m\n' "$(date +%H:%M:%S)" "$*"; }
trap 'printf "\n\033[31mFAILED at: %s\033[0m  (log: %s)\n" "$BASH_COMMAND" "$LOG";
      notify FAILED "stopped at: ${BASH_COMMAND:0:80}"' ERR

step "Preflight: loading $LEAGUES ($SEASON)"
ssh -o BatchMode=yes -o ConnectTimeout=15 awsvm true || { echo "cannot ssh to awsvm"; exit 1; }
"$PY" scripts/new_games.py || echo "(could not read ESPN's scoreboard; carrying on)"

step "ESPN -> local Postgres -> snapshot ($LEAGUES $SEASON)"
# $LEAGUES unquoted on purpose: one argument per league.
"$PY" scripts/update_season.py "$SEASON" --league $LEAGUES

step "Verify the cfb/nfl split, local"
"$PY" scripts/verify_split.py --local

step "Sync the EC2 mirror (both leagues, $SEASON)"
"$PY" scripts/sync_ec2.py --season "$SEASON"

step "Verify the mirror row by row"
"$PY" scripts/verify_mirror.py

step "Verify the cfb/nfl split, mirror"
"$PY" scripts/verify_split.py --mirror

step "Ship the snapshot to the Play Explorer"
bash web/deploy/push.sh --data

# A game that was final when this run started and is STILL missing after a clean load is one
# the pipeline cannot take, and it would re-trigger every run until someone looks. Say so
# loudly. (One that finished while the run was going is just the next run's.)
step "Re-check"
"$PY" scripts/new_games.py
LEFT="$(comm -12 <(sort <<<"$NEW") <("$PY" scripts/new_games.py --ids | sort) | xargs)"
if [[ -n "$LEFT" ]]; then
    echo "WARNING: final before this run and still not loaded: $LEFT"
    notify "Loaded with a gap" "still missing after the load: $LEFT"
else
    notify "Loaded $LEAGUES" "both apps are current (${SECONDS}s)"
fi

step "Done in ${SECONDS}s  (log: $LOG)"
