#!/usr/bin/env bash
# Obtain a Let's Encrypt certificate for the explorer host and swap in the TLS
# vhost.
#
# Run ON THE BOX, AFTER the DNS A record resolves:
#   sudo bash /tmp/pbp-deploy/deploy/enable-tls.sh
# or from the Mac:  bash mcp_server/deploy/push.sh --tls
#
# SELF-REVERTING. If `nginx -t` fails with the TLS vhost in place, the previous
# HTTP-only file is restored and nginx is left running on the working config --
# a broken vhost file takes down EVERY site on the box, not just this one, so
# this must never leave nginx unable to load.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
HOSTNAME_APP="${HOSTNAME_APP:-pbp.dustincremascoli.com}"
EMAIL="${CERTBOT_EMAIL:?set CERTBOT_EMAIL to the address certbot registers with}"
VHOST=/etc/nginx/conf.d/pbp.conf

log()  { printf '\n== %s\n' "$*"; }
fail() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "run with sudo"

log "Checking DNS"
RESOLVED=$(dig +short "$HOSTNAME_APP" | tail -1)
echo "   ${HOSTNAME_APP} -> ${RESOLVED:-<nothing>}"
[[ -n "$RESOLVED" ]] || fail "${HOSTNAME_APP} does not resolve.
Add the A record at GoDaddy first:  ${HOSTNAME_APP}  A  <EC2_PUBLIC_IP>
certbot cannot validate a host that does not resolve, and failed attempts count
against Let's Encrypt rate limits."

# IMDSv2. This box requires a token, so the plain IMDSv1 GET returns 401 --
# which, because MY_IP is only checked when non-empty, would make this guard
# silently dead code. Fetch the token first.
IMDS_TOKEN=$(curl -fsS --max-time 5 -X PUT \
    "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null || echo "")
if [[ -n "$IMDS_TOKEN" ]]; then
    MY_IP=$(curl -fsS --max-time 5 -H "X-aws-ec2-metadata-token: ${IMDS_TOKEN}" \
        http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo "")
else
    MY_IP=""
fi
if [[ -z "$MY_IP" ]]; then
    # Say so rather than passing quietly: a skipped check that looks like a
    # passed check is how the wrong A record reaches certbot.
    echo "   WARNING: could not read this instance's public IP from IMDS --"
    echo "   skipping the does-DNS-point-here check. Verify by hand if unsure."
else
    echo "   this instance: ${MY_IP}"
fi
if [[ -n "$MY_IP" && "$RESOLVED" != "$MY_IP" ]]; then
    fail "${HOSTNAME_APP} resolves to ${RESOLVED} but this box is ${MY_IP}.
Fix the A record, or wait for the old value's TTL to expire."
fi

log "Requesting the certificate"
mkdir -p /var/www/letsencrypt
if [[ -d "/etc/letsencrypt/live/${HOSTNAME_APP}" ]]; then
    echo "   certificate already exists -- skipping issuance"
else
    # --webroot, not --nginx: the nginx plugin rewrites the vhost itself, which
    # would fight the checked-in file. The HTTP vhost installed by provision.sh
    # already serves /.well-known/acme-challenge/ from this root.
    certbot certonly --webroot -w /var/www/letsencrypt \
        -d "$HOSTNAME_APP" \
        --non-interactive --agree-tos -m "$EMAIL" \
        || fail "certbot failed -- nothing was changed"
fi

log "Installing the TLS vhost"
[[ -f "$VHOST" ]] || fail "${VHOST} is missing -- run provision.sh first"
BACKUP="${VHOST}.bak-$(date +%Y%m%d-%H%M%S)"
cp -a "$VHOST" "$BACKUP"
install -m 644 "$SRC/pbp.conf" "$VHOST"

if ! nginx -t; then
    cp -a "$BACKUP" "$VHOST"
    nginx -t || fail "config is broken even after reverting -- DO NOT reload nginx"
    fail "TLS vhost failed nginx -t; reverted to ${BACKUP}. Nothing was reloaded."
fi

systemctl reload nginx
echo "   reloaded (previous file kept at ${BACKUP})"

log "Verification"
systemctl is-active --quiet certbot-renew.timer \
    && echo "   certbot-renew.timer active" \
    || echo "   WARNING: certbot-renew.timer is NOT active -- this cert will expire"

# 502 is the CORRECT answer until the explorer app exists. Say so, or the next
# person reads a successful deploy as a broken one.
code=$(curl -s -o /dev/null -w '%{http_code}' -L "https://${HOSTNAME_APP}/")
echo "   https://${HOSTNAME_APP}/ -> ${code}"
case "$code" in
    200) echo "     (the app is up)" ;;
    502|503|504)
        echo "     EXPECTED: nothing listens on 127.0.0.1:8504 yet, so the"
        echo "     maintenance page is served. TLS and the vhost are working." ;;
    *)  echo "     unexpected -- check /var/log/nginx/pbp.error.log" ;;
esac

code=$(curl -s -o /dev/null -w '%{http_code}' "http://${HOSTNAME_APP}/")
echo "   http redirect -> ${code} (expect 301)"

# The certificate actually presented, not the one on disk: a vhost that failed to
# match server_name is answered by the default server with ITS certificate, and
# that is the failure this catches.
echo -n "   certificate presented for ${HOSTNAME_APP}: "
echo | openssl s_client -connect "${HOSTNAME_APP}:443" -servername "${HOSTNAME_APP}" 2>/dev/null \
    | openssl x509 -noout -subject 2>/dev/null | sed 's/^subject=//' || echo "could not read"
