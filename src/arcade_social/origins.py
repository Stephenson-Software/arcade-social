# @author Daniel McCoy Stephenson
"""Which page is calling: the request's Origin decides it (RFC 0013 §2).

The API has no slug parameter that a game's page chooses. A browser sets
Origin and page script cannot forge it, so a page at
https://kreatures.play.danielstephenson.dev acts for kreatures and for nothing
else, although it holds the same cookie as every other game.

    https://<slug>.play.danielstephenson.dev   -> Caller(GAME, slug)
    https://<alias>  (registry `aliases`)      -> Caller(GAME, slug)
    https://danielstephenson.dev  (portal)     -> Caller(PORTAL)
    the service's own origin                   -> Caller(SERVICE)
    anything else, or no Origin                -> None
"""

import re

GAME = "game"
PORTAL = "portal"
SERVICE = "service"

# A serialised origin as browsers send it: scheme://host[:port], lowercase, no
# path. Anything else - a trailing slash, a path, userinfo, "null" - is refused.
_ORIGIN = re.compile(r"^(https?)://([a-z0-9.-]+)(:[0-9]{1,5})?$")


class Caller(object):
    __slots__ = ("kind", "slug", "origin")

    def __init__(self, kind, origin, slug=None):
        self.kind = kind
        self.origin = origin
        self.slug = slug

    def __repr__(self):
        return "Caller(%s, %r, %r)" % (self.kind, self.origin, self.slug)


def classify(origin, registry, gameDomain, portalOrigin, serviceOrigin):
    """The Caller an Origin header value stands for, or None if it is not allowed."""
    if not origin:
        return None
    if origin == serviceOrigin:
        return Caller(SERVICE, origin)
    if origin == portalOrigin:
        return Caller(PORTAL, origin)
    match = _ORIGIN.match(origin)
    # Games are served over https on the default port only.
    if not match or match.group(1) != "https" or match.group(3):
        return None
    host = match.group(2)
    suffix = "." + gameDomain
    if host.endswith(suffix):
        label = host[: -len(suffix)]
        if "." in label:
            return None
        game = registry.get(label)
    else:
        game = registry.forAlias(host)
    if game is None:
        return None
    return Caller(GAME, origin, game.slug)


def returnOrigins(registry, gameDomain, portalOrigin):
    """Every origin the sign-in page may send a player back to, as CSP sources."""
    sources = [portalOrigin, "https://*." + gameDomain]
    for game in registry:
        sources.extend("https://" + alias for alias in game.aliases)
    return sources
