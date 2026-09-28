"""Where this application is mounted, and the two things that follow from it.

Locally the app owns the whole origin: `python -m web.app` serves it on
127.0.0.1:8060 and every route hangs off `/`. On the EC2 box it is mounted at
`/plays/` under `pbp.dustincremascoli.com`, whose root is already taken by the
Streamlit text-to-SQL agent. So in production every address this app emits, and
every address it reads back, carries a prefix that is not part of the route.

Dash handles one half of that by itself. `requests_pathname_prefix` puts the
prefix in front of the URLs *Dash* generates -- the asset bundles under
`assets/`, the `_dash-update-component` callback endpoint, the dependency
manifest -- and none of that needs anything from this module.

It does NOT touch the two halves that are this application's own work:

  * the hrefs the pages build by hand (`/team/nfl/2`, `/player/cfb/3124679`).
    Dash never sees those strings; they are handed to `dcc.Link` and
    `dmc.Anchor` and go straight out to the browser.
  * `dcc.Location.pathname`, which is the browser's idea of the current address
    and therefore arrives at the router WITH the prefix still on it.

`url()` and `strip()` are inverses and cover exactly those two. The router calls
`strip()` before it splits on "/", so every line of parsing below it goes on
seeing `/team/nfl/2` and never learns the app is mounted anywhere in particular.

`PBP_WEB_PREFIX` is read ONCE, at import, and that is deliberate rather than
lazy. Dash bakes `requests_pathname_prefix` into the served HTML when the app
object is constructed, so a mount point that could change later would already be
a lie in a page someone has open. Moving the app is a restart.
"""
from __future__ import annotations

import os


def _normalise(raw: str | None) -> str:
    """Anything a human might reasonably type, reduced to the one form that concatenates.

    `plays`, `/plays`, `plays/` and `/plays/` all mean the same mount and all come
    back as `/plays/`. Exactly one leading and one trailing slash means `PREFIX +
    path.lstrip("/")` is correct with no special case for the root, where PREFIX
    is the single slash and the two functions below are both identity.
    """
    inner = (raw or "").strip().strip("/")
    return f"/{inner}/" if inner else "/"


PREFIX = _normalise(os.environ.get("PBP_WEB_PREFIX"))

# What Dash is constructed with. Kept as a separate name because the two are the
# same value for a different reason: PREFIX is where the pages think they are,
# this is what Dash stamps into the HTML. nginx proxies WITHOUT stripping the
# prefix -- see web/deploy/pbp-web.location.conf -- so the routes Flask registers
# and the URLs the browser requests are the same strings, and both carry it.
BASE = PREFIX


def url(path: str) -> str:
    """An in-app route -> the address to give the browser for it.

    >>> url("/team/nfl/2")   # with PBP_WEB_PREFIX=/plays
    '/plays/team/nfl/2'
    """
    return PREFIX + path.lstrip("/")


def strip(pathname: str | None) -> str:
    """The browser's address -> the in-app route, with the mount point removed.

    The two-branch test is not redundant. Matching on the bare `/plays` would also
    match `/playsomething` and hand the router `omething`, turning a 404 into a
    wrong page; matching only on `/plays/` would miss the bare mount point, which
    is what a visitor gets if they type the URL without the trailing slash.
    """
    path = pathname or "/"
    if PREFIX == "/":
        return path
    if path == PREFIX.rstrip("/"):
        return "/"
    if path.startswith(PREFIX):
        return "/" + path[len(PREFIX):]
    return path
