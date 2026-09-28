#!/usr/bin/env bash
# Install / update the Dash instance explorer on the EC2 box. Runs UNDER sudo,
# on the box, from the staging copy push.sh made:
#
#     sudo bash /tmp/pbp-web-deploy/web/deploy/provision.sh
#
# Do not run it by hand from /opt -- it copies FROM the staging tree INTO /opt,
# and pointing it at its own destination is a no-op at best.
#
# Idempotent. Safe to re-run on every deploy. It never touches the warehouse, the
# Streamlit agent, or anything else on this hostname: the only nginx file it
# owns are its own two, plus ONE include line spliced into pbp.conf.
set -euo pipefail

STAGE="$(cd "$(dirname "$0")/../.." && pwd)"   # /tmp/pbp-web-deploy
SRC="${STAGE}/web"
APP=/opt/pbp-web
DATA_DIR=/var/lib/pbp-web
USER_NAME=pbpweb
PORT=8061
PREFIX=/plays
PYTHON="${PYTHON:-/usr/bin/python3.11}"
VHOST=/etc/nginx/conf.d/pbp.conf
ARCHIVE=/home/ec2-user/nginx-conf-archive

log()  { printf '\n== %s\n' "$*"; }
fail() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }

# --- 0. Pre-flight ------------------------------------------------------------
log "Pre-flight"

[[ $EUID -eq 0 ]] || fail "must run under sudo"
[[ -f "$SRC/app.py" ]] || fail "no staged app at $SRC -- run push.sh, do not run this directly"
[[ -f "$VHOST" ]] || fail "$VHOST is missing -- the Streamlit half must be deployed first"

# THE INTERPRETER TRAP. Three python3s exist on this box and two are wrong:
# ec2-user's is a pyenv shim under /home (unreachable: ProtectHome=true), and
# root's is 3.9 (too old). /usr/bin/python3.11 is the one every other service
# here runs on. provision.sh runs under sudo, so it gets root's 3.9 unless told
# otherwise -- PYTHON= overrides. Documented in mcp_server/deploy/README-deploy.md.
[[ -x "$PYTHON" ]] || fail "$PYTHON not found -- set PYTHON= to a 3.11+ interpreter"
"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    || fail "$PYTHON is too old; need 3.11+"
echo "   interpreter $($PYTHON -V)"

# The port must be free, or free because WE hold it. Anything else means a
# collision with another service and the bind would fail after the unit is
# already installed.
if ss -ltn | grep -qE "127\.0\.0\.1:${PORT}\b"; then
    systemctl is-active --quiet pbp-web \
        || fail "127.0.0.1:${PORT} is in use by something that is not pbp-web"
    echo "   ${PORT} held by the running pbp-web (will be restarted)"
else
    echo "   ${PORT} free"
fi

# --- 1. Service user ----------------------------------------------------------
log "Service user"
if id -u "$USER_NAME" >/dev/null 2>&1; then
    echo "   ${USER_NAME} exists"
else
    # --no-create-home is why pbp-web.service sets HOME explicitly. See the unit.
    useradd --system --no-create-home --shell /sbin/nologin "$USER_NAME"
    echo "   ${USER_NAME} created"
fi

# --- 2. Code ------------------------------------------------------------------
log "Code"
mkdir -p "$APP/web"
# --delete so a file removed from the repo is removed here. Scoped to $APP/web,
# so .venv/ beside it survives -- rebuilding a virtualenv on every deploy would
# turn a 5-second push into a 3-minute one.
rsync -a --delete --exclude '__pycache__/' "$SRC/" "$APP/web/"

# $APP is the import ROOT, not a package. gunicorn loads `web.app:server` with
# /opt/pbp-web on sys.path (--pythonpath in the unit), so `web/` must be the
# package and /opt/pbp-web must NOT have an __init__.py of its own -- one here
# would make `web` a subpackage of a nameless parent and the import would fail.
rm -f "$APP/__init__.py"

# STALE .pyc FILES LIE ABOUT PATHS after a directory move. The ec2-nginx CLAUDE.md
# documents this costing a debugging session: a __pycache__ entry created under a
# different absolute path keeps that path in co_filename, and tracebacks then point
# at a directory that does not exist. rsync excludes them on the way in; clear any
# that a previous run left.
find "$APP" -type d -name __pycache__ -not -path "$APP/.venv/*" -exec rm -rf {} + 2>/dev/null || true

chown -R "$USER_NAME:$USER_NAME" "$APP"
echo "   code at $APP/web"

# --- 3. Virtualenv ------------------------------------------------------------
log "Virtualenv"
if [[ ! -x "$APP/.venv/bin/python" ]]; then
    "$PYTHON" -m venv "$APP/.venv"
    echo "   created"
fi
"$APP/.venv/bin/pip" install --quiet --upgrade pip
# --only-binary=:all: is a guard, not an optimisation. This box has 2 vCPU and no
# compiler toolchain worth the name; a source build of pandas or duckdb would run
# for an hour and then fail. If this line ever errors with "no matching
# distribution", a pin has outrun the wheels available for 3.11 -- fix the pin,
# do not drop the flag.
"$APP/.venv/bin/pip" install --quiet --only-binary=:all: \
    -r "$APP/web/deploy/requirements-deploy.txt" \
    || fail "dependency install failed -- see web/deploy/README-deploy.md §Pins"
chown -R "$USER_NAME:$USER_NAME" "$APP/.venv"
echo "   $("$APP/.venv/bin/python" -c 'import dash, duckdb, pandas; print(f"dash {dash.__version__}  duckdb {duckdb.__version__}  pandas {pandas.__version__}")')"

# Gunicorn's sd_notify support is what makes Type=notify in the unit correct.
# Assert it rather than trusting it: if this ever stops being true the unit hangs
# until systemd's start timeout and then gets killed, which looks like the app
# failing to boot rather than like a unit-file problem.
"$APP/.venv/bin/python" -c 'import gunicorn.systemd' 2>/dev/null \
    || fail "this gunicorn has no sd_notify support -- change Type=notify to Type=simple in pbp-web.service"
echo "   gunicorn sd_notify ok"

# --- 4. Snapshot --------------------------------------------------------------
log "Snapshot"
mkdir -p "$DATA_DIR"
shipped=0
for lg in cfb nfl; do
    staged="${STAGE}/pbp_${lg}.duckdb"
    if [[ -s "$staged" ]]; then
        # One rename per file, on the same filesystem, so the app never sees a
        # partial database under the name it opens. mv is atomic here; rsync
        # straight into place is not.
        mv -f "$staged" "${DATA_DIR}/pbp_${lg}.duckdb"
        shipped=1
    fi
    [[ -s "${DATA_DIR}/pbp_${lg}.duckdb" ]] \
        || fail "no ${lg} snapshot at ${DATA_DIR} -- re-run push.sh --data"
done
# Readable by the service user, writable by nobody. ProtectSystem=strict plus
# ReadOnlyPaths in the unit is the belt; this is the braces.
chown -R root:"$USER_NAME" "$DATA_DIR"
chmod 750 "$DATA_DIR"
chmod 640 "$DATA_DIR"/pbp_*.duckdb
(( shipped )) && echo "   snapshot replaced" || echo "   snapshot unchanged"
du -sh "$DATA_DIR" | sed 's/^/   /'

# --- 5. systemd ---------------------------------------------------------------
log "systemd unit"
install -m 644 "$APP/web/deploy/pbp-web.service" /etc/systemd/system/pbp-web.service
systemctl daemon-reload
systemctl enable --quiet pbp-web
echo "   pbp-web.service installed and enabled"

# --- 6. nginx -----------------------------------------------------------------
log "nginx"

# FINGERPRINT BEFORE. NGINX-RUNBOOK.md §9 requires the resolved config to be
# compared before and after any change, and an archived copy kept OUTSIDE
# conf.d/ -- stale .bak files inside conf.d are the trap that runbook opens with.
mkdir -p "$ARCHIVE"
stamp="$(date +%Y%m%d-%H%M%S)"

# `|| true` INSIDE the command substitution, on both of these, and it is not
# belt-and-braces -- it is what lets this script recover from its own last run.
#
# `nginx -T` exits non-zero when the config on disk is invalid. Under
# `set -o pipefail` that failure propagates out of the pipeline, the assignment
# fails, and `set -e` kills the script HERE -- before the install step that would
# have replaced the bad file. So a run that left an invalid config could never be
# fixed by re-running, which is the first thing anyone would try. It died with
# "== nginx" as the last line and no error, because the failing command was a
# fingerprint nobody thinks of as load-bearing.
#
# An invalid starting config simply produces a different (or empty) hash, which
# is exactly right: the before/after comparison is meant to show that something
# changed, and in that situation something certainly did.
nginx -T > "${ARCHIVE}/resolved-${stamp}-before.conf" 2>/dev/null || true
before="$( { nginx -T 2>/dev/null || true; } | sha256sum | cut -d' ' -f1)"
cp -a "$VHOST" "${ARCHIVE}/pbp.conf-${stamp}"
echo "   archived pbp.conf and the resolved config to ${ARCHIVE}"

install -m 644 "$APP/web/deploy/pbp-web.conf"            /etc/nginx/conf.d/pbp-web.conf
install -m 644 "$APP/web/deploy/pbp-plays.inc"           /etc/nginx/pbp-plays.inc
install -m 644 "$APP/web/deploy/proxy_params_pbp_web.inc" /etc/nginx/proxy_params_pbp_web.inc

# Splice ONE line into the existing 443 server block. Idempotent: if the include
# is already there, nothing is edited.
#
# The anchor is the maintenance error_page, which is the last thing in that
# block. Inserting BEFORE it keeps the include inside the server and ahead of
# nothing that matters -- nginx location matching is by specificity, not by file
# order, so where in the block it sits does not change routing.
if grep -q 'include /etc/nginx/pbp-plays.inc;' "$VHOST"; then
    echo "   include already present in pbp.conf"
else
    grep -q 'error_page 502 503 504 /maintenance.html;' "$VHOST" \
        || fail "cannot find the anchor line in $VHOST -- splice the include by hand"
    # Only the LAST occurrence is inside the 443 block; the :80 block has no
    # error_page. Guard anyway by requiring exactly one match.
    (( $(grep -c 'error_page 502 503 504 /maintenance.html;' "$VHOST") == 1 )) \
        || fail "anchor line appears more than once in $VHOST -- splice by hand"
    sed -i 's|^\( *\)error_page 502 503 504 /maintenance.html;|\1# The Dash instance explorer at /plays/. Owned by web/deploy/ in the pbp repo.\n\1include /etc/nginx/pbp-plays.inc;\n\n\1error_page 502 503 504 /maintenance.html;|' "$VHOST"
    echo "   include spliced into pbp.conf"
fi

nginx -t || fail "nginx config invalid -- NOTHING was reloaded. Restore from ${ARCHIVE}/pbp.conf-${stamp}"

# --- 7. Start -----------------------------------------------------------------
log "Starting"
systemctl restart pbp-web
for _ in $(seq 1 60); do
    ss -ltn 2>/dev/null | grep -q "127.0.0.1:${PORT}" && break
    sleep 1
done
systemctl is-active --quiet pbp-web \
    || { journalctl -u pbp-web -n 40 --no-pager; fail "pbp-web did not start"; }

# Reload nginx only once the upstream is actually answering, so no request is
# routed at a port with nothing behind it. The config is already proven valid.
systemctl reload nginx
echo "   pbp-web up, nginx reloaded"

# --- 8. Assert the things that fail silently ----------------------------------
log "Verification"

# THE ONE THAT MATTERS. A bind on 0.0.0.0 publishes this port on the public
# Elastic IP -- past nginx, past its rate limits, past every security header.
# The Streamlit units assert the same thing about --server.address for the same
# reason. Checked live rather than by reading the unit file.
if ss -ltn | awk '{print $4}' | grep -qE "(^|[^0-9.])0\.0\.0\.0:${PORT}|^\*:${PORT}|\[::\]:${PORT}"; then
    fail "${PORT} is listening on a public address -- --bind in pbp-web.service is wrong"
fi
echo "   ${PORT} is loopback-only"

# The app answers, at the prefix, and the prefix is not stripped. A 200 at
# ${PREFIX}/ proves gunicorn is up AND that url_base_pathname agrees with the
# nginx location -- the two halves that silently half-load when they disagree.
code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}${PREFIX}/")"
[[ "$code" == "200" ]] || fail "http://127.0.0.1:${PORT}${PREFIX}/ returned ${code}, expected 200"
echo "   ${PREFIX}/ returns 200 from gunicorn"

# And through nginx, over TLS, on the real hostname -- which additionally proves
# the location block is winning over the Streamlit `location /`.
code="$(curl -s -o /dev/null -w '%{http_code}' "https://pbp.dustincremascoli.com${PREFIX}/")"
[[ "$code" == "200" ]] || fail "https://pbp.dustincremascoli.com${PREFIX}/ returned ${code}"
echo "   ${PREFIX}/ returns 200 through nginx"

# The Streamlit agent at / must be UNDISTURBED. This is the assertion that makes
# sharing a hostname safe: it is the thing a bad location block would break, and
# it would break silently because nobody testing the new app would load the old one.
code="$(curl -s -o /dev/null -w '%{http_code}' https://pbp.dustincremascoli.com/)"
[[ "$code" == "200" ]] || fail "the Streamlit agent at / returned ${code} -- the /plays/ block broke it"
echo "   the Streamlit agent at / is still 200"

# FINGERPRINT AFTER, per NGINX-RUNBOOK.md §9. Same `|| true` as the before hash,
# for the same reason -- though by this point `nginx -t` has already passed, so
# here it really is belt-and-braces.
nginx -T > "${ARCHIVE}/resolved-${stamp}-after.conf" 2>/dev/null || true
after="$( { nginx -T 2>/dev/null || true; } | sha256sum | cut -d' ' -f1)"
if [[ "$before" == "$after" ]]; then
    echo "   resolved config unchanged (re-run, nothing to do)"
else
    echo "   resolved config changed -- diff the two files in ${ARCHIVE} to see exactly what"
fi

# --- Done ---------------------------------------------------------------------
log "Done"
systemctl show pbp-web -p MemoryCurrent | sed 's/MemoryCurrent=/   memory now: /'
echo "   https://pbp.dustincremascoli.com${PREFIX}/"
