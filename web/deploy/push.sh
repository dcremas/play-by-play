#!/usr/bin/env bash
# Ship the Dash instance explorer to the EC2 box and provision it. Run FROM THE MAC:
#
#     bash web/deploy/push.sh              # code only -- the usual case
#     bash web/deploy/push.sh --data       # code AND the 331 MB snapshot
#
# TWO CADENCES, ONE SCRIPT, AND THE FLAG IS THE WHOLE POINT.
#
# The code changes when the app changes. The snapshot changes when
# scripts/build_snapshot.py runs after a weekly warehouse update. Those are not
# the same clock, and shipping 331 MB of DuckDB to deploy a one-line CSS fix over
# a home uplink is the kind of friction that stops people deploying. So the data
# is opt-in: --data, or the first run, which detects the box has no snapshot yet
# and ships it regardless.
#
# This mirrors the split mcp_server/deploy/push.sh already makes between code and
# scripts/sync_ec2.py's data, and for the same reason.
#
# Safe to re-run. It never touches the warehouse, never rewrites a credential,
# and never restarts the Streamlit agent sharing this hostname.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"      # the repo root -- web/ is a package under it
SSH_HOST="${SSH_HOST:-awsvm}"          # see ~/.ssh/config
STAGE=/tmp/pbp-web-deploy
DATA_DIR=/var/lib/pbp-web

WANT_DATA=0
[[ "${1:-}" == "--data" ]] && WANT_DATA=1

log()  { printf '\n== %s\n' "$*"; }
fail() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }

[[ -f "$REPO/web/app.py" ]]   || fail "cannot find the app at $REPO/web"
[[ -f "$HERE/provision.sh" ]] || fail "cannot find provision.sh next to this script"

log "Checking the box is reachable"
ssh -o BatchMode=yes -o ConnectTimeout=15 "$SSH_HOST" true \
    || fail "cannot ssh to ${SSH_HOST}. Check ~/.ssh/config."
echo "   ${SSH_HOST} ok"

# The first deploy has no snapshot on the box, and the app cannot start without
# one -- web/data.py raises FileNotFoundError naming both files. Detecting that
# here turns a confusing crash-loop into a slightly longer first push.
#
# `sudo test`, not `test`. /var/lib/pbp-web is mode 750 root:pbpweb, so ec2-user
# cannot stat through it and a plain `test -s` returns false whether the snapshot
# is missing or merely unreadable. Without the sudo this reships 331 MB on EVERY
# deploy while reporting "no snapshot on the box yet", which is both slow and a
# lie.
if (( ! WANT_DATA )); then
    if ! ssh "$SSH_HOST" "sudo test -s ${DATA_DIR}/pbp_cfb.duckdb -a -s ${DATA_DIR}/pbp_nfl.duckdb" 2>/dev/null; then
        log "No snapshot on the box yet -- shipping it with this push"
        WANT_DATA=1
    fi
fi

log "Staging the code"
# Excludes, and why each one:
#   .venv/        built on the box against /usr/bin/python3.11, not copied
#   __pycache__/  stale .pyc files lie about paths after a move -- this exact
#                 trap is documented in the ec2-nginx CLAUDE.md
#   data/         the snapshot goes to /var/lib/pbp-web, not under the code
ssh "$SSH_HOST" "rm -rf ${STAGE} && mkdir -p ${STAGE}/web"
rsync -az --delete \
      --exclude '.venv/' --exclude '__pycache__/' --exclude 'data/' \
      "$REPO/web/" "${SSH_HOST}:${STAGE}/web/"
echo "   $(find "$REPO/web" -name '*.py' -not -path '*/.venv/*' | wc -l | tr -d ' ') python files staged at ${STAGE}"

if (( WANT_DATA )); then
    log "Shipping the snapshot (331 MB -- this is the slow part)"
    for lg in cfb nfl; do
        f="$REPO/data/out/pbp_${lg}.duckdb"
        [[ -s "$f" ]] || fail "missing $f -- run scripts/build_snapshot.py first"
    done
    # To /tmp first, then moved into place by provision.sh as a single rename per
    # file. /var/lib/pbp-web is ReadOnlyPaths for the running unit and owned by
    # root, so rsync cannot write there as ec2-user -- and a partial file landing
    # under the name the app opens would be worse than no file at all.
    # --progress, NOT --info=progress2. Recent macOS ships openrsync rather than
    # GNU rsync, and openrsync does not implement --info at all: the run dies
    # with "unrecognized option" after the code has already been staged, which
    # leaves a half-finished deploy. --progress is in both implementations.
    rsync -az --progress \
          "$REPO/data/out/pbp_cfb.duckdb" "$REPO/data/out/pbp_nfl.duckdb" \
          "${SSH_HOST}:${STAGE}/"
    echo "   both snapshots staged"
fi

log "Provisioning (sudo on the box)"
ssh -t "$SSH_HOST" "sudo bash ${STAGE}/web/deploy/provision.sh"

log "Cleaning up the staging copy"
# The staged tree is world-readable under /tmp and holds no secret -- this app
# has none -- but a full copy of the code plus 331 MB of DuckDB is not something
# to leave lying about on a 20 GB disk.
ssh "$SSH_HOST" "rm -rf ${STAGE}"
echo "   removed ${STAGE}"

log "Done"
echo "   https://pbp.dustincremascoli.com/plays/"
