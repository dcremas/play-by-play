#!/usr/bin/env bash
# Refresh this laptop's copy of the warehouse from the EC2 box, the system of record since
# 2026-11. Run it when you want to develop against current data; nothing else needs it.
#
#     bash scripts/pull_from_box.sh            # data/ files and Postgres
#     bash scripts/pull_from_box.sh --files    # data/ files only (fast; the apps read these)
#
# Files: every league's ESPN files and data/out, including both DuckDB snapshots, so the local explorer shows
# what production shows. Postgres: pbp, raw, cfb and nfl, dumped on the box and restored
# here over the local copies. Your local dev schemas (cfb_dbt, nfl_dbt) are left alone, except
# their staging views, which depend on pbp.* and are dropped so the restore can replace it;
# the next `dbt build` recreates them.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SSH_HOST="${SSH_HOST:-awsvm}"
APP=/opt/pbp-etl
DB=pbp
FILES_ONLY=0
[[ "${1:-}" == "--files" ]] && FILES_ONLY=1

log() { printf '\n== %s\n' "$*"; }

log "Files from $SSH_HOST:$APP/data"
mkdir -p "$REPO/data"
# All of data/: each league's raw ESPN files are in their own directory (scripts/league.py).
rsync -az --progress --exclude '/logs/' --exclude '/state/' --exclude '.duckdb_spill/' \
      "$SSH_HOST:$APP/data/" "$REPO/data/"
(( FILES_ONLY )) && { log "Done (files only)"; exit 0; }

DUMP="$(mktemp -t pbp_box).dump"
trap 'rm -f "$DUMP"' EXIT
log "Dumping pbp, raw, cfb, nfl on the box"
ssh "$SSH_HOST" "sudo -u postgres pg_dump -d $DB -Fc -n pbp -n raw -n cfb -n nfl" > "$DUMP"
ls -lh "$DUMP" | awk '{print "   " $5}'

log "Restoring into local $DB"
psql -d "$DB" -q -c "DROP SCHEMA IF EXISTS cfb_dbt_staging, nfl_dbt_staging CASCADE"
# --no-owner: the box's objects belong to pbpetl, a role this laptop does not have.
pg_restore -d "$DB" --clean --if-exists --no-owner --exit-on-error "$DUMP"
psql -d "$DB" -Atc "SELECT 'pbp.special_teams_play ' || count(*) FROM pbp.special_teams_play
                    UNION ALL SELECT 'pbp.scrimmage_play ' || count(*) FROM pbp.scrimmage_play" \
    | sed 's/^/   /'
log "Done"
