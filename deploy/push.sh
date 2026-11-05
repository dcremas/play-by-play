#!/usr/bin/env bash
# Ship the play-by-play pipeline to the EC2 box and (re)provision it.
#
#     bash deploy/push.sh             # code only: every later deploy
#     bash deploy/push.sh --seed      # first time: also copy all of data/ (~1.6 GB)
#     bash deploy/push.sh --enable    # also turn on the 06:00 timer and the heartbeat
#
# Code is staged in /tmp and installed by deploy/provision.sh under sudo, the same shape as
# web/deploy/push.sh. Data goes straight to /opt/pbp-etl/data instead: /tmp on the box is a
# RAM-backed tmpfs, and 1.5 GB there would squeeze every site on it.
#
# --seed copies this checkout's data, which is right exactly once, at cutover. After that the box
# is the system of record and data flows the other way (scripts/pull_from_box.sh); a second
# --seed would overwrite newer ESPN fetches with older ones, so it refuses.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
SSH_HOST="${SSH_HOST:-awsvm}"
STAGE=/tmp/pbp-etl-deploy
APP=/opt/pbp-etl
SEED=0 ENABLE=""
for arg in "$@"; do
    case "$arg" in
        --seed)   SEED=1 ;;
        --enable) ENABLE=--enable ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

log()  { printf '\n== %s\n' "$*"; }
fail() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }

log "Checking the box is reachable"
ssh -o BatchMode=yes -o ConnectTimeout=15 "$SSH_HOST" true || fail "cannot ssh to $SSH_HOST"

if (( SEED )); then
    log "Seeding $APP/data from this checkout"
    if ssh "$SSH_HOST" "sudo test -e $APP/data/state/last_success"; then
        fail "the box has already run the job -- its data is newer than this checkout's"
    fi
    ssh "$SSH_HOST" "sudo mkdir -p $APP/data"
    # ALL of data/, not a list: each league keeps its raw ESPN files in its own directory
    # (data/espn, data/espn_nfl -- see scripts/league.py), and a seed that named only data/espn
    # built the NFL's athlete links from one season on 2026-11-05.
    rsync -az --progress --rsync-path="sudo rsync" \
          --exclude '/logs/' --exclude '/state/' --exclude '.athlete_combined/' \
          --exclude '.duckdb_spill/' \
          "$REPO/data/" "$SSH_HOST:$APP/data/"
fi

log "Staging the code"
ssh "$SSH_HOST" "rm -rf $STAGE && mkdir -p $STAGE"
rsync -az --delete \
      --exclude '.git/' --exclude '.venv/' --exclude 'data/' --exclude '__pycache__/' \
      --exclude 'dbt/target/' --exclude 'dbt/dbt_packages/' --exclude 'dbt/logs/' \
      --exclude '/web/' --exclude '/explorer/' --exclude '/reports/' \
      --exclude 'mcp_server/.venv/' --exclude 'mcp_server/.env' \
      "$REPO/" "$SSH_HOST:$STAGE/"

log "Provisioning (sudo on the box)"
ssh -t "$SSH_HOST" "sudo bash $STAGE/deploy/provision.sh $ENABLE"

ssh "$SSH_HOST" "rm -rf $STAGE"
log "Done"
