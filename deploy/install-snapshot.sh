#!/usr/bin/env bash
# Root half of pbp-daily.service: copy a newer DuckDB snapshot to where pbp-web reads it, and
# restart the app so it opens the new file. Does nothing on a day with nothing new.
#
# Same ownership and modes web/deploy/provision.sh sets: root:pbpweb, 640.
set -euo pipefail

SRC=/opt/pbp-etl/data/out
DST=/var/lib/pbp-web
changed=0

for lg in cfb nfl; do
    src="$SRC/pbp_${lg}.duckdb"
    dst="$DST/pbp_${lg}.duckdb"
    [[ -s "$src" ]] || { echo "no snapshot at $src"; exit 1; }
    # install stamps dst with the time of copy, so an unchanged src is never newer again.
    if [[ ! -e "$dst" || "$src" -nt "$dst" ]]; then
        install -o root -g pbpweb -m 640 "$src" "$dst.new"
        mv -f "$dst.new" "$dst"
        echo "installed $dst"
        changed=1
    fi
done

if (( changed )); then
    systemctl try-restart pbp-web.service
    echo "restarted pbp-web"
fi
