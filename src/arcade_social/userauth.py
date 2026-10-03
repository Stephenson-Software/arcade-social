# @author Daniel McCoy Stephenson
"""A client for UserAuth, the shared account
service, reached over the gateway's internal network (http://userauth:9998).
No browser can reach UserAuth; this service is a thin proxy in front of it,
the way dpc-api's AuthController and trace's /login are (RFC 0013 §1).

Endpoints used, as UserAuth defines them (its controllers and DTOs, read at
UserAuth commit f0f1836):

  POST /register         {username, password, email?} -> 201 {id, username, email, createdAt}
                         409 username taken, 400 validation ("field: message; ...")
  POST /login            {username, password} -> {token, tokenType, expiresAt, refreshToken}
                         401 bad credentials
  POST /token/refresh    {refreshToken} -> the same shape; refresh tokens are SINGLE-USE
                         (RefreshResponse: "the one just presented is now invalid")
  GET  /session/validate Authorization: Bearer -> {valid, username, roles, issuedAt, expiresAt};
                         401 if expired, bad or revoked (it checks revocation)
  POST /logout           Authorization: Bearer -> revokes that session
  429 on /login, /register, /token/refresh past 20 requests / 60 s per remote address.

The access token's `sub` claim is the username, normalised by UserAuth to
lowercase (User.normalizeUsername). The service keeps no session table: it
validates with /session/validate and caches a positive answer for at most 60
seconds (RFC 0013 §1), so a logout elsewhere takes effect within a minute.
"""

import base64
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request


class UserAuthError(Exception):
    def __init__(self, message, status=None, retryAfter=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.retryAfter = retryAfter


class Rejected(UserAuthError):
    """401: wrong credentials, or a token that is expired, bad or revoked."""


class Invalid(UserAuthError):
    """400: UserAuth refused the input; the message is its own (safe to show)."""


class Conflict(UserAuthError):
    """409: the username (or email) is taken."""


class Busy(UserAuthError):
    """429: UserAuth's own rate limit, which sees this whole service as one client."""


class Unavailable(UserAuthError):
    """UserAuth could not be reached or failed (5xx, timeout, garbage)."""


class Tokens(object):
    __slots__ = ("access", "refresh", "username", "expiresAt")

    def __init__(self, access, refresh, username, expiresAt):
        self.access = access
        self.refresh = refresh
        self.username = username
        self.expiresAt = expiresAt


def _digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokenClaims(token):
    """The claims of a JWT, decoded WITHOUT verifying it. Used only on tokens
    that just came from UserAuth over the internal network; tokens from a
    browser are always checked with /session/validate."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except (IndexError, ValueError, UnicodeError):
        raise Unavailable("UserAuth returned a token that is not a JWT")
    if not isinstance(claims, dict) or not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise Unavailable("UserAuth returned a token without a subject")
    return claims


class UserAuthClient(object):
    def __init__(self, baseUrl, timeout=5.0, cacheSeconds=60, forwardClientIp=False, clock=time.time):
        self.baseUrl = baseUrl.rstrip("/")
        self.timeout = timeout
        self.cacheSeconds = min(60, cacheSeconds)
        self.forwardClientIp = forwardClientIp
        self._clock = clock
        self._cacheLock = threading.Lock()
        self._validated = {}
        self._refreshLock = threading.Lock()
        self._refreshed = {}

    # --- HTTP ---------------------------------------------------------------

    def _call(self, method, path, payload=None, bearer=None, clientIp=None):
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if bearer is not None:
            headers["Authorization"] = "Bearer " + bearer
        if self.forwardClientIp and clientIp:
            # Only useful if UserAuth runs with RATE_LIMIT_TRUST_FORWARDED_FOR=true;
            # off by default (see README, "Rate limits").
            headers["X-Forwarded-For"] = clientIp
        request = urllib.request.Request(self.baseUrl + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status, body = response.status, response.read()
                retryAfter = None
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read()
            retryAfter = e.headers.get("Retry-After") if e.headers else None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise Unavailable("UserAuth is unreachable: %s" % e)
        try:
            document = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeError):
            document = {}
        if not isinstance(document, dict):
            document = {}
        message = document.get("message") if isinstance(document.get("message"), str) else ""
        if 200 <= status < 300:
            return status, document
        if status == 401:
            raise Rejected(message or "unauthorized", status)
        if status == 400:
            raise Invalid(message or "UserAuth refused the request", status)
        if status == 409:
            raise Conflict(message or "already exists", status)
        if status == 429:
            try:
                wait = int(retryAfter) if retryAfter else 60
            except ValueError:
                wait = 60
            raise Busy("UserAuth is rate limiting sign-ins", status, retryAfter=wait)
        raise Unavailable("UserAuth answered %d" % status, status)

    def _tokens(self, document):
        access = document.get("token")
        refresh = document.get("refreshToken")
        if not isinstance(access, str) or not isinstance(refresh, str) or not access or not refresh:
            raise Unavailable("UserAuth's token response is missing token or refreshToken")
        claims = tokenClaims(access)
        expires = claims.get("exp")
        if not isinstance(expires, (int, float)):
            expires = self._clock() + 3600
        return Tokens(access, refresh, claims["sub"].lower(), float(expires))

    # --- operations -----------------------------------------------------------

    def login(self, username, password, clientIp=None):
        _, document = self._call("POST", "/login", {"username": username, "password": password}, clientIp=clientIp)
        tokens = self._tokens(document)
        self._remember(tokens.access, tokens.username, tokens.expiresAt)
        return tokens

    def register(self, username, password, clientIp=None):
        self._call("POST", "/register", {"username": username, "password": password}, clientIp=clientIp)

    def refresh(self, refreshToken, clientIp=None):
        """Exchange a refresh token. Refresh tokens rotate on use, so a page that
        fires several requests at once would otherwise spend the token on the
        first and be signed out by the second: the result of each exchange is
        kept for 60 s and handed to any request presenting the same old token."""
        key = _digest(refreshToken)
        with self._refreshLock:
            now = self._clock()
            for old in [old for old, (_, at) in self._refreshed.items() if now - at >= 60]:
                del self._refreshed[old]
            if key in self._refreshed:
                return self._refreshed[key][0]
            _, document = self._call("POST", "/token/refresh", {"refreshToken": refreshToken}, clientIp=clientIp)
            tokens = self._tokens(document)
            self._refreshed[key] = (tokens, now)
        self._remember(tokens.access, tokens.username, tokens.expiresAt)
        return tokens

    def validate(self, accessToken):
        """The username an access token belongs to, or None if UserAuth says it
        is not valid (expired, bad, revoked). Raises Unavailable if UserAuth
        cannot answer."""
        key = _digest(accessToken)
        now = self._clock()
        with self._cacheLock:
            cached = self._validated.get(key)
            if cached is not None and now < cached[1]:
                return cached[0]
        try:
            _, document = self._call("GET", "/session/validate", bearer=accessToken)
        except Rejected:
            self.forget(accessToken)
            return None
        username = document.get("username")
        if document.get("valid") is not True or not isinstance(username, str) or not username:
            return None
        username = username.lower()
        expires = None
        try:
            expires = tokenClaims(accessToken).get("exp")
        except Unavailable:
            pass
        self._remember(accessToken, username, expires if isinstance(expires, (int, float)) else now + 60)
        return username

    def logout(self, accessToken):
        """Revoke the session; best effort (the cookies are cleared either way)."""
        self.forget(accessToken)
        try:
            self._call("POST", "/logout", bearer=accessToken)
            return True
        except UserAuthError:
            return False

    # --- validation cache -----------------------------------------------------

    def _remember(self, accessToken, username, expiresAt):
        now = self._clock()
        until = min(now + self.cacheSeconds, float(expiresAt))
        if until <= now:
            return
        with self._cacheLock:
            if len(self._validated) > 10000:
                for old in [old for old, (_, at) in self._validated.items() if at <= now]:
                    del self._validated[old]
                if len(self._validated) > 10000:
                    self._validated.clear()
            self._validated[_digest(accessToken)] = (username, until)

    def forget(self, accessToken):
        with self._cacheLock:
            self._validated.pop(_digest(accessToken), None)
