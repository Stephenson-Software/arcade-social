# @author Daniel McCoy Stephenson
"""Which page is calling: the request's Origin decides it (RFC 0013 §2).

The API has no slug parameter that a game's page chooses. A browser sets
Origin and page script cannot forge it, so a page at
https://kreatures.play.danielstephenson.dev acts for kreatures and for nothing
else, although it holds the same cookie as every other game.

    https://<slug>.play.danielstephenson.dev   -> Caller(GAME, slug)
    https://<alias>  (registry `aliases`)      -> Caller(GAME, slug)
    https://danielstephenson.dev  (portal)     -> Caller(PORTAL)
    a hall origin (ARCADE_SOCIAL_HALL_ORIGINS) -> Caller(HALL)
    the service's own origin                   -> Caller(SERVICE)
    anything else, or no Origin                -> None

The hall (the walkable arcade, https://play.danielstephenson.dev/hall/ and
https://arcade.danielstephenson.dev) is portal-class: it may read the
signed-in session and likes and like any registry game, but it is not a game,
so it can never submit a score, unlock an achievement or touch cloud saves.
The bare game domain is a hall origin only when configured as one; it is never
a slug host, because a slug is a non-empty label.
"""

import re

GAME = "game"
PORTAL = "portal"
HALL = "hall"
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


def classify(origin, registry, gameDomain, portalOrigin, serviceOrigin, hallOrigins=()):
    """The Caller an Origin header value stands for, or None if it is not allowed."""
    if not origin:
        return None
    if origin == serviceOrigin:
        return Caller(SERVICE, origin)
    if origin == portalOrigin:
        return Caller(PORTAL, origin)
    if origin in hallOrigins:
        return Caller(HALL, origin)
    match = _ORIGIN.match(origin)
    # Games are served over https on the default port only.
    if not match or match.group(1) != "https" or match.group(3):
        return None
    host = match.group(2)
    if host == gameDomain:
        # The bare game domain is no game: only a configured hall origin above.
        return None
    suffix = "." + gameDomain
    if host.endswith(suffix):
        label = host[: -len(suffix)]
        if not label or "." in label:
            return None
        game = registry.get(label)
    else:
        game = registry.forAlias(host)
    if game is None:
        return None
    return Caller(GAME, origin, game.slug)


def returnOrigins(registry, gameDomain, portalOrigin, hallOrigins=()):
    """Every origin the sign-in page may send a player back to, as CSP sources."""
    sources = [portalOrigin, "https://*." + gameDomain]
    sources.extend(hallOrigins)
    for game in registry:
        sources.extend("https://" + alias for alias in game.aliases)
    return sources
