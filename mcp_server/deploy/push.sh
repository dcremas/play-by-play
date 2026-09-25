#!/usr/bin/env bash
# Ship this server's code to the EC2 box and provision it. Run FROM THE MAC:
#
#     bash mcp_server/deploy/push.sh
#     bash mcp_server/deploy/push.sh --tls     # also run enable-tls.sh
#
# THIS IS THE CODE SYNC STORY scripts/sync_ec2.py does not have. That script
# ships DATA and SQL -- the CSV extracts, the loaders, the grant and comment
# files -- and deliberately knows nothing about the server. The two move on
# different clocks: the warehouse is refreshed weekly, the server changes when
# its code changes. Coupling them would mean an hour-long data sync to ship a
# one-line fix.
#
# Safe to re-run. It never touches the database and never rewrites a credential.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/.." && pwd)"          # mcp_server/
SSH_HOST="${SSH_HOST:-awsvm}"          # see ~/.ssh/config
STAGE=/tmp/pbp-deploy

WANT_TLS=0
[[ "${1:-}" == "--tls" ]] && WANT_TLS=1

log()  { printf '\n== %s\n' "$*"; }
fail() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }

[[ -f "$SRC/pbp_mcp/server.py" ]] || fail "cannot find the server at $SRC"

log "Checking the box is reachable"
ssh -o BatchMode=yes -o ConnectTimeout=15 "$SSH_HOST" true \
    || fail "cannot ssh to ${SSH_HOST}. Check ~/.ssh/config."
echo "   ${SSH_HOST} ok"

log "Staging the code"
# .env is EXCLUDED. The laptop's copy holds the real password at mode 600; the
# box gets its credential from /etc/pbp-mcp/mcp.env instead, so there is exactly
# one place to rotate it on each machine. provision.sh asserts the file did not
# arrive.
ssh "$SSH_HOST" "rm -rf ${STAGE} && mkdir -p ${STAGE}"
rsync -az --delete \
      --exclude '.venv/' --exclude '__pycache__/' --exclude '.env' \
      "$SRC/" "${SSH_HOST}:${STAGE}/"
# The explorer lives beside mcp_server/ in the repo. Staged INSIDE the same directory so
# the cleanup at the end removes it too -- a second staging root under /tmp would survive
# every run and quietly accumulate.
if [[ -d "$SRC/../explorer" ]]; then
    rsync -az --delete \
          --exclude '.venv/' --exclude '__pycache__/' --exclude '.env' \
          --exclude '.budget.json' \
          "$SRC/../explorer/" "${SSH_HOST}:${STAGE}/explorer/"
    echo "   explorer staged at ${STAGE}/explorer"
fi
echo "   $(find "$SRC" -name '*.py' -not -path '*/.venv/*' | wc -l | tr -d ' ') python files staged at ${STAGE}"

log "Provisioning (sudo on the box)"
ssh -t "$SSH_HOST" "sudo bash ${STAGE}/deploy/provision.sh"

if (( WANT_TLS )); then
    log "Enabling TLS"
    ssh -t "$SSH_HOST" "sudo bash ${STAGE}/deploy/enable-tls.sh"
fi

log "Cleaning up the staging copy"
# The staged tree is world-readable under /tmp. It holds no secret -- .env was
# excluded -- but leaving a full copy of the server lying about serves nothing.
ssh "$SSH_HOST" "rm -rf ${STAGE}"
echo "   removed ${STAGE}"
