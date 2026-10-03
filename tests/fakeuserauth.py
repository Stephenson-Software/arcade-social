# @author Daniel McCoy Stephenson
"""A stand-in for UserAuth (stdlib http.server), with the behaviour the service
depends on, taken from UserAuth's own code (controller/*.java, dto/*.java,
config/RateLimitFilter.java at UserAuth commit f0f1836):

- POST /register  201 | 400 {"message": "field: ..."} | 409
- POST /login     {token, tokenType, expiresAt, refreshToken} | 401
- POST /token/refresh  single-use refresh tokens, rotated on every exchange | 401
- GET /session/validate  Bearer -> {valid, username, roles, issuedAt, expiresAt} | 401 (checks revocation)
- POST /logout   revokes the token's whole session
- 429 + Retry-After on /login, /register, /token/refresh when `busy` is set
- usernames normalised to lowercase; tokens are JWTs whose `sub` is the username
"""

import base64
import http.server
import itertools
import json
import re
import threading
import time

_PASSWORD = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^a-zA-Z0-9]).+$")


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class FakeUserAuth(object):
    def __init__(self, accessSeconds=3600):
        self.accessSeconds = accessSeconds
        self.users = {}
        self.access = {}  # token -> (username, session, expires)
        self.refreshTokens = {}  # token -> (username, session)
        self.revokedSessions = set()
        self.calls = []  # (method, path, headers dict)
        self.busy = False
        self.broken = False
        self._counter = itertools.count(1)
        self._lock = threading.Lock()
        self.server = None

    # --- token minting ---------------------------------------------------------

    def _jwt(self, username, session, expires):
        header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        claims = {"sub": username, "exp": int(expires), "jti": "j%d" % next(self._counter), "sid": session}
        return "%s.%s.%s" % (header, _b64(json.dumps(claims).encode()), _b64(b"signature"))

    def _issue(self, username, session=None):
        session = session or "s%d" % next(self._counter)
        expires = time.time() + self.accessSeconds
        access = self._jwt(username, session, expires)
        refresh = "rt-%d-%s" % (next(self._counter), _b64(b"refresh"))
        self.access[access] = (username, session, expires)
        self.refreshTokens[refresh] = (username, session)
        return {
            "token": access,
            "tokenType": "Bearer",
            "expiresAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires)),
            "refreshToken": refresh,
        }

    def expire(self, accessToken):
        """Make an access token expired, as time passing would."""
        with self._lock:
            username, session, _ = self.access[accessToken]
            self.access[accessToken] = (username, session, time.time() - 1)

    def addUser(self, username, password):
        self.users[username.lower()] = password

    def count(self, path):
        return sum(1 for _, called, _ in self.calls if called == path)

    # --- server ------------------------------------------------------------------

    def start(self):
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _reply(self, status, payload=None, headers=()):
                body = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}")

            def _bearer(self):
                header = self.headers.get("Authorization") or ""
                return header[7:] if header.startswith("Bearer ") else None

            def _record(self):
                fake.calls.append((self.command, self.path, dict(self.headers)))

            def do_GET(self):
                self._record()
                if fake.broken:
                    return self._reply(500, {"message": "an unexpected error occurred"})
                if self.path != "/session/validate":
                    return self._reply(404, {})
                with fake._lock:
                    found = fake.access.get(self._bearer() or "")
                    if not found or found[2] < time.time() or found[1] in fake.revokedSessions:
                        return self._reply(401, {"status": 401, "message": "token is not valid"})
                return self._reply(200, {"valid": True, "username": found[0], "roles": [], "issuedAt": None, "expiresAt": None})

            def do_POST(self):
                self._record()
                if fake.broken:
                    return self._reply(500, {"message": "an unexpected error occurred"})
                if fake.busy and self.path in ("/login", "/register", "/token/refresh"):
                    return self._reply(429, {"message": "Too many requests"}, (("Retry-After", "42"),))
                body = self._body() if self.path != "/logout" else {}
                with fake._lock:
                    if self.path == "/register":
                        username = (body.get("username") or "").strip().lower()
                        password = body.get("password") or ""
                        if not 3 <= len(username) <= 50:
                            return self._reply(400, {"message": "username: username must be between 3 and 50 characters"})
                        if not 8 <= len(password) <= 72 or not _PASSWORD.match(password):
                            return self._reply(
                                400,
                                {"message": "password: password must contain a lowercase letter, an uppercase "
                                 "letter, a digit, and a special character"},
                            )
                        if username in fake.users:
                            return self._reply(409, {"message": "username already exists"})
                        fake.users[username] = password
                        return self._reply(201, {"id": len(fake.users), "username": username, "email": None})
                    if self.path == "/login":
                        username = (body.get("username") or "").strip().lower()
                        if fake.users.get(username) != body.get("password"):
                            return self._reply(401, {"message": "invalid username or password"})
                        return self._reply(200, fake._issue(username))
                    if self.path == "/token/refresh":
                        found = fake.refreshTokens.pop(body.get("refreshToken") or "", None)
                        if not found or found[1] in fake.revokedSessions:
                            return self._reply(401, {"message": "invalid refresh token"})
                        return self._reply(200, fake._issue(found[0], found[1]))
                    if self.path == "/logout":
                        found = fake.access.get(self._bearer() or "")
                        if not found or found[2] < time.time():
                            return self._reply(401, {"message": "token is not valid"})
                        fake.revokedSessions.add(found[1])
                        return self._reply(200, {"message": "logged out"})
                return self._reply(404, {})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        return self

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
