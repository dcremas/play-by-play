#!/usr/bin/env bash
# Provision the pbp MCP server on the EC2 box, and the nginx vhost the future
# explorer app will land on.
#
# Run FROM THE MAC:
#   bash mcp_server/deploy/push.sh
#
# or by hand, ON THE BOX, as a user with sudo:
#   ssh awsvm 'sudo bash /tmp/pbp-deploy/deploy/provision.sh'
#
# Idempotent: safe to re-run. Re-running refreshes the code and restarts the
# service, but never rewrites an existing credential file.
#
# HTTP ONLY. This deliberately stops before TLS, because the DNS A record is a
# manual step at GoDaddy (there is no Route53 zone and no API credentials on the
# Mac), and certbot cannot validate a host that does not resolve. Run
# enable-tls.sh once DNS is live.
#
# WHAT THIS DOES NOT DO: create the pbp_ro role. That is setup_role_pbp.sql, run
# against the database, and scripts/sync_ec2.py already runs it after every sync
# because split_leagues.sql drops the schemas whose tables it grants.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
HOSTNAME_APP="${HOSTNAME_APP:-pbp.dustincremascoli.com}"
WEBLOG_SITE="${WEBLOG_SITE:-pbp}"
# One instance per corpus. The name is both the systemd instance and the schema.
declare -A MCP_PORTS=( [cfb]=8771 [nfl]=8772 )
LEAGUES=(cfb nfl)
APP_PORT=8504          # reserved for the explorer; nothing listens on it yet

log()  { printf '\n== %s\n' "$*"; }
fail() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "run with sudo"

# --- 0. Pre-flight ------------------------------------------------------------
# Memory is the real constraint on this box, not disk or CPU. Check before
# installing rather than discovering it when the OOM killer picks the website.
log "Pre-flight"
AVAIL_MB=$(free -m | awk '/^Mem:/ {print $7}')
SWAP_USED=$(free -m | awk '/^Swap:/ {print $3}')
echo "   available memory: ${AVAIL_MB} MB   (swap in use: ${SWAP_USED} MB)"
if (( AVAIL_MB < 400 )); then
    fail "only ${AVAIL_MB} MB available; this service is capped at 360 MB.
Free something first -- this box runs a dozen services on 3.8 GB and is already
using swap."
fi

systemctl is-active --quiet postgresql || fail "postgresql is not running"

# THE INTERPRETER MUST BE NAMED EXPLICITLY. Do not use bare `python3` here.
# Three different answers exist on this box:
#   ec2-user's `python3`  -> 3.13, but it is a PYENV SHIM under /home/ec2-user.
#                            Unusable for a system service: it lives in a home
#                            directory this unit cannot read (ProtectHome=true).
#   root's `python3`      -> 3.9, the AL2023 system default. Too old: every
#                            release of `mcp` requires >=3.10, and a venv built
#                            with it fails with the deeply unhelpful
#                            "Could not find a version that satisfies the
#                            requirement mcp>=1.2 (from versions: none)".
#   /usr/bin/python3.11   -> 3.11.16. This is the one, and it is what
#                            weather-mcp already runs on.
# provision.sh runs under sudo, so it gets root's 3.9 unless told otherwise.
PYTHON="${PYTHON:-/usr/bin/python3.11}"
[[ -x "$PYTHON" ]] || fail "no interpreter at ${PYTHON}.
Install one (dnf install python3.11) or set PYTHON= to a 3.10+ interpreter that
is NOT under a home directory."
PYVER=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
case "$PYVER" in
    3.9|3.8|3.7|2.*) fail "${PYTHON} is ${PYVER}; mcp needs 3.10+" ;;
esac
echo "   interpreter: ${PYTHON} (${PYVER})"

# The port must be free, or systemd will start the unit and the bind will fail
# five seconds later in a way that only shows up in the journal.
# The superseded single-instance unit holds 8771 until step 5 retires it, so it is
# an expected holder here, not a conflict. Anything else on either port is.
for L in "${LEAGUES[@]}"; do
    P=${MCP_PORTS[$L]}
    if ss -ltn | grep -qE "127\.0\.0\.1:${P}\b" \
       && ! systemctl is-active --quiet "pbp-mcp@${L}" \
       && ! systemctl is-active --quiet pbp-mcp.service; then
        fail "something other than pbp-mcp is already listening on ${P}"
    fi
done

# --- 1. Service user ----------------------------------------------------------
# Its own user, separate from weathermcp. Each holds one database credential and
# they are now genuinely DIFFERENT credentials (pbp_ro vs mcp_ro), so this
# separation buys real isolation rather than being decorative.
# THREE users, and the separation is the security model. pbpmcp holds the Postgres
# credential; pbpapp holds the LLM API key and no database credential at all; neither can
# read the other's file. The public-facing Streamlit process is the one an attacker
# reaches first and it has nothing to steal.
log "Service users"
for user in pbpmcp pbpapp; do
    if id "$user" >/dev/null 2>&1; then
        echo "   $user exists"
    else
        useradd --system --no-create-home --shell /sbin/nologin "$user"
        echo "   created $user"
    fi
done

# --- 2. Code ------------------------------------------------------------------
log "Code"
mkdir -p /opt/pbp-mcp
# --delete keeps the target a mirror, but must never remove the venv.
#
# .env IS EXCLUDED DELIBERATELY. The laptop's .env holds the real password at
# mode 600; copying it to /opt would put the credential in a world-traversable
# directory AND in a second place to rotate. On the box the password comes from
# /etc/pbp-mcp/mcp.env via the unit's EnvironmentFile, and db.py's _load_env()
# simply finds no file and falls through to the real environment.
rsync -a --delete \
      --exclude '.venv/' --exclude '__pycache__/' --exclude '.env' \
      --exclude 'deploy/' \
      "$SRC/../" /opt/pbp-mcp/
chown -R pbpmcp:pbpmcp /opt/pbp-mcp
echo "   /opt/pbp-mcp"
[[ ! -e /opt/pbp-mcp/.env ]] || fail "/opt/pbp-mcp/.env exists -- it must not; remove it"

# The explorer. Shipped from the repo's explorer/ directory, which push.sh stages
# alongside mcp_server/.
if [[ -d "$SRC/../explorer" ]]; then
    mkdir -p /opt/pbp-explorer
    rsync -a --delete --exclude '.venv/' --exclude '__pycache__/' --exclude '.env' \
          --exclude '.budget.json' "$SRC/../explorer/" /opt/pbp-explorer/
    chown -R pbpapp:pbpapp /opt/pbp-explorer
    echo "   /opt/pbp-explorer"
    [[ ! -e /opt/pbp-explorer/.env ]] || fail "/opt/pbp-explorer/.env exists -- remove it"
else
    echo "   explorer/ not staged -- skipping the app"
fi

# --- 3. Virtualenv ------------------------------------------------------------
log "Virtualenv"
# Rebuild from scratch if an existing venv was built with the wrong interpreter:
# a 3.9 venv cannot be upgraded in place, and leaving it means every re-run fails
# the same way.
if [[ -x /opt/pbp-mcp/.venv/bin/python ]]; then
    HAVE=$(/opt/pbp-mcp/.venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    if [[ "$HAVE" != "$PYVER" ]]; then
        echo "   existing venv is python ${HAVE}, rebuilding on ${PYVER}"
        rm -rf /opt/pbp-mcp/.venv
    fi
fi
[[ -x /opt/pbp-mcp/.venv/bin/python ]] || "$PYTHON" -m venv /opt/pbp-mcp/.venv
/opt/pbp-mcp/.venv/bin/pip install --quiet --upgrade pip
/opt/pbp-mcp/.venv/bin/pip install --quiet -r /opt/pbp-mcp/requirements.txt
chown -R pbpmcp:pbpmcp /opt/pbp-mcp/.venv
echo "   /opt/pbp-mcp/.venv  ($(/opt/pbp-mcp/.venv/bin/python -V))"

# sqlglot is not optional: run_sql refuses to execute anything at all without it,
# rather than falling back to a regex. Assert it imported rather than trusting
# that pip printed nothing.
/opt/pbp-mcp/.venv/bin/python -c 'import sqlglot, psycopg, mcp' \
    || fail "the venv is missing a required package"
# The app's venv. SEPARATE, and required to be: the servers need mcp>=2 and the app's
# langchain-mcp-adapters needs mcp<2 (it imports RequestContext from a module v2 moved).
# One shared venv cannot satisfy both. They are two processes sharing a wire protocol.
if [[ -d /opt/pbp-explorer ]]; then
    if [[ -x /opt/pbp-explorer/.venv/bin/python ]]; then
        HAVE=$(/opt/pbp-explorer/.venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])')
        [[ "$HAVE" == "$PYVER" ]] || rm -rf /opt/pbp-explorer/.venv
    fi
    [[ -x /opt/pbp-explorer/.venv/bin/python ]] || "$PYTHON" -m venv /opt/pbp-explorer/.venv
    /opt/pbp-explorer/.venv/bin/pip install --quiet --upgrade pip
    /opt/pbp-explorer/.venv/bin/pip install --quiet -r /opt/pbp-explorer/requirements.txt
    chown -R pbpapp:pbpapp /opt/pbp-explorer/.venv
    APP_MCP=$(/opt/pbp-explorer/.venv/bin/python -c 'import importlib.metadata as m; print(m.version("mcp"))')
    echo "   /opt/pbp-explorer/.venv  (mcp ${APP_MCP})"
    [[ ${APP_MCP%%.*} -lt 2 ]] || fail "the app venv needs mcp<2, has ${APP_MCP}"
fi

MCP_VER=$(/opt/pbp-mcp/.venv/bin/python -c 'import importlib.metadata as m; print(m.version("mcp"))')
[[ ${MCP_VER%%.*} -ge 2 ]] || fail "the server venv needs mcp>=2, has ${MCP_VER}"
echo "   mcp ${MCP_VER}, sqlglot $(/opt/pbp-mcp/.venv/bin/python -c 'import sqlglot;print(sqlglot.__version__)')"

# --- 4. Credential ------------------------------------------------------------
# Created empty if absent and NEVER overwritten, so a re-run cannot clobber a
# working secret. Readable only by root and pbpmcp.
log "Credential file"
mkdir -p /etc/pbp-mcp; chmod 755 /etc/pbp-mcp
if [[ -s /etc/pbp-mcp/mcp.env ]]; then
    echo "   /etc/pbp-mcp/mcp.env exists -- left untouched"
else
    cat > /etc/pbp-mcp/mcp.env <<'EOT'
# Password for the pbp_ro Postgres role. THIS SERVER'S OWN ROLE -- not the
# shared mcp_ro the weather MCP uses. Set it with:
#   sudo -u postgres psql -d pbp -c "ALTER ROLE pbp_ro PASSWORD '<value>'"
MCP_DB_PASSWORD=
EOT
    echo "   /etc/pbp-mcp/mcp.env CREATED EMPTY -- fill it in before the service will work"
fi
chown root:pbpmcp /etc/pbp-mcp/mcp.env
chmod 640 /etc/pbp-mcp/mcp.env

if [[ -d /opt/pbp-explorer ]]; then
    mkdir -p /etc/pbp-explorer; chmod 755 /etc/pbp-explorer
    if [[ -s /etc/pbp-explorer/app.env ]]; then
        echo "   /etc/pbp-explorer/app.env exists -- left untouched"
    else
        cat > /etc/pbp-explorer/app.env <<'EOT'
# Google AI Studio key for Gemini. NO DATABASE CREDENTIAL BELONGS IN THIS FILE -- the app
# reaches data only through the MCP servers on the loopback.
PBPX_API_KEY=
# Spend caps. Tighter than the laptop defaults: this endpoint is public. nginx cannot
# enforce them -- see explorer/budget.py.
PBPX_MAX_SESSION_QUESTIONS=10
PBPX_MAX_DAILY_TOKENS=1500000
EOT
        echo "   /etc/pbp-explorer/app.env CREATED EMPTY -- fill in PBPX_API_KEY"
    fi
    chown root:pbpapp /etc/pbp-explorer/app.env
    chmod 640 /etc/pbp-explorer/app.env
    # The whole reason for a third user: it must not be able to read the database password.
    if sudo -u pbpapp test -r /etc/pbp-mcp/mcp.env 2>/dev/null; then
        fail "pbpapp can read /etc/pbp-mcp/mcp.env -- the isolation is broken"
    fi
    echo "   pbpapp cannot read the database credential (checked)"
fi

# The separation is the point, so prove it rather than asserting it in a comment.
if id weathermcp >/dev/null 2>&1; then
    if sudo -u weathermcp test -r /etc/pbp-mcp/mcp.env 2>/dev/null; then
        fail "weathermcp can read /etc/pbp-mcp/mcp.env -- the isolation is broken"
    fi
    echo "   weathermcp cannot read this file (checked)"
fi

# --- 5. systemd ---------------------------------------------------------------
log "systemd units"
install -m 644 "$SRC/pbp-mcp@.service" /etc/systemd/system/
# One port file per instance; systemd cannot derive a port from %i.
for L in "${LEAGUES[@]}"; do
    printf 'MCP_HTTP_PORT=%s\n' "${MCP_PORTS[$L]}" > "/etc/pbp-mcp/port-${L}.env"
    chown root:pbpmcp "/etc/pbp-mcp/port-${L}.env"; chmod 640 "/etc/pbp-mcp/port-${L}.env"
done
# The single-instance unit this replaced. Left running it would hold 8771 and the
# template instance would fail to bind, with the cause five lines deep in a journal.
if [[ -f /etc/systemd/system/pbp-mcp.service ]]; then
    systemctl disable --now pbp-mcp.service 2>/dev/null || true
    rm -f /etc/systemd/system/pbp-mcp.service
    echo "   removed the superseded single-instance unit"
fi
if [[ -d /opt/pbp-explorer ]]; then
    install -m 644 "$SRC/pbp-explorer.service" /etc/systemd/system/
fi
systemctl daemon-reload
for L in "${LEAGUES[@]}"; do systemctl enable --quiet "pbp-mcp@${L}"; done
[[ -d /opt/pbp-explorer ]] && systemctl enable --quiet pbp-explorer.service
echo "   installed pbp-mcp@{${LEAGUES[*]}}$([[ -d /opt/pbp-explorer ]] && echo " + pbp-explorer") and enabled"

# --- 6. nginx -----------------------------------------------------------------
# The vhost goes in AHEAD of the app it proxies. Until something listens on
# ${APP_PORT} it serves the maintenance page; see the header of pbp.conf.
log "nginx"
install -m 644 "$SRC/proxy_params_pbp.inc" /etc/nginx/
# error_page target referenced by this vhost AND by sqlx.conf, which has pointed
# at a file that did not exist since it was written.
if [[ ! -f /usr/share/nginx/html/maintenance.html ]]; then
    install -m 644 "$SRC/maintenance.html" /usr/share/nginx/html/maintenance.html
    echo "   installed maintenance.html (was absent -- sqlx.conf referenced it too)"
else
    echo "   maintenance.html present"
fi

if [[ ! -f /etc/nginx/conf.d/pbp.conf ]]; then
    install -m 644 "$SRC/pbp-http.conf" /etc/nginx/conf.d/pbp.conf
    echo "   installed HTTP-only vhost (run enable-tls.sh after DNS)"
else
    echo "   /etc/nginx/conf.d/pbp.conf exists -- left as-is"
fi

# default_server is claimed by the first `listen` nginx parses, which is the
# alphabetically-first .conf in conf.d holding a server block. Adding a file that
# sorts earlier would silently hand every unknown-Host request to this vhost,
# answered with this certificate. Assert it rather than trusting the filename.
FIRST_VHOST=$(grep -lE '^\s*listen' /etc/nginx/conf.d/*.conf | sort | head -1)
if [[ "$(basename "$FIRST_VHOST")" == "pbp.conf" ]]; then
    fail "pbp.conf now sorts first in conf.d and has taken default_server.
Rename it to something sorting after $(ls /etc/nginx/conf.d/*.conf | sort | head -1)."
fi
echo "   default_server still belongs to $(basename "$FIRST_VHOST")"

# The weblog map entry. Without it this host falls to `default other`, and
# `other` on this box means "scanner hitting an unknown Host" -- a real signal
# that must not be polluted by a legitimate vhost.
if grep -q "\"${HOSTNAME_APP}\"" /etc/nginx/conf.d/00-weblog.conf; then
    echo "   weblog map already has ${HOSTNAME_APP}"
else
    cp -a /etc/nginx/conf.d/00-weblog.conf "/etc/nginx/conf.d/00-weblog.conf.bak-$(date +%Y%m%d)"
    sed -i "s|^\(\s*\)\"sql\.dustincremascoli\.com\"\(\s*\)sqlx;|&\n\1\"${HOSTNAME_APP}\"\2${WEBLOG_SITE};|" \
        /etc/nginx/conf.d/00-weblog.conf
    grep -q "\"${HOSTNAME_APP}\"" /etc/nginx/conf.d/00-weblog.conf \
        || fail "could not add ${HOSTNAME_APP} to the weblog map -- add it by hand"
    echo "   added ${HOSTNAME_APP} -> ${WEBLOG_SITE} to the weblog map"
fi

nginx -t || fail "nginx config invalid -- nothing was reloaded, fix and re-run"

# RELOAD HERE, not after starting the service. If the service fails to start,
# `fail` exits before a reload placed at the end -- leaving the vhost file and
# the weblog map edit ON DISK but NOT in the running config, which produces a
# thoroughly misleading symptom: requests for this host fall through to the
# default server and are answered with the wrong certificate. The config is
# already proven valid by `nginx -t`, so reloading now is safe.
systemctl reload nginx
echo "   reloaded nginx"

# --- 7. Start -----------------------------------------------------------------
log "Starting both instances"
for L in "${LEAGUES[@]}"; do
    P=${MCP_PORTS[$L]}
    systemctl restart "pbp-mcp@${L}"
    for _ in $(seq 1 30); do
        ss -ltn 2>/dev/null | grep -q "127.0.0.1:${P}" && break
        sleep 1
    done
    systemctl is-active --quiet "pbp-mcp@${L}" \
        || fail "pbp-mcp@${L} did not start: journalctl -u pbp-mcp@${L} -n 40"
    echo "   pbp-mcp@${L} on ${P}"
done

if [[ -d /opt/pbp-explorer ]]; then
    systemctl restart pbp-explorer.service
    for _ in $(seq 1 60); do
        curl -fsS -m 2 "http://127.0.0.1:${APP_PORT}/_stcore/health" >/dev/null 2>&1 && break
        sleep 1
    done
    systemctl is-active --quiet pbp-explorer.service \
        || fail "pbp-explorer did not start: journalctl -u pbp-explorer -n 40"
    echo "   pbp-explorer on ${APP_PORT}"
fi

# --- 8. Assert the things that fail silently ----------------------------------
log "Verification"

for L in "${LEAGUES[@]}"; do
    P=${MCP_PORTS[$L]}
    # The whole security model for the MCP endpoint is that it is loopback-only: it
    # has no authentication at all. This is the check that catches a wrong
    # MCP_HTTP_HOST before the internet does.
    if ss -ltn | awk '{print $4}' | grep -qE "(^|[^0-9.])0\.0\.0\.0:${P}|^\*:${P}|\[::\]:${P}"; then
        fail "${P} is listening on a public address -- MCP_HTTP_HOST is wrong"
    fi
    # And that nginx is not proxying it. A `location /mcp` would publish an
    # unauthenticated SQL interface.
    if grep -rqE "proxy_pass\s+https?://127\.0\.0\.1:${P}" /etc/nginx/; then
        fail "nginx proxies ${P} -- that endpoint has NO authentication"
    fi
    echo "   ${P} (${L}) is loopback-only and not proxied"

    # End-to-end through the MCP protocol, not just a port check: a listening socket
    # with a broken database credential looks identical from the outside.
    INIT=$(curl -s -m 15 -X POST "http://127.0.0.1:${P}/mcp" \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"provision","version":"0"}}}' 2>&1 || true)
    grep -q '"serverInfo"' <<<"$INIT" \
        && echo "     MCP initialize ok" \
        || echo "     WARNING: ${L} did not answer initialize -- journalctl -u pbp-mcp@${L} -n 40"

    # THE ENVIRONMENT IS LOAD-BEARING. Connection settings live as `Environment=`
    # lines in the unit; the env files hold only the password and the port. Invoked
    # without the unit's environment, db.py falls back to its development default of
    # port 15432 -- the laptop's SSH tunnel -- and every check fails with
    # "connection refused" against a perfectly healthy local database.
    #
    # `env $UNIT_ENV` is deliberately unquoted: systemctl returns space-separated
    # KEY=VALUE pairs that must word-split into separate arguments.
    UNIT_ENV=$(systemctl show "pbp-mcp@${L}" -p Environment --value)
    SELFTEST_CMD="sudo -u pbpmcp env \$(systemctl show pbp-mcp@${L} -p Environment --value) \\
     bash -c 'set -a; . /etc/pbp-mcp/mcp.env; . /etc/pbp-mcp/port-${L}.env; set +a
       cd /opt/pbp-mcp && ./.venv/bin/python -m pbp_mcp.selftest'"
    # shellcheck disable=SC2086
    SELFTEST=$(sudo -u pbpmcp env $UNIT_ENV bash -c \
        "set -a; . /etc/pbp-mcp/mcp.env; . /etc/pbp-mcp/port-${L}.env; set +a
         cd /opt/pbp-mcp && ./.venv/bin/python -m pbp_mcp.selftest" 2>&1 || true)
    if grep -q "checks passed" <<<"$SELFTEST"; then
        grep -E "checks passed" <<<"$SELFTEST" | sed "s/^/     ${L}: /"
        if grep -q "FAIL" <<<"$SELFTEST"; then
            echo "     ^ some checks FAILED. To see which:"
            echo "       $SELFTEST_CMD"
        fi
    else
        echo "     selftest produced no summary -- the suite could not run. Try:"
        echo "       $SELFTEST_CMD"
    fi
done

# Streamlit binding 0.0.0.0 would publish 8504 on the public EIP, past nginx and past its
# rate limits. This is the check that catches it.
if [[ -d /opt/pbp-explorer ]]; then
    if ss -ltn | awk '{print $4}' | grep -qE "(^|[^0-9.])0\.0\.0\.0:${APP_PORT}|^\*:${APP_PORT}|\[::\]:${APP_PORT}"; then
        fail "${APP_PORT} is listening on a public address -- --server.address is missing"
    fi
    echo "   ${APP_PORT} is loopback-only"
    curl -fsS -m 5 "http://127.0.0.1:${APP_PORT}/_stcore/health" >/dev/null \
        && echo "   streamlit health ok" \
        || echo "   WARNING: streamlit health check failed"
fi

for L in "${LEAGUES[@]}"; do
    MEM=$(systemctl show "pbp-mcp@${L}" -p MemoryCurrent --value)
    echo "   pbp-mcp@${L} memory: $(( MEM / 1024 / 1024 )) MB (MemoryMax 360M)"
done
if [[ -d /opt/pbp-explorer ]]; then
    MEM=$(systemctl show pbp-explorer -p MemoryCurrent --value)
    echo "   pbp-explorer memory: $(( MEM / 1024 / 1024 )) MB (MemoryMax 560M)"
fi

# --- Done ---------------------------------------------------------------------
log "Done"

# The remaining-steps list is CONDITIONAL, so a routine redeploy of an
# already-live host does not end with a list of first-install chores that reads
# as a failed deploy.
REMAINING=0
note() { REMAINING=$((REMAINING + 1)); echo "  ${REMAINING}. $1"; }

echo "pbp-mcp@cfb is on 127.0.0.1:${MCP_PORTS[cfb]}, pbp-mcp@nfl on ${MCP_PORTS[nfl]}."
echo

if ! grep -q '[^[:space:]]' <<<"$(sed -n 's/^MCP_DB_PASSWORD=//p' /etc/pbp-mcp/mcp.env)"; then
    note "Fill in MCP_DB_PASSWORD in /etc/pbp-mcp/mcp.env, then:
       systemctl restart pbp-mcp@cfb pbp-mcp@nfl"
fi

if ! getent hosts "${HOSTNAME_APP}" >/dev/null 2>&1; then
    note "Add the DNS A record at GoDaddy (manual -- no Route53 zone, no API creds):
       ${HOSTNAME_APP}  A  $(curl -fsS -H "X-aws-ec2-metadata-token: $(curl -fsS -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null)" http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo '<EC2_PUBLIC_IP>')
     Confirm with: dig +short ${HOSTNAME_APP}"
fi

if [[ ! -d "/etc/letsencrypt/live/${HOSTNAME_APP}" ]]; then
    note "Issue the certificate: sudo bash ${SRC}/enable-tls.sh"
fi

if [[ -d /opt/pbp-explorer ]] \
   && ! grep -q '[^[:space:]]' <<<"$(sed -n 's/^PBPX_API_KEY=//p' /etc/pbp-explorer/app.env)"; then
    note "Fill in PBPX_API_KEY in /etc/pbp-explorer/app.env, then:
       systemctl restart pbp-explorer"
fi

echo
echo "${REMAINING} step(s) outstanding."
