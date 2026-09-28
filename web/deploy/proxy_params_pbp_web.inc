# Shared proxy settings for the Dash instance explorer upstream.
#
# Install to /etc/nginx/proxy_params_pbp_web.inc.
#
# Separate from proxy_params_pbp.inc rather than shared with it, because the two
# apps on this hostname are different shapes:
#
#   proxy_params_pbp.inc   Streamlit. Hard-codes `proxy_pass http://pbp_app`,
#                          forwards the WebSocket Upgrade handshake, and sets
#                          3600s timeouts for a socket that stays open for the
#                          life of the session.
#   this file              Dash. Ordinary request/response over HTTP. No Upgrade
#                          header, no hour-long timeouts, and a different
#                          upstream.
#
# Sharing them would mean one file with two `proxy_pass` targets, which is not a
# thing nginx can express.

# No trailing URI. The /plays/ prefix is passed through to gunicorn unchanged --
# see the long note in pbp-plays.inc for what breaks if that changes.
proxy_pass http://pbp_web;
proxy_http_version 1.1;

proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host  $host;

# Deliberately NO Upgrade/Connection headers. Dash opens no WebSocket, and the
# vhost's $connection_upgrade map would set "close" on every request here --
# harmless, but it would imply this app has a socket to upgrade. It does not.

proxy_redirect off;

# Buffering ON, which is the opposite of the Streamlit half and is correct here.
# Streamlit disables it because it streams UI updates and any buffer adds
# latency to every keystroke. Dash returns one complete JSON body per callback,
# and letting nginx buffer it frees the gunicorn worker as soon as the response
# is written instead of holding it for the duration of a slow client's download.
# With 2 workers, that is the difference between a slow mobile connection tying
# up half the app and tying up none of it.
proxy_buffering on;
proxy_buffers 16 32k;
proxy_busy_buffers_size 64k;

proxy_connect_timeout 5s;
proxy_send_timeout    60s;

# ONE read timeout for every location, set HERE and nowhere else.
#
# The first draft had 60s here and a 120s override on the callback location, and
# nginx rejected the whole config: an `include` splices its directives in at the
# including level, so the two land in the same block and
# `proxy_read_timeout is duplicate` is a hard error, not a last-one-wins. Any
# per-location override therefore means removing this line and repeating it in
# all four blocks.
#
# 120s is the callback endpoint's number, because that is the only location where
# the value matters: an unfiltered sort across 1.5M scrimmage rows is a real query
# on 2 shared vCPU. The static locations inherit it and are unaffected -- this is
# a ceiling on waiting for an upstream that answers those in milliseconds, not a
# delay anything actually spends.
#
# It matches --timeout 120 in pbp-web.service deliberately. A worker killed at
# gunicorn's default 30s while nginx waits 120s turns a slow query into a 502
# with nothing useful in either log.
proxy_read_timeout    120s;
