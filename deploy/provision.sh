#!/usr/bin/env bash
# Root side of deploy/push.sh: install the play-by-play pipeline at /opt/pbp-etl and the
# systemd units that run it. Idempotent -- every code deploy runs it again.
#
#     sudo bash /tmp/pbp-etl-deploy/deploy/provision.sh [--enable]
#
# --enable  also start the 06:00 timer and the hourly heartbeat. Leave it off until one
#           manual `systemctl start pbp-daily` has succeeded.
#
# What it sets up, and why each piece is shaped the way it is:
#   pbpetl          system user, no login. Runs the pipeline. Not a superuser in Postgres:
#                   it OWNS the warehouse schemas (pbp, raw, cfb, nfl and their _next/_old
#                   stages), may read server files for COPY, and may create schemas.
#                   Creating and configuring pbp_ro stays an administrator's job
#                   (mcp_server/setup_role_pbp.sql, run as postgres).
#   /opt/pbp-etl    the repo, owned by pbpetl, with data/ beside the code as in any checkout.
#   two venvs       .venv on Python 3.14.7 from uv (the pipeline's pin), dbt/.venv on the
#                   uv-managed 3.13 (dbt does not import on 3.14). Rebuilt only when a
#                   requirements file changes.
set -euo pipefail

STAGE="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/pbp-etl
USER_NAME=pbpetl
UV=/home/ec2-user/.local/bin/uv
ENABLE=0
[[ "${1:-}" == "--enable" ]] && ENABLE=1

log()  { printf '\n== %s\n' "$*"; }
fail() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }
as_app() { sudo -u "$USER_NAME" -H "$@"; }
pg()     { sudo -u postgres psql -d pbp -v ON_ERROR_STOP=1 -q "$@"; }

[[ $EUID -eq 0 ]] || fail "run as root"
[[ -f "$STAGE/scripts/daily_box.sh" ]] || fail "no staged repo at $STAGE"
[[ -x "$UV" ]] || fail "uv not found at $UV"

log "User"
id "$USER_NAME" >/dev/null 2>&1 \
    || useradd --system --home-dir "$APP" --no-create-home --shell /sbin/nologin "$USER_NAME"
echo "   $(id "$USER_NAME")"

log "Code -> $APP"
mkdir -p "$APP/data"
rsync -a --delete \
      --exclude '/.venv/' --exclude '/.python/' --exclude '/data/' \
      --exclude '/dbt/.venv/' --exclude '/dbt/target/' --exclude '/dbt/dbt_packages/' \
      --exclude '/dbt/logs/' --exclude '__pycache__/' \
      "$STAGE/" "$APP/"
chown -R "$USER_NAME:$USER_NAME" "$APP"
# Postgres's server-side COPY reads data/out as the postgres user.
chmod 755 "$APP" "$APP/data"
[[ -d "$APP/data/out" ]] || fail "no data at $APP/data/out -- run deploy/push.sh --seed first"
chmod -R a+rX "$APP/data/out"
chmod 755 "$APP"/scripts/*.sh "$APP"/deploy/*.sh

log "Python venvs"
build_venv() {   # <venv dir> <requirements> <command that creates the venv>
    local venv="$1" req="$2"; shift 2
    local want; want="$(sha256sum "$req" | cut -d' ' -f1)"
    if [[ -x "$venv/bin/python" && "$(cat "$venv/.req-sha256" 2>/dev/null)" == "$want" ]]; then
        echo "   $venv up to date"; return
    fi
    rm -rf "$venv"
    "$@"
    "$UV" pip install --quiet --only-binary :all: --python "$venv/bin/python" -r "$req"
    echo "$want" > "$venv/.req-sha256"
    chown -R "$USER_NAME:$USER_NAME" "$venv"
    echo "   built $venv"
}
export UV_PYTHON_INSTALL_DIR="$APP/.python" UV_CACHE_DIR=/var/cache/pbp-etl-uv
build_venv "$APP/.venv" "$APP/requirements.txt" \
    "$UV" venv --quiet --python 3.14.7 --python-preference only-managed "$APP/.venv"
build_venv "$APP/dbt/.venv" "$APP/dbt/requirements.txt" \
    "$UV" venv --quiet --python 3.13 --python-preference only-managed "$APP/dbt/.venv"
chown -R "$USER_NAME:$USER_NAME" "$APP/.python"
echo "   $("$APP/.venv/bin/python" --version), dbt $("$APP/dbt/.venv/bin/dbt" --version 2>/dev/null | awk '/installed/{print $3; exit}')"

# Every league's raw ESPN directory, as scripts/league.py names it, with a full history of
# participants files. The athlete build reads them all; with one league's missing it still
# "succeeds" and silently strips that league's athlete links (2026-11-05, the NFL).
for d in $(cd "$APP/scripts" && "$APP/.venv/bin/python" -c 'import league as lg; print(" ".join(lg.data_dir(l) for l in lg.SPEC))'); do
    n=$(find "$d/participants" -name '*.json.gz' 2>/dev/null | wc -l)
    (( n >= 1000 )) || fail "$d has $n participants files -- seed it with deploy/push.sh --seed"
    echo "   $d: $n participants files"
done

log "Postgres role"
pg <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$USER_NAME') THEN
        CREATE ROLE $USER_NAME LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 6;
    END IF;
END \$\$;
GRANT pg_read_server_files TO $USER_NAME;
GRANT CONNECT, CREATE ON DATABASE pbp TO $USER_NAME;
CREATE SCHEMA IF NOT EXISTS raw;

-- Hand every warehouse schema and everything in it to the pipeline's role. Grants to pbp_ro
-- survive an owner change. Matches the live corpora and their build stages only; weather
-- and every other database are untouched.
DO \$\$
DECLARE s text; r record;
BEGIN
    FOR s IN SELECT nspname FROM pg_namespace
             WHERE nspname ~ '^(pbp|raw|cfb|nfl)(_next|_old|_dbt)?(_staging)?\$' LOOP
        EXECUTE format('ALTER SCHEMA %I OWNER TO $USER_NAME', s);
        FOR r IN SELECT c.relname, c.relkind FROM pg_class c
                 JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = s AND c.relkind IN ('r','v','m','S','p') LOOP
            EXECUTE format('ALTER %s %I.%I OWNER TO $USER_NAME',
                CASE r.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                               WHEN 'S' THEN 'SEQUENCE' ELSE 'TABLE' END, s, r.relname);
        END LOOP;
        RAISE NOTICE 'schema % -> $USER_NAME', s;
    END LOOP;
END \$\$;
SQL

# Peer auth for exactly one pairing: OS user pbpetl as role pbpetl, database pbp, local socket.
# The box's catch-all `local all all scram-sha-256` would otherwise demand a password, and
# first match wins, so the rule goes directly above it. Backed up before the edit; the file's
# owner and mode are kept by rewriting it in place.
HBA="$(sudo -u postgres psql -Atc 'show hba_file')"
if ! grep -qE "^local[[:space:]]+pbp[[:space:]]+$USER_NAME[[:space:]]+peer" "$HBA"; then
    cp -p "$HBA" "$HBA.bak-$(date -u +%Y%m%dT%H%M%SZ)"
    awk -v rule="local   pbp             $USER_NAME          peer    # pbp-daily (deploy/provision.sh)" \
        '!done && /^local[ \t]+all[ \t]+all[ \t]/ { print rule; done = 1 } { print }' \
        "$HBA" > "$HBA.new"
    grep -q "^local   pbp             $USER_NAME" "$HBA.new" || fail "no 'local all all' line to insert above in $HBA"
    cat "$HBA.new" > "$HBA" && rm -f "$HBA.new"
    sudo -u postgres psql -Atc "SELECT pg_reload_conf()" >/dev/null
    echo "   pg_hba: added peer auth for $USER_NAME on pbp, reloaded"
fi
bad="$(sudo -u postgres psql -Atc "SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL")"
[[ "$bad" == 0 ]] || fail "pg_hba.conf has $bad unparseable line(s) -- restore $HBA.bak-*"

as_app psql -d pbp -Atc "SELECT 'connected as ' || current_user || ', owns ' ||
    (SELECT count(*) FROM pg_namespace WHERE pg_get_userbyid(nspowner) = current_user) || ' schemas'" \
    | sed 's/^/   /' || fail "pbpetl cannot connect over the local socket (pg_hba peer auth?)"

log "dbt"
as_app bash -c "cd $APP/dbt && DBT_PROFILES_DIR=. PBP_PGHOST=/var/run/postgresql \
    DBT_SEND_ANONYMOUS_USAGE_STATS=false .venv/bin/dbt deps --quiet && \
    DBT_PROFILES_DIR=. PBP_PGHOST=/var/run/postgresql DBT_SEND_ANONYMOUS_USAGE_STATS=false \
    .venv/bin/dbt debug --quiet --vars '{league: cfb}'" && echo "   dbt connects"

log "systemd units"
MEM="${PBP_MEMORY_MAX:-2500M}"
sed "s/__MEMORY_MAX__/$MEM/" "$APP/deploy/pbp-daily.service" > /etc/systemd/system/pbp-daily.service
for u in pbp-daily.timer pbp-daily-heartbeat.service pbp-daily-heartbeat.timer; do
    install -m 644 "$APP/deploy/$u" "/etc/systemd/system/$u"
done
systemctl daemon-reload
echo "   installed pbp-daily.{service,timer}, pbp-daily-heartbeat.{service,timer} (MemoryMax=$MEM)"
if (( ENABLE )); then
    systemctl enable --now pbp-daily.timer pbp-daily-heartbeat.timer
    echo "   timers enabled"
fi
systemctl list-timers --all --no-pager | grep -E "NEXT|pbp-daily" | sed 's/^/   /'

log "Done"
echo "   first run:  sudo systemctl start pbp-daily && sudo journalctl -u pbp-daily -f"
