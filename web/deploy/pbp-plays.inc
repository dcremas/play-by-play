# The Dash instance explorer, mounted at /plays/ on pbp.dustincremascoli.com.
#
# Install to /etc/nginx/pbp-plays.inc and include it from INSIDE the 443 server
# block in pbp.conf:
#
#     include /etc/nginx/pbp-plays.inc;
#
# That one line is the entire change to pbp.conf. Everything else this app needs
# from nginx is in this file or in conf.d/pbp-web.conf.
#
# ------------------------------------------------------------------------------
# THE PREFIX IS NOT STRIPPED, AND THAT IS THE WHOLE CONTRACT
# ------------------------------------------------------------------------------
# `proxy_pass http://pbp_web;` with NO trailing URI passes the request path
# through unchanged, so gunicorn sees /plays/... exactly as the browser sent it.
# That matches the app, which is constructed with url_base_pathname="/plays/" and
# therefore REGISTERS its Flask routes under that prefix -- see web/routes.py.
#
# Writing `proxy_pass http://pbp_web/;` instead -- one trailing slash -- makes
# nginx strip /plays/ and the app 404s every request, because nothing is
# registered at the root. The failure is total and immediate, which is the good
# case. The bad case is "fixing" it by ALSO setting routes_pathname_prefix="/" :
# the pages then render, but every asset and callback URL Dash generates still
# carries /plays/ while the routes behind them do not, and the page half-loads
# with no error that says why. Leave both halves alone.
#
# ------------------------------------------------------------------------------
# NO WEBSOCKET, UNLIKE THE STREAMLIT HALF ABOVE
# ------------------------------------------------------------------------------
# Dash is a request/response app: callbacks are POSTs to
# /plays/_dash-update-component. There is no Upgrade handshake to forward and no
# long-lived socket to keep open, so this does NOT reuse proxy_params_pbp.inc --
# that file hard-codes `proxy_pass http://pbp_app` (the wrong upstream) and sets
# hour-long timeouts for a socket this app never opens.

# Dash's own static bundles: the React runtime, the component libraries, and
# anything in the app's assets/ directory. Same chunks on every load, cheap to
# serve, and the burst that would otherwise eat the rate limit on first paint.
# Not limited, and not logged, for the same reasons as the Streamlit /static/.
# A LONGER prefix than the `^~ /plays/_dash-` block below, so this one wins for
# the suites -- nginx picks the longest matching prefix, not the first in the
# file. That is deliberate: these want `access_log off` and a year of caching,
# neither of which is right for the callback endpoint.
location ^~ /plays/_dash-component-suites/ {
    include /etc/nginx/proxy_params_pbp_web.inc;
    access_log off;
    # Dash fingerprints these filenames with the package version and build mtime
    # (…react@18.v4_4_1m1790607385.3.1.min.js), so a given URL really is
    # immutable and a year is honest. Without this every navigation re-fetches
    # ~4 MB of JavaScript.
    #
    # proxy_hide_header FIRST, and it is what makes this one header instead of
    # two. Dash sets its own `Cache-Control: max-age=31536000` on these, and
    # nginx's add_header APPENDS rather than replaces -- so without the hide the
    # response carries the directive twice:
    #
    #     cache-control: max-age=31536000
    #     cache-control: public, max-age=31536000, immutable
    #
    # Browsers pick one, and which one is not something to rely on: `immutable`
    # was silently not being applied to the max-age it qualifies. Dropping the
    # add_header instead would also give one header, but loses `immutable`, which
    # is the directive that stops a reload revalidating ~4 MB of JavaScript.
    proxy_hide_header Cache-Control;
    add_header Cache-Control "public, max-age=31536000, immutable" always;
}

location ^~ /plays/assets/ {
    include /etc/nginx/proxy_params_pbp_web.inc;
    access_log off;
    # NOT immutable, unlike the suites above: app.css and resize.js are this
    # repo's own files and they change when the app changes. Dash appends a
    # ?m=<mtime> cache-buster to the URLs it generates for them, so a short
    # max-age is enough and a stale copy cannot outlive a deploy.
    #
    # Dash sends `no-cache` here, which would revalidate app.css and resize.js on
    # every navigation. The ?m= buster already guarantees freshness across a
    # deploy, so five minutes is both safe and strictly better. Hidden and
    # replaced rather than appended, for the same reason as above.
    proxy_hide_header Cache-Control;
    add_header Cache-Control "public, max-age=300" always;
}

# Dash's machinery: the callback endpoint every interaction lands on, plus the
# layout and dependency manifests fetched once per page load.
#
# ------------------------------------------------------------------------------
# THIS BLOCK EXISTS TO KEEP THESE OUT OF THE ANALYTICS PIPELINE
# ------------------------------------------------------------------------------
# Every vhost on this box writes a JSON access line that weblog-shipper tails into
# weblog.page_views -- capture happens at the proxy, so a new vhost is captured
# with no application code. That is the right default and it is wrong here.
#
# A Dash callback is an ordinary HTTP POST. One person filtering and scrolling for
# a few minutes generates HUNDREDS of them: a single session during deployment
# produced 121 hits on /plays/_dash-update-component against 11 real page loads.
# The shipper cannot filter them out either -- enrich.py's `is_asset()` keys off
# file extensions and the /static/ and /favicon prefixes, and none of these has an
# extension or matches a prefix it knows. So every one would land in page_views as
# a page view, and this one app would swamp the analytics for the whole estate.
#
# The Streamlit half of this hostname has no such problem, and not by design: its
# interactions travel over a WebSocket and generate no HTTP requests at all.
#
# A SINGLE PLAIN access_log IS THE SUPPRESSION MECHANISM. access_log at location
# level REPLACES the inherited set rather than adding to it, so naming only the
# plain log here drops the weblog_json line the server block would otherwise
# apply. This is the same technique analytics.conf uses to keep the analytics
# dashboard out of its own numbers. Debugging is unaffected -- the plain log still
# has every request.
location ^~ /plays/_dash- {
    access_log /var/log/nginx/pbp.access.log;

    # A generous burst, because one visitor exploring normally IS a burst -- see
    # the note on the zone in conf.d/pbp-web.conf. nodelay so the burst is served
    # at once rather than queued, which is the difference between a responsive
    # grid and one that feels broken.
    limit_req zone=pbp_web_all burst=200 nodelay;
    limit_req_status 429;

    # The 120s read timeout this endpoint needs is set in the include, for every
    # location, because an override here is not possible: `include` splices at
    # the including level, so a second proxy_read_timeout in this block is a
    # duplicate-directive error that fails `nginx -t` outright. See the note in
    # proxy_params_pbp_web.inc.
    include /etc/nginx/proxy_params_pbp_web.inc;
}

# Everything else under the mount: the index page, the profile routes
# (/plays/team/nfl/2), the layout and dependency manifests, the favicon.
#
# `^~` matters. Without it this is an ordinary prefix match that competes with
# the vhost's `location /`, and the Streamlit block would win for any request
# that also matched a regex location. With it, nginx stops at this prefix and
# never considers the others -- which is what keeps the two apps on one hostname
# from interleaving.
location ^~ /plays/ {
    limit_req zone=pbp_web_all burst=200 nodelay;
    limit_req_status 429;
    include /etc/nginx/proxy_params_pbp_web.inc;
}

# The mount point without its trailing slash. A person handed "…/plays" should
# land on the app, not on the Streamlit agent at /. gunicorn would answer this
# with its own 308 to /plays/, but only after the request has been routed here --
# and `location = /plays` is what routes it, since `^~ /plays/` does not match a
# path with no trailing slash. Answering it in nginx saves the round trip and,
# more importantly, does not depend on the app being up.
location = /plays {
    return 308 /plays/;
}
