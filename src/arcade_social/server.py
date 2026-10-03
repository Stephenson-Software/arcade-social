# @author Daniel McCoy Stephenson
"""The arcade-social HTTP server.

One host, api.play.danielstephenson.dev, answers two kinds of request:

  pages  /signin /register /welcome /account ... - HTML on the service's own
         origin; the only place a password is typed (RFC 0013 §1)
  API    /v1/...  - JSON, called with credentialed CORS by the portal and by
         game pages (RFC 0013 §1-§2, RFC 0014 §3, RFC 0015 §2)

Sign-in proxies to UserAuth and keeps its tokens in two host-only cookies,
`__Host-play_at` (access JWT) and `__Host-play_rt` (refresh token), both
Secure, HttpOnly, SameSite=Lax, Path=/, no Domain. No page script can read
them, and no other host - no game, not the portal - is ever sent them.

Who may do what is decided by the request's Origin (origins.py), never by a
slug a page sends. Writes made with the cookie must also carry
`Content-Type: application/json` and `X-Play-Client: 1`, which forces a CORS
preflight, and an allowed Origin; a write without Origin is refused. The
sign-in forms carry their own CSRF token (double-submit, `__Host-play_csrf`)
and must come from the service's own origin.
"""

import hmac
import http.server
import json
import math
import re
import secrets
import threading
import time
import traceback
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse

from arcade_social import __version__, config as configModule, displaynames, origins, pages, ratelimit
from arcade_social.store import NameHeld, NameTaken, NameTooSoon, Store
from arcade_social.userauth import Busy, Conflict, Invalid, Rejected, Unavailable, UserAuthClient

ACCESS_COOKIE = "__Host-play_at"
REFRESH_COOKIE = "__Host-play_rt"
CSRF_COOKIE = "__Host-play_csrf"
CLIENT_HEADER = "X-Play-Client"
MAX_JSON_BYTES = 16 * 1024
MAX_FORM_BYTES = 8 * 1024
MAX_SAFE_INTEGER = 2**53 - 1
NOT_VERIFIED = "Scores are reported by players' browsers and are not verified."
_COOKIE_VALUE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,4096}$")
_SLUG = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_ID = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_RUN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_USERNAME = re.compile(r"^[^/\s]{1,50}$")

# Route access levels.
PUBLIC = "public"  # anyone; Access-Control-Allow-Origin: * (or the allowed origin, credentialed)
PRIVATE = "private"  # an allowed origin; reads the signed-in player's own data
WRITE = "write"  # an allowed origin + JSON + X-Play-Client: changes state
ADMIN_READ = "admin-read"  # an operator; never CORS
ADMIN_WRITE = "admin-write"  # an operator, from the service's own origin + JSON + X-Play-Client

ROUTES = (
    ("GET", r"^/v1/session$", "apiSession", PRIVATE),
    ("POST", r"^/v1/signout$", "apiSignOut", WRITE),
    ("POST", r"^/v1/scores/(?P<board>[^/]+)$", "apiScore", WRITE),
    ("POST", r"^/v1/achievements/(?P<achievement>[^/]+)$", "apiUnlock", WRITE),
    ("GET", r"^/v1/games/(?P<slug>[^/]+)/boards$", "apiBoards", PUBLIC),
    ("GET", r"^/v1/games/(?P<slug>[^/]+)/achievements$", "apiAchievements", PUBLIC),
    ("GET", r"^/v1/boards/(?P<slug>[^/]+)/(?P<board>[^/]+)$", "apiTop", PUBLIC),
    ("GET", r"^/v1/boards/(?P<slug>[^/]+)/(?P<board>[^/]+)/around-me$", "apiAround", PRIVATE),
    ("GET", r"^/v1/me/(?P<slug>[^/]+)$", "apiMe", PRIVATE),
    ("DELETE", r"^/v1/me/(?P<slug>[^/]+)$", "apiDeleteGame", WRITE),
    ("DELETE", r"^/v1/me$", "apiDeleteAll", WRITE),
    ("GET", r"^/v1/likes/counts$", "apiLikeCounts", PUBLIC),
    ("GET", r"^/v1/likes/me$", "apiMyLikes", PRIVATE),
    ("POST", r"^/v1/likes/(?P<slug>[^/]+)$", "apiLike", WRITE),
    ("DELETE", r"^/v1/likes/(?P<slug>[^/]+)$", "apiUnlike", WRITE),
    ("GET", r"^/v1/admin/entries/(?P<entry>[0-9]{1,18})$", "adminEntry", ADMIN_READ),
    ("DELETE", r"^/v1/admin/entries/(?P<entry>[0-9]{1,18})$", "adminDeleteEntry", ADMIN_WRITE),
    ("GET", r"^/v1/admin/log/(?P<slug>[^/]+)/(?P<board>[^/]+)$", "adminLog", ADMIN_READ),
    ("GET", r"^/v1/admin/exclusions$", "adminExclusions", ADMIN_READ),
    ("POST", r"^/v1/admin/exclude/(?P<username>[^/]+)$", "adminExclude", ADMIN_WRITE),
    ("DELETE", r"^/v1/admin/exclude/(?P<username>[^/]+)$", "adminUnexclude", ADMIN_WRITE),
    ("DELETE", r"^/v1/admin/likes/(?P<username>[^/]+)$", "adminDeleteLikes", ADMIN_WRITE),
)
_COMPILED = tuple((method, re.compile(pattern), name, access) for method, pattern, name, access in ROUTES)

log = configModule.log


class ApiError(Exception):
    def __init__(self, status, message, headers=(), **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = tuple(headers)
        self.extra = extra


class Social(object):
    """The state one server process shares across requests."""

    def __init__(self, config, store=None, userauth=None, limiter=None, clock=time.time):
        self.config = config
        self.registryHolder = configModule.registryHolder(config)
        self.boardsHolder = configModule.boardsHolder(config)
        self.store = store if store is not None else Store(config.databasePath)
        self.userauth = (
            userauth
            if userauth is not None
            else UserAuthClient(
                config.userauthUrl,
                timeout=config.userauthTimeout,
                cacheSeconds=config.validateCacheSeconds,
                forwardClientIp=config.forwardClientIp,
            )
        )
        self.limiter = limiter if limiter is not None else ratelimit.RateLimiter(clock)
        self.clock = clock

    @property
    def registry(self):
        return self.registryHolder.value

    @property
    def boards(self):
        return self.boardsHolder.value

    def caller(self, origin):
        config = self.config
        return origins.classify(origin, self.registry, config.gameDomain, config.portalOrigin, config.serviceOrigin)

    def safeReturn(self, raw):
        """The URL to send a player back to after signing in: an https URL on
        the portal, a game or this service, or the default. Never anything
        else, so the sign-in page cannot be used as an open redirect."""
        default = self.config.defaultReturn
        if not raw or len(raw) > 2048 or any(character in raw for character in "\\\r\n\t "):
            return default
        try:
            parsed = urlparse(raw)
            port = parsed.port
        except ValueError:
            return default
        if parsed.scheme != "https" or parsed.username or parsed.password or port is not None:
            return default
        host = parsed.hostname or ""
        if parsed.netloc != host or (parsed.path and not parsed.path.startswith("/")):
            return default
        if self.caller("https://" + host) is None:
            return default
        return urlunparse(("https", host, parsed.path or "/", "", parsed.query, parsed.fragment))

    def formTargets(self):
        return origins.returnOrigins(self.registry, self.config.gameDomain, self.config.portalOrigin)

    def isOperator(self, username):
        return username is not None and username in self.config.operators

    def maintain(self):
        removed = self.store.pruneLog(self.config.logRetentionDays)
        if removed:
            log("pruned %d score submission(s) older than %d days" % (removed, self.config.logRetentionDays))


def _iso(millis):
    if millis is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(millis / 1000.0))


def _day(millis):
    return time.strftime("%d %B %Y", time.gmtime(millis / 1000.0)).lstrip("0")


def _rejectConstant(name):
    raise ValueError("%s is not allowed" % name)


def makeHandler(social):
    config = social.config

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "arcade-social/" + __version__
        sys_version = ""
        protocol_version = "HTTP/1.1"
        # A client that stops sending mid-request frees its thread after this.
        timeout = 30

        # --- plumbing -------------------------------------------------------------

        def log_message(self, *args):
            pass  # no access log: it would hold IP addresses

        def do_GET(self):
            self._dispatch()

        def do_HEAD(self):
            self._dispatch()

        def do_POST(self):
            self._dispatch()

        def do_DELETE(self):
            self._dispatch()

        def do_PUT(self):
            self._dispatch()

        def do_PATCH(self):
            self._dispatch()

        def do_OPTIONS(self):
            self._dispatch()

        def _dispatch(self):
            # Reset per request: one handler serves a whole keep-alive connection.
            self._outCookies = []
            self._cors = []
            self._session = None
            self._sessionKnown = False
            self._bodyRead = False
            self._csrfToken = None
            try:
                path = unquote(urlparse(self.path).path)
                if path == "/healthz" and self.command in ("GET", "HEAD"):
                    self._send(200, b"ok\n", "text/plain; charset=utf-8")
                elif path.startswith("/v1/") or path == "/v1":
                    self._api(path)
                else:
                    self._pages(path)
            except ApiError as e:
                self._drain()
                payload = {"error": e.message}
                payload.update(e.extra)
                self._json(e.status, payload, headers=e.headers)
            except Unavailable as e:
                log("UserAuth unavailable: %s" % e)
                self._drain()
                if self.path.startswith("/v1"):
                    self._json(503, {"error": "sign-in is unavailable right now; try again shortly"})
                else:
                    self._html(503, pages.message("Sign-in is unavailable", "Please try again in a minute."))
            except Exception:
                log("unhandled error on %s %s:\n%s" % (self.command, self.path.split("?")[0], traceback.format_exc()))
                self.close_connection = True
                try:
                    self._json(500, {"error": "internal error"})
                except Exception:
                    pass

        def _send(self, status, body, contentType, headers=()):
            self.send_response(status)
            self.send_header("Content-Type", contentType)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in self._cors:
                self.send_header(name, value)
            for name, value in headers:
                self.send_header(name, value)
            for cookie in self._outCookies:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status, payload, headers=(), cache="no-store"):
            body = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
            self._send(status, body, "application/json", tuple(headers) + (("Cache-Control", cache),))

        def _html(self, status, body, headers=()):
            self._send(
                status,
                body,
                "text/html; charset=utf-8",
                tuple(headers)
                + (
                    ("Content-Security-Policy", pages.csp(social.formTargets())),
                    ("X-Frame-Options", "DENY"),
                    # same-origin, not no-referrer: under no-referrer a browser
                    # sends "Origin: null" with the page's own form POSTs, and
                    # the form check (which needs this origin) would refuse them.
                    ("Referrer-Policy", "same-origin"),
                    ("Cache-Control", "no-store"),
                ),
            )

        def _redirect(self, location, status=303):
            self._send(status, b"", "text/plain; charset=utf-8", (("Location", location), ("Cache-Control", "no-store")))

        def _query(self):
            return parse_qs(urlparse(self.path).query, keep_blank_values=True)

        def _param(self, name, default=None):
            values = self._query().get(name)
            return values[0] if values else default

        def _contentLength(self):
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                raise ApiError(411, "Content-Length is required")
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self.close_connection = True
                raise ApiError(400, "bad Content-Length")
            if length < 0:
                self.close_connection = True
                raise ApiError(400, "bad Content-Length")
            return length

        def _readBody(self, limit):
            length = self._contentLength()
            if length > limit:
                self.close_connection = True
                raise ApiError(413, "the body is larger than %d bytes" % limit)
            self._bodyRead = True
            return self.rfile.read(length) if length else b""

        def _drain(self):
            if self._bodyRead:
                return
            self._bodyRead = True
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self.close_connection = True
                return
            if 0 < length <= 64 * 1024:
                self.rfile.read(length)
            elif length:
                self.close_connection = True

        def _clientIp(self):
            if config.trustForwardedFor:
                forwarded = self.headers.get("X-Forwarded-For") or ""
                # The right-most entry is the one the trusted proxy (Traefik)
                # added; anything to its left is client-supplied.
                last = forwarded.split(",")[-1].strip()
                if last:
                    return last
            return self.client_address[0]

        def _limit(self, limit, key):
            wait = social.limiter.hit(limit, key)
            if wait:
                raise ApiError(429, "too many requests; try again later", headers=(("Retry-After", str(wait)),))

        # --- cookies and the session ---------------------------------------------------

        def _cookie(self, name):
            for part in (self.headers.get("Cookie") or "").split(";"):
                key, _, value = part.strip().partition("=")
                if key == name:
                    value = value.strip()
                    return value if _COOKIE_VALUE.match(value) else None
            return None

        def _setCookie(self, name, value, maxAge, sameSite="Lax"):
            # Host-only (no Domain), so only this host is ever sent it; the
            # __Host- prefix makes browsers enforce exactly that, plus Secure
            # and Path=/.
            cookie = "%s=%s; Path=/; Secure; HttpOnly; SameSite=%s" % (name, value, sameSite)
            if maxAge is not None:
                cookie += "; Max-Age=%d" % maxAge
            self._outCookies = [existing for existing in self._outCookies if not existing.startswith(name + "=")]
            self._outCookies.append(cookie)

        def _setAuthCookies(self, tokens):
            if not _COOKIE_VALUE.match(tokens.access) or not _COOKIE_VALUE.match(tokens.refresh):
                raise Unavailable("UserAuth returned a token that cannot be a cookie value")
            accessAge = max(1, int(tokens.expiresAt - social.clock()))
            self._setCookie(ACCESS_COOKIE, tokens.access, accessAge)
            self._setCookie(REFRESH_COOKIE, tokens.refresh, config.refreshDays * 86400)

        def _clearAuthCookies(self):
            self._setCookie(ACCESS_COOKIE, "", 0)
            self._setCookie(REFRESH_COOKIE, "", 0)

        def _signedIn(self):
            """The signed-in UserAuth username, or None. Refreshes an expired
            access token with the refresh cookie (setting new cookies on this
            response) and clears cookies UserAuth no longer accepts."""
            if self._sessionKnown:
                return self._session
            username = None
            access = self._cookie(ACCESS_COOKIE)
            refresh = self._cookie(REFRESH_COOKIE)
            if access:
                username = social.userauth.validate(access)
            if username is None and refresh:
                try:
                    tokens = social.userauth.refresh(refresh, clientIp=self._clientIp())
                    self._setAuthCookies(tokens)
                    username = tokens.username
                except (Rejected, Invalid):
                    self._clearAuthCookies()
                except Busy:
                    # UserAuth's shared limit: signed out for this request only.
                    pass
            elif username is None and access:
                self._clearAuthCookies()
            self._session = username
            self._sessionKnown = True
            return username

        def _requirePlayer(self, create=True):
            """(username, player row) for a signed-in caller, or a 401. With
            create=False (reads) the row may be None: a read never writes."""
            username = self._signedIn()
            if username is None:
                raise ApiError(401, "sign in required", signIn=config.publicUrl + "/signin")
            if not create:
                return username, social.store.player(username)
            return username, social.store.ensurePlayer(username)

        # --- API ------------------------------------------------------------------

        def _route(self, path):
            allowedMethods = []
            for method, pattern, name, access in _COMPILED:
                match = pattern.match(path)
                if match is None:
                    continue
                allowedMethods.append(method)
                if method == self.command or (method == "GET" and self.command == "HEAD"):
                    return name, access, match.groupdict(), allowedMethods
            return None, None, None, allowedMethods

        def _api(self, path):
            caller = social.caller(self.headers.get("Origin"))
            credentialed = caller is not None and caller.kind in (origins.GAME, origins.PORTAL)
            if self.command == "OPTIONS":
                self._preflight(path, caller, credentialed)
                return
            name, access, groups, allowedMethods = self._route(path)
            if name is None:
                if allowedMethods:
                    raise ApiError(405, "method not allowed", headers=(("Allow", ", ".join(sorted(set(allowedMethods)))),))
                raise ApiError(404, "not found")
            self._cors = [("Vary", "Origin")]
            if access not in (ADMIN_READ, ADMIN_WRITE):
                if credentialed:
                    self._cors += [
                        ("Access-Control-Allow-Origin", caller.origin),
                        ("Access-Control-Allow-Credentials", "true"),
                    ]
                elif access == PUBLIC:
                    self._cors.append(("Access-Control-Allow-Origin", "*"))
            if access in (PRIVATE, WRITE) and not credentialed:
                raise ApiError(403, "this origin may not use the signed-in API")
            if access in (WRITE, ADMIN_WRITE):
                self._limit(ratelimit.API_WRITES_PER_IP, self._clientIp())
                self._writeGuard(caller, admin=access == ADMIN_WRITE)
            if access in (ADMIN_READ, ADMIN_WRITE):
                origin = self.headers.get("Origin")
                if origin is not None and (caller is None or caller.kind != origins.SERVICE):
                    raise ApiError(403, "operator endpoints answer only to this service's own origin")
                username = self._signedIn()
                if not social.isOperator(username):
                    raise ApiError(403 if username else 401, "operators only")
            getattr(self, name)(caller, **groups)

        def _preflight(self, path, caller, credentialed):
            _, _, _, allowedMethods = self._route(path)
            if not allowedMethods:
                raise ApiError(404, "not found")
            requested = (self.headers.get("Access-Control-Request-Method") or "").upper()
            if not credentialed or requested not in allowedMethods or path.startswith("/v1/admin/"):
                # No CORS headers: the browser blocks the request.
                self._drain()
                self._send(403, b"", "text/plain; charset=utf-8", (("Vary", "Origin"),))
                return
            self._drain()
            self._send(
                204,
                b"",
                "text/plain; charset=utf-8",
                (
                    ("Access-Control-Allow-Origin", caller.origin),
                    ("Access-Control-Allow-Credentials", "true"),
                    ("Access-Control-Allow-Methods", ", ".join(sorted(set(allowedMethods)))),
                    ("Access-Control-Allow-Headers", "Content-Type, " + CLIENT_HEADER),
                    ("Access-Control-Max-Age", "600"),
                    ("Vary", "Origin"),
                ),
            )

        def _writeGuard(self, caller, admin):
            """CSRF for cookie-authenticated writes (RFC 0013 §1): an allowed
            Origin (checked by the caller of this), the JSON content type and
            the custom header - the last two cannot be sent cross-origin
            without a preflight, which only allowed origins pass."""
            if admin and (caller is None or caller.kind != origins.SERVICE):
                raise ApiError(403, "operator writes must come from this service's own origin")
            if self.headers.get(CLIENT_HEADER) != "1":
                raise ApiError(403, "the %s: 1 header is required" % CLIENT_HEADER)
            contentType = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if contentType != "application/json":
                raise ApiError(415, "Content-Type must be application/json")

        def _jsonBody(self, allowed):
            raw = self._readBody(MAX_JSON_BYTES)
            if not raw.strip():
                return {}
            try:
                payload = json.loads(raw.decode("utf-8"), parse_constant=_rejectConstant)
            except (ValueError, UnicodeError):
                raise ApiError(400, "the body is not valid JSON")
            if not isinstance(payload, dict):
                raise ApiError(400, "the body must be a JSON object")
            unknown = sorted(set(payload) - set(allowed))
            if unknown:
                raise ApiError(400, "unknown field(s): %s" % ", ".join(unknown))
            return payload

        def _gameCaller(self, caller, what):
            if caller.kind != origins.GAME:
                raise ApiError(403, "%s come only from the game's own page" % what)
            return caller.slug

        def _slugFor(self, caller, slug):
            """A slug named in the path: a game's page may name only its own."""
            if not _SLUG.match(slug):
                raise ApiError(404, "no such game")
            if caller.kind == origins.GAME and caller.slug != slug:
                raise ApiError(403, "a game's page may act only for its own game")
            return slug

        def _declarations(self, slug):
            if not _SLUG.match(slug):
                raise ApiError(404, "no such game")
            declarations = social.boards.get(slug)
            if declarations is None:
                raise ApiError(404, "%s declares no boards or achievements" % slug)
            return declarations

        def _listedDeclarations(self, slug):
            """For the public listings: a game in arcade's registry that
            declares nothing has empty lists (200), so a page can ask "does
            this game have boards?" without a 404 for the common answer "no".
            A slug arcade does not serve is still a 404."""
            if not _SLUG.match(slug):
                raise ApiError(404, "no such game")
            declarations = social.boards.get(slug)
            if declarations is None and social.registry.get(slug) is None:
                raise ApiError(404, "no such game")
            return declarations

        def _board(self, slug, boardId):
            board = self._declarations(slug).board(boardId) if _ID.match(boardId) else None
            if board is None:
                raise ApiError(404, "no such board")
            return board

        def _value(self, board, value):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ApiError(400, "value must be a number")
            if isinstance(value, float):
                if not math.isfinite(value):
                    raise ApiError(400, "value must be finite")
                if board.integer:
                    if not value.is_integer():
                        raise ApiError(400, "this board takes whole numbers")
                    value = int(value)
            if isinstance(value, int) and abs(value) > MAX_SAFE_INTEGER:
                # Beyond what a JavaScript number holds exactly, and beyond
                # what SQLite's INTEGER stores without overflow.
                raise ApiError(400, "value is out of range")
            return value

        def _notice(self, payload):
            payload["verified"] = False
            payload["notice"] = NOT_VERIFIED
            return payload

        # session

        def apiSession(self, caller):
            username = self._signedIn()
            player = social.store.player(username) if username else None
            self._json(
                200,
                {
                    "signedIn": username is not None,
                    "displayName": player["display_name"] if player is not None else None,
                    "needsDisplayName": username is not None and (player is None or player["display_name"] is None),
                    "signInUrl": config.publicUrl + "/signin",
                    "accountUrl": config.publicUrl + "/account",
                },
            )

        def apiSignOut(self, caller):
            self._jsonBody(())
            self._signOut()
            self._json(200, {"signedIn": False})

        def _signOut(self):
            access = self._cookie(ACCESS_COOKIE)
            refresh = self._cookie(REFRESH_COOKIE)
            if not access and refresh:
                # Revoke the session even when the access token has lapsed.
                try:
                    access = social.userauth.refresh(refresh, clientIp=self._clientIp()).access
                except (Rejected, Invalid, Busy, Unavailable):
                    access = None
            if access:
                social.userauth.logout(access)
            self._clearAuthCookies()
            self._session = None
            self._sessionKnown = True

        # scores and achievements (RFC 0014)

        def apiScore(self, caller, board):
            slug = self._gameCaller(caller, "scores")
            declared = self._board(slug, board)
            body = self._jsonBody(("value", "run"))
            if "value" not in body:
                raise ApiError(400, "value is required")
            value = self._value(declared, body["value"])
            run = body.get("run")
            if run is not None and (not isinstance(run, str) or not _RUN.match(run)):
                raise ApiError(400, "run must be 1-64 characters of letters, digits and . _ : -")
            username, player = self._requirePlayer()
            if player["display_name"] is None:
                raise ApiError(
                    409,
                    "choose a display name before your scores can go on a leaderboard",
                    welcome=config.publicUrl + "/welcome",
                )
            self._limit(ratelimit.API_WRITES_PER_PLAYER, player["id"])
            if value < declared.min or value > declared.max:
                social.store.logRefused(player["id"], slug, declared.id, value, run, "outside min..max")
                raise ApiError(422, "the value is outside this board's limits", min=declared.min, max=declared.max)
            result = social.store.submitScore(player["id"], slug, declared, value, run)
            if result is None:
                raise ApiError(
                    429,
                    "this board takes at most %d submissions per player per hour" % declared.maxPerHour,
                    headers=(("Retry-After", "600"),),
                )
            self._json(
                200,
                self._notice(
                    {
                        "slug": slug,
                        "board": declared.id,
                        "best": result["best"],
                        "improved": result["improved"],
                        "rank": result["rank"],
                    }
                ),
            )

        def apiUnlock(self, caller, achievement):
            slug = self._gameCaller(caller, "achievements")
            declared = self._declarations(slug).achievement(achievement) if _ID.match(achievement) else None
            if declared is None:
                raise ApiError(404, "no such achievement")
            self._jsonBody(())
            username, player = self._requirePlayer()
            self._limit(ratelimit.API_WRITES_PER_PLAYER, player["id"])
            self._limit(ratelimit.ACHIEVEMENTS_PER_PLAYER, (player["id"], slug))
            newly, unlockedAt = social.store.unlock(player["id"], slug, declared.id)
            self._json(
                200,
                self._notice(
                    {
                        "slug": slug,
                        "achievement": declared.id,
                        "unlocked": True,
                        "newlyUnlocked": newly,
                        "unlockedAt": _iso(unlockedAt),
                    }
                ),
            )

        def apiBoards(self, caller, slug):
            declarations = self._listedDeclarations(slug)
            boards = [board.describe() for board in declarations.listBoards()] if declarations else []
            self._json(
                200,
                self._notice({"slug": slug, "boards": boards}),
                cache="public, max-age=60",
            )

        def _entries(self, entries):
            for entry in entries:
                entry["achievedAt"] = _iso(entry["achievedAt"])
            return entries

        def _intParam(self, name, default, low, high):
            raw = self._param(name)
            if raw is None:
                return default
            if not raw.isdigit() or not low <= int(raw) <= high:
                raise ApiError(400, "%s must be a whole number from %d to %d" % (name, low, high))
            return int(raw)

        def apiTop(self, caller, slug, board):
            declared = self._board(slug, board)
            limit = self._intParam("limit", 10, 1, 100)
            entries, total = social.store.top(slug, declared, limit)
            self._json(
                200,
                self._notice(
                    {"slug": slug, "board": declared.describe(), "entries": self._entries(entries), "total": total}
                ),
                cache="public, max-age=15",
            )

        def apiAround(self, caller, slug, board):
            slug = self._slugFor(caller, slug)
            declared = self._board(slug, board)
            window = self._intParam("window", 5, 1, 25)
            username, player = self._requirePlayer(create=False)
            entries, mine = social.store.aroundPlayer(player["id"], slug, declared, window) if player else ([], None)
            self._json(
                200,
                self._notice(
                    {"slug": slug, "board": declared.describe(), "entries": self._entries(entries), "me": mine}
                ),
            )

        def apiAchievements(self, caller, slug):
            declarations = self._listedDeclarations(slug)
            players, counts = social.store.achievementShares(slug)
            listed = []
            for achievement in declarations.listAchievements() if declarations else []:
                count = counts.get(achievement.id, 0)
                listed.append(
                    {
                        "id": achievement.id,
                        "title": "Hidden achievement" if achievement.hidden else achievement.title,
                        "description": None if achievement.hidden else achievement.description,
                        "hidden": achievement.hidden,
                        "players": count,
                        "percent": round(100.0 * count / players, 1) if players else 0.0,
                    }
                )
            self._json(
                200,
                self._notice({"slug": slug, "players": players, "achievements": listed}),
                cache="public, max-age=60",
            )

        def apiMe(self, caller, slug):
            slug = self._slugFor(caller, slug)
            username, player = self._requirePlayer(create=False)
            declarations = social.boards.get(slug)
            unlocks = social.store.playerUnlocks(player["id"], slug) if player else []
            for unlock in unlocks:
                declared = declarations.achievement(unlock["achievement"]) if declarations else None
                unlock["title"] = declared.title if declared else unlock["achievement"]
                unlock["description"] = declared.description if declared else None
                unlock["unlockedAt"] = _iso(unlock["unlockedAt"])
            bests = social.store.playerBests(player["id"], slug, declarations) if player else []
            for best in bests:
                best["achievedAt"] = _iso(best["achievedAt"])
            self._json(
                200,
                self._notice(
                    {
                        "slug": slug,
                        "displayName": player["display_name"] if player else None,
                        "bests": bests,
                        "achievements": unlocks,
                    }
                ),
            )

        def apiDeleteGame(self, caller, slug):
            slug = self._slugFor(caller, slug)
            self._jsonBody(())
            username, player = self._requirePlayer(create=False)
            removed = social.store.deleteGameData(player["id"], slug) if player else 0
            self._json(200, {"slug": slug, "deleted": removed})

        def apiDeleteAll(self, caller):
            body = self._jsonBody(("confirm",))
            if body.get("confirm") != "delete":
                raise ApiError(400, 'send {"confirm": "delete"} to delete all of your data')
            username = self._signedIn()
            if username is None:
                raise ApiError(401, "sign in required", signIn=config.publicUrl + "/signin")
            deleted = social.store.deletePlayer(username)
            log("a player deleted their data")
            self._json(200, {"deleted": deleted})

        # likes (RFC 0015)

        def apiLike(self, caller, slug):
            slug = self._slugFor(caller, slug)
            if social.registry.get(slug) is None:
                raise ApiError(404, "no such game")
            self._jsonBody(())
            username, player = self._requirePlayer()
            self._limit(ratelimit.API_WRITES_PER_PLAYER, player["id"])
            self._json(200, {"slug": slug, "count": social.store.like(player["id"], slug), "liked": True})

        def apiUnlike(self, caller, slug):
            # No registry check: a like on a game that has left the registry
            # can still be removed (RFC 0015 §1).
            slug = self._slugFor(caller, slug)
            self._jsonBody(())
            username, player = self._requirePlayer(create=False)
            if player is not None:
                self._limit(ratelimit.API_WRITES_PER_PLAYER, player["id"])
                count = social.store.unlike(player["id"], slug)
            else:
                count = social.store.likeCounts().get(slug, 0)
            self._json(200, {"slug": slug, "count": count, "liked": False})

        def apiLikeCounts(self, caller):
            registry = social.registry
            counts = social.store.likeCounts()
            self._json(
                200,
                dict((slug, count) for slug, count in counts.items() if registry.get(slug) is not None),
                cache="public, max-age=15",
            )

        def apiMyLikes(self, caller):
            username = self._signedIn()
            if username is None:
                raise ApiError(401, "sign in required", signIn=config.publicUrl + "/signin")
            player = social.store.player(username)
            self._json(200, social.store.likedBy(player["id"]) if player is not None else [])

        # operator tools

        def _entryPayload(self, entry):
            entry = dict(entry)
            entry["achievedAt"] = _iso(entry.pop("achieved_at"))
            entry["displayName"] = entry.pop("display_name")
            return entry

        def adminEntry(self, caller, entry):
            found = social.store.entry(int(entry))
            if found is None:
                raise ApiError(404, "no such entry")
            self._json(200, self._entryPayload(found))

        def adminDeleteEntry(self, caller, entry):
            self._jsonBody(())
            if not social.store.deleteEntry(int(entry)):
                raise ApiError(404, "no such entry")
            log("operator removed leaderboard entry %s" % entry)
            self._json(200, {"deleted": int(entry)})

        def adminLog(self, caller, slug, board):
            limit = self._intParam("limit", 100, 1, 1000)
            rows = social.store.submissionLog(slug, board, limit)
            for row in rows:
                row["receivedAt"] = _iso(row.pop("received_at"))
                row["displayName"] = row.pop("display_name")
                row["accepted"] = bool(row["accepted"])
            self._json(200, {"slug": slug, "board": board, "submissions": rows})

        def adminExclusions(self, caller):
            rows = social.store.exclusions()
            for row in rows:
                row["createdAt"] = _iso(row.pop("created_at"))
            self._json(200, {"exclusions": rows})

        def _username(self, username):
            username = username.strip().lower()
            if not _USERNAME.match(username):
                raise ApiError(400, "not a username")
            return username

        def adminExclude(self, caller, username):
            username = self._username(username)
            body = self._jsonBody(("reason",))
            reason = body.get("reason")
            if reason is not None and (not isinstance(reason, str) or len(reason) > 200):
                raise ApiError(400, "reason must be a string of at most 200 characters")
            social.store.exclude(username, reason)
            log("operator excluded an account from leaderboards")
            self._json(200, {"excluded": username})

        def adminUnexclude(self, caller, username):
            username = self._username(username)
            self._jsonBody(())
            if not social.store.unexclude(username):
                raise ApiError(404, "that account is not excluded")
            self._json(200, {"unexcluded": username})

        def adminDeleteLikes(self, caller, username):
            username = self._username(username)
            self._jsonBody(())
            removed = social.store.deleteLikesOf(username)
            log("operator removed %d like(s) from one account" % removed)
            self._json(200, {"deleted": removed})

        # --- pages ------------------------------------------------------------------

        def _csrf(self):
            """The form CSRF token: the existing cookie, or a new one set now."""
            if self._csrfToken is None:
                token = self._cookie(CSRF_COOKIE)
                if not token or len(token) < 32:
                    token = secrets.token_urlsafe(32)
                    self._setCookie(CSRF_COOKIE, token, None, sameSite="Strict")
                self._csrfToken = token
            return self._csrfToken

        def _form(self):
            """A POSTed form, after the CSRF checks: the Origin must be this
            service's own and the form's token must match the cookie."""
            if self.headers.get("Origin") != config.serviceOrigin:
                self._drain()
                raise _PageError(403, "This form must be sent from %s." % config.serviceOrigin)
            contentType = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if contentType != "application/x-www-form-urlencoded":
                self._drain()
                raise _PageError(415, "Unsupported form encoding.")
            try:
                raw = self._readBody(MAX_FORM_BYTES).decode("utf-8")
            except UnicodeError:
                raise _PageError(400, "The form could not be read.")
            fields = dict((key, values[0]) for key, values in parse_qs(raw, keep_blank_values=True).items())
            cookie = self._cookie(CSRF_COOKIE)
            token = fields.get("csrf", "")
            if not cookie or not token or not hmac.compare_digest(cookie.encode("utf-8"), token.encode("utf-8")):
                raise _PageError(403, "This form has expired. Go back, reload the page and try again.")
            return fields

        def _pages(self, path):
            try:
                self._page(path)
            except _PageError as e:
                self._html(e.status, pages.message("Something went wrong", e.message, "/signin", "Sign in"))
            except ApiError as e:
                if e.status == 429:
                    self._html(e.status, pages.message("Please wait", "Too many attempts. Try again later."), e.headers)
                else:
                    self._html(e.status, pages.message("Something went wrong", e.message), e.headers)

        def _page(self, path):
            method = "GET" if self.command == "HEAD" else self.command
            route = {
                ("GET", "/"): self.pageRoot,
                ("GET", "/signin"): self.pageSignIn,
                ("POST", "/signin"): self.postSignIn,
                ("GET", "/register"): self.pageRegister,
                ("POST", "/register"): self.postRegister,
                ("GET", "/welcome"): self.pageWelcome,
                ("POST", "/welcome"): self.postWelcome,
                ("GET", "/account"): self.pageAccount,
                ("GET", "/account/name"): self.pageChangeName,
                ("POST", "/account/name"): self.postChangeName,
                ("GET", "/account/export"): self.pageExport,
                ("POST", "/account/delete"): self.postDelete,
                ("POST", "/signout"): self.postSignOut,
            }.get((method, path))
            if route is None:
                self._drain()
                self._html(404, pages.message("Not found", "There is nothing here.", "/account", "Your account"))
                return
            route()

        def _return(self, fields=None):
            raw = fields.get("return") if fields is not None else self._param("return")
            return social.safeReturn(raw)

        def _withReturn(self, path, returnUrl):
            return "%s?%s" % (path, urlencode({"return": returnUrl}))

        def pageRoot(self):
            self._redirect("/account", 302)

        def pageSignIn(self, error=None, notice=None, username="", status=200):
            returnUrl = self._return()
            self._html(
                status,
                pages.signIn(self._csrf(), returnUrl, self._withReturn("/register", returnUrl), error, notice, username),
            )

        def _afterSignIn(self, tokens, returnUrl):
            self._setAuthCookies(tokens)
            player = social.store.ensurePlayer(tokens.username)
            if player["display_name"] is None:
                self._redirect(self._withReturn("/welcome", returnUrl))
            else:
                self._redirect(returnUrl)

        def postSignIn(self):
            fields = self._form()
            returnUrl = self._return(fields)
            username = fields.get("username", "").strip()
            password = fields.get("password", "")
            csrf = self._csrf()
            registerUrl = self._withReturn("/register", returnUrl)
            if not username or not password or len(username) > 50 or len(password) > 1024:
                self._html(400, pages.signIn(csrf, returnUrl, registerUrl, "Enter your username and password.", None, username[:50]))
                return
            for limit, key in (
                (ratelimit.SIGNIN_PER_IP, self._clientIp()),
                (ratelimit.SIGNIN_PER_USERNAME, username.lower()),
            ):
                wait = social.limiter.hit(limit, key)
                if wait:
                    self._html(
                        429,
                        pages.signIn(csrf, returnUrl, registerUrl, "Too many attempts. Try again in a few minutes.", None, username),
                        (("Retry-After", str(wait)),),
                    )
                    return
            try:
                tokens = social.userauth.login(username, password, clientIp=self._clientIp())
            except (Rejected, Invalid):
                self._html(401, pages.signIn(csrf, returnUrl, registerUrl, "Wrong username or password.", None, username))
                return
            except Busy as e:
                self._html(
                    429,
                    pages.signIn(csrf, returnUrl, registerUrl, "Sign-in is busy. Try again in a minute.", None, username),
                    (("Retry-After", str(e.retryAfter or 60)),),
                )
                return
            self._afterSignIn(tokens, returnUrl)

        def pageRegister(self, error=None, username="", status=200):
            returnUrl = self._return()
            self._html(status, pages.register(self._csrf(), returnUrl, self._withReturn("/signin", returnUrl), error, username))

        def postRegister(self):
            fields = self._form()
            returnUrl = self._return(fields)
            username = fields.get("username", "").strip()
            password = fields.get("password", "")
            csrf = self._csrf()
            signInUrl = self._withReturn("/signin", returnUrl)

            def again(status, error, headers=()):
                self._html(status, pages.register(csrf, returnUrl, signInUrl, error, username[:50]), headers)

            if password != fields.get("password2", ""):
                again(400, "The two passwords do not match.")
                return
            if not 3 <= len(username) <= 50 or not 8 <= len(password) <= 72:
                again(400, "A username is 3 to 50 characters; a password 8 to 72.")
                return
            wait = social.limiter.hit(ratelimit.REGISTER_PER_IP, self._clientIp())
            if wait:
                again(429, "Too many new accounts from here. Try again later.", (("Retry-After", str(wait)),))
                return
            try:
                social.userauth.register(username, password, clientIp=self._clientIp())
            except Conflict:
                again(409, "That username is taken.")
                return
            except Invalid as e:
                again(400, e.message)
                return
            except Busy as e:
                again(429, "Sign-up is busy. Try again in a minute.", (("Retry-After", str(e.retryAfter or 60)),))
                return
            try:
                tokens = social.userauth.login(username, password, clientIp=self._clientIp())
            except (Rejected, Invalid, Busy):
                self._redirect(self._withReturn("/signin", returnUrl))
                return
            self._afterSignIn(tokens, returnUrl)

        def _pageSession(self):
            """(username, player) for a page that needs a signed-in player, or
            None after redirecting to the sign-in page."""
            username = self._signedIn()
            if username is None:
                self._drain()
                self._redirect(self._withReturn("/signin", config.publicUrl + self.path.split("?")[0]), 303)
                return None
            return username, social.store.ensurePlayer(username)

        def pageWelcome(self):
            session = self._pageSession()
            if session is None:
                return
            username, player = session
            returnUrl = self._return()
            if player["display_name"] is not None:
                self._redirect(returnUrl)
                return
            self._html(200, pages.chooseName(self._csrf(), returnUrl))

        def _saveName(self, username, fields, returnUrl, current):
            name = fields.get("name", "")
            try:
                checked = displaynames.check(name, operator=social.isOperator(username))
                social.store.setDisplayName(
                    username, checked, displaynames.nameKey(checked), config.nameChangeDays, config.nameHoldDays
                )
            except displaynames.InvalidName as e:
                return str(e), name
            except (NameTaken, NameHeld):
                return "That display name is taken; please choose another.", name
            except NameTooSoon as e:
                return "You can next change your display name on %s." % _day(e.nextChangeAt), name
            return None, name

        def postWelcome(self):
            fields = self._form()
            session = self._pageSession()
            if session is None:
                return
            username, player = session
            returnUrl = self._return(fields)
            if player["display_name"] is not None:
                self._redirect(returnUrl)
                return
            error, name = self._saveName(username, fields, returnUrl, None)
            if error:
                self._html(400, pages.chooseName(self._csrf(), returnUrl, error, name))
                return
            self._redirect(returnUrl)

        def _nextChange(self, player):
            if player["display_name"] is None or player["name_changed_at"] is None:
                return None
            nextChange = player["name_changed_at"] + config.nameChangeDays * 86400000
            return _day(nextChange) if social.store.clock() < nextChange else None

        def pageAccount(self, error=None, notice=None, status=200):
            session = self._pageSession()
            if session is None:
                return
            username, player = session
            if player["display_name"] is None and error is None and notice is None:
                self._redirect(self._withReturn("/welcome", config.publicUrl + "/account"))
                return
            self._html(
                status,
                pages.account(
                    self._csrf(),
                    username,
                    player["display_name"],
                    social.store.summary(username),
                    self._return(),
                    error,
                    notice,
                    self._nextChange(player),
                ),
            )

        def pageChangeName(self):
            session = self._pageSession()
            if session is None:
                return
            username, player = session
            if player["display_name"] is None:
                self._redirect(self._withReturn("/welcome", config.publicUrl + "/account"))
                return
            self._html(
                200,
                pages.chooseName(
                    self._csrf(), config.publicUrl + "/account", None, "", player["display_name"], self._nextChange(player)
                ),
            )

        def postChangeName(self):
            fields = self._form()
            session = self._pageSession()
            if session is None:
                return
            username, player = session
            self._limit(ratelimit.ACCOUNT_FORMS_PER_PLAYER, player["id"])
            error, name = self._saveName(username, fields, None, player["display_name"])
            if error:
                self._html(
                    400,
                    pages.chooseName(
                        self._csrf(), config.publicUrl + "/account", error, name, player["display_name"], self._nextChange(player)
                    ),
                )
                return
            self._redirect("/account")

        def pageExport(self):
            session = self._pageSession()
            if session is None:
                return
            username, _ = session
            exported = social.store.export(username) or {}
            body = (json.dumps(exported, indent=2, sort_keys=True) + "\n").encode("utf-8")
            self._send(
                200,
                body,
                "application/json",
                (
                    ("Content-Disposition", 'attachment; filename="arcade-social-data.json"'),
                    ("Cache-Control", "no-store"),
                ),
            )

        def postDelete(self):
            fields = self._form()
            session = self._pageSession()
            if session is None:
                return
            username, player = session
            if fields.get("confirm", "").strip().lower() != "delete":
                self.pageAccount(error="Type delete to confirm.", status=400)
                return
            social.store.deletePlayer(username)
            log("a player deleted their data")
            self._signOut()
            self._html(
                200,
                pages.message(
                    "Your data is deleted",
                    "Every score, achievement, like and your display name are gone from this service, "
                    "and you are signed out. Your UserAuth account itself still exists.",
                    config.defaultReturn,
                    "Back to the games",
                ),
            )

        def postSignOut(self):
            fields = self._form()
            returnUrl = self._return(fields)
            self._signOut()
            self._redirect(returnUrl)

    return Handler


class _PageError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Server(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def makeServer(social, host="0.0.0.0", port=8080):
    return Server((host, port), makeHandler(social))


def startMaintenance(social, intervalSeconds=3600):
    """Prune the submission log now and then every interval, on a daemon thread."""

    def loop():
        while True:
            try:
                social.maintain()
            except Exception as e:  # keep the thread alive; the next run retries
                log("maintenance failed: %s" % e)
            time.sleep(intervalSeconds)

    thread = threading.Thread(target=loop, name="maintenance", daemon=True)
    thread.start()
    return thread
