# @author Daniel McCoy Stephenson
"""A running arcade-social against a fake UserAuth, and a client that behaves
like one browser profile (it keeps cookies and sends Origin)."""

import http.client
import json
import os
import re
import threading
from urllib.parse import urlencode

from fakeuserauth import FakeUserAuth

from arcade_social.config import Config
from arcade_social.server import Social, makeServer

SERVICE = "https://api.play.example.test"
PORTAL = "https://example.test"
GAME_DOMAIN = "play.example.test"
FISHE = "https://fishe.play.example.test"
FROG = "https://frog-hopper.play.example.test"
TIDEWATER = "https://tidewater.play.example.test"
TIDEWATER_ALIAS = "https://tidewater.example.test"
PASSWORD = "Correct-horse-1"

GAMES_YAML = """games:
  - slug: tidewater
    title: Tidewater
    repo: Stephenson-Software/Tidewater
    token_sha256: "0000000000000000000000000000000000000000000000000000000000000000"
    aliases: [tidewater.example.test]
  - slug: fishe
    title: FishE
    repo: Stephenson-Software/FishE
    token_sha256: "1111111111111111111111111111111111111111111111111111111111111111"
  - slug: frog-hopper
    title: Frog Hopper
    repo: Stephenson-Software/Frog-Hopper-2D
    token_sha256: "2222222222222222222222222222222222222222222222222222222222222222"
    kind: static
"""

BOARDS_YAML = """games:
  fishe:
    boards:
      - id: most-money
        title: Most money in one save
        order: desc
        unit: dollars
        integer: true
        min: 0
        max: 100000000
        maxPerHour: 5
    achievements:
      - id: first-catch
        title: First Catch
        description: Catch your first fish
      - id: secret
        title: Secret
        description: You found it
        hidden: true
  frog-hopper:
    boards:
      - id: fastest
        title: Fastest crossing
        order: asc
        unit: seconds
        min: 1.5
        max: 3600
"""

WRITE_HEADERS = {"Content-Type": "application/json", "X-Play-Client": "1"}


class Response(object):
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers  # list of (name, value)
        self.body = body

    def header(self, name):
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return None

    def all(self, name):
        return [value for key, value in self.headers if key.lower() == name.lower()]

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self):
        return self.body.decode("utf-8")


class Client(object):
    """One browser profile: a cookie jar for the service's host."""

    def __init__(self, port, ip="203.0.113.7"):
        self.port = port
        self.ip = ip
        self.cookies = {}

    def request(self, method, path, origin=None, body=None, headers=None, cookies=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        sent = {"Host": "api.play.example.test", "X-Forwarded-For": self.ip}
        if origin:
            sent["Origin"] = origin
        if cookies and self.cookies:
            sent["Cookie"] = "; ".join("%s=%s" % item for item in self.cookies.items())
        sent.update(headers or {})
        if isinstance(body, str):
            body = body.encode("utf-8")
        connection.request(method, path, body=body, headers=sent)
        raw = connection.getresponse()
        response = Response(raw.status, raw.getheaders(), raw.read())
        connection.close()
        for cookie in response.all("Set-Cookie"):
            nameValue = cookie.split(";", 1)[0]
            name, _, value = nameValue.partition("=")
            if "Max-Age=0" in cookie or not value:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value
        return response

    # pages

    def csrfFrom(self, response):
        match = re.search(r'name="csrf" value="([^"]+)"', response.text)
        assert match, response.text
        return match.group(1)

    def form(self, path, fields, origin=SERVICE, csrfPage="/signin"):
        page = self.request("GET", csrfPage)
        fields = dict(fields)
        fields.setdefault("csrf", self.csrfFrom(page))
        return self.request(
            "POST",
            path,
            origin=origin,
            body=urlencode(fields),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    def signIn(self, username, password=PASSWORD, returnUrl=None):
        fields = {"username": username, "password": password}
        if returnUrl:
            fields["return"] = returnUrl
        return self.form("/signin", fields)

    def chooseName(self, name):
        return self.form("/welcome", {"name": name}, csrfPage="/signin")

    # API

    def api(self, method, path, origin=FISHE, payload=None, headers=None):
        sent = dict(WRITE_HEADERS) if method not in ("GET", "HEAD") else {}
        sent.update(headers or {})
        body = json.dumps(payload) if payload is not None else None
        return self.request(method, path, origin=origin, body=body, headers=sent)


class Env(object):
    def __init__(self, tmpPath, operators=("boss",), clock=None, store=None, gamesYaml=GAMES_YAML, boardsYaml=BOARDS_YAML):
        self.fake = FakeUserAuth().start()
        self.registryPath = os.path.join(str(tmpPath), "games.yaml")
        self.boardsPath = os.path.join(str(tmpPath), "boards.yaml")
        with open(self.registryPath, "w") as handle:
            handle.write(gamesYaml)
        with open(self.boardsPath, "w") as handle:
            handle.write(boardsYaml)
        self.config = Config(
            publicUrl=SERVICE,
            portalOrigin=PORTAL,
            gameDomain=GAME_DOMAIN,
            registryPath=self.registryPath,
            boardsPath=self.boardsPath,
            databasePath=os.path.join(str(tmpPath), "social.sqlite3"),
            userauthUrl=self.fake.url,
            operators=operators,
            trustForwardedFor=True,
            defaultReturn=PORTAL + "/play",
        )
        self.social = Social(self.config, store=store)
        self.server = makeServer(self.social, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def client(self, ip="203.0.113.7"):
        return Client(self.port, ip)

    def player(self, username, name=None, ip=None):
        """A signed-in client for a new account, with a display name."""
        self.fake.addUser(username, PASSWORD)
        client = self.client(ip or "198.51.100.%d" % (len(self.fake.users) % 250 + 1))
        response = client.signIn(username)
        assert response.status == 303, response.text
        if name is not False:
            response = client.chooseName(name or username.capitalize() + "Name")
            assert response.status == 303, response.text
        return client

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.fake.stop()
