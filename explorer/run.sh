#!/usr/bin/env bash
# Run the explorer locally. Checks the things that fail confusingly, then starts Streamlit.
#
#     ./run.sh
#
# Needs BOTH MCP servers up, one per corpus. They are separate processes against separate
# schemas; a missing one is not a degraded app, it is a league that cannot be selected.
set -euo pipefail
cd "$(dirname "$0")"

[[ -x .venv/bin/streamlit ]] || {
    echo "no venv -- python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
}

for pair in cfb:8771 nfl:8772; do
    lg=${pair%%:*}; port=${pair##*:}
    if ! curl -fsS -m 3 -o /dev/null -X POST "http://127.0.0.1:${port}/mcp" \
         -H 'Content-Type: application/json' \
         -H 'Accept: application/json, text/event-stream' \
         -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"run.sh","version":"0"}}}' 2>/dev/null; then
        echo "The ${lg} MCP server is not answering on ${port}. Start it with:" >&2
        echo "  (cd ../mcp_server && MCP_DB_SCHEMA=${lg} MCP_HTTP_PORT=${port} \\" >&2
        echo "      ./.venv/bin/python -m pbp_mcp.server --http)" >&2
        exit 1
    fi
done
echo "both MCP servers answering"

# --server.address is load-bearing even locally: it is the same flag the systemd unit
# relies on, and testing without it means never testing it.
exec .venv/bin/streamlit run app.py \
    --server.address=127.0.0.1 \
    --server.port="${PBPX_PORT:-8504}" \
    --server.headless=true \
    --browser.gatherUsageStats=false
