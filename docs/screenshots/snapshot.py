# @author Daniel McCoy Stephenson
"""Snapshot the service's real pages and JSON for the README screenshots.

Starts the real server against the test suite's fake UserAuth (tests/fakeuserauth.py)
with demo accounts and the example boards and saves, then writes what the server
answered to docs/screenshots/build/: each page's HTML exactly as sent, plus api.html,
the JSON of a few public reads laid out as a terminal. darkroom (media.json) shoots
those files. Nothing here talks to the network beyond 127.0.0.1.

    python3 docs/screenshots/snapshot.py
"""

import html
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [os.path.join(ROOT, "src"), os.path.join(ROOT, "tests")]

from helpers import FISHE, FROG, GAMES_YAML, PORTAL, SERVICE, Env  # noqa: E402

BUILD = os.path.join(HERE, "build")


def _read(name):
    with open(os.path.join(ROOT, "examples", name)) as handle:
        return handle.read()


def _write(name, text):
    with open(os.path.join(BUILD, name), "w") as handle:
        handle.write(text)


def _ok(response, status=200):
    assert response.status == status, (response.status, response.text)
    return response


def _upload(client, parent, files, label):
    document = {
        "format": "tak-saves",
        "version": 1,
        "game": "fishe-saves",
        "exported": "2026-11-21T00:00:00Z",
        "files": files,
    }
    body = {
        "parent": parent,
        "device": "device-" + label.split()[0].lower(),
        "deviceLabel": label,
        "file": document,
    }
    return _ok(client.api("PUT", "/v1/saves/fishe-saves", payload=body), 201).json()["id"]


def _terminal(calls):
    blocks = []
    for path, status, payload in calls:
        blocks.append(
            '<div class="cmd"><span class="p">GET</span> %s <span class="s">&rarr; %d</span></div><pre>%s</pre>'
            % (html.escape(SERVICE + path), status, html.escape(json.dumps(payload, indent=2)))
        )
    return """<!doctype html><html><head><meta charset="utf-8"><title>arcade-social API</title><style>
body { margin: 0; background: #0f1115; color: #d7dae0; font: 13px/1.45 ui-monospace, "DejaVu Sans Mono", monospace; }
main { padding: 18px 22px; }
.cmd { color: #8fd6a0; margin: 14px 0 4px; } .cmd:first-child { margin-top: 0; }
.p { color: #7fb0dc; font-weight: 700; } .s { color: #a5a5a5; }
pre { margin: 0; white-space: pre-wrap; }
</style></head><body><main>%s</main></body></html>""" % "".join(
        blocks
    )


def main():
    os.makedirs(BUILD, exist_ok=True)
    tmpdir = tempfile.TemporaryDirectory(prefix="arcade-social-shots-")
    env = Env(
        tmpdir.name,
        gamesYaml=GAMES_YAML,
        boardsYaml=_read("boards.yaml"),
        savesYaml=_read("saves.yaml"),
        savesMode="on",
        savesAccounts=None,
    )
    try:
        # Demo players with scores, unlocks and likes.
        scores = [
            ("wren", "Wren", 48210),
            ("otto", "Otto", 31975),
            ("marisol", "Marisol", 31975),
            ("thistle", "Thistle", 12040),
            ("anvil", "Anvil", 6500),
        ]
        players = {}
        for username, name, money in scores:
            client = env.player(username, name)
            players[username] = client
            _ok(client.api("POST", "/v1/scores/most-money", origin=FISHE, payload={"value": money}))
            _ok(client.api("POST", "/v1/achievements/first-catch", origin=FISHE))
            if money >= 10000:
                _ok(client.api("POST", "/v1/achievements/reached-goal", origin=FISHE))
            _ok(client.api("POST", "/v1/likes/fishe", origin=PORTAL))
        for username, seconds in (("wren", 14.2), ("otto", 11.8)):
            _ok(
                players[username].api(
                    "POST", "/v1/scores/fastest-crossing", origin=FROG, payload={"value": seconds}
                )
            )
            _ok(players[username].api("POST", "/v1/likes/frog-hopper", origin=PORTAL))

        # Wren backs up FishE saves from two browsers.
        wren = players["wren"]
        _ok(wren.api("POST", "/v1/saves/fishe-saves/enroll", origin=FISHE))
        first = _upload(wren, None, {"/saves/slot_1/save.json": '{"money": 9000}'}, "Firefox on laptop")
        second = _upload(wren, first, {"/saves/slot_1/save.json": '{"money": 48210}'}, "Firefox on laptop")
        _upload(
            wren,
            second,
            {"/saves/slot_1/save.json": '{"money": 48210}', "/saves/slot_2/save.json": '{"money": 120}'},
            "Safari on phone",
        )

        # Pages, as a visitor and as Wren.
        visitor = env.client("198.51.100.200")
        back = "?return=" + FISHE + "/"
        _write("signin.html", _ok(visitor.request("GET", "/signin" + back)).text)
        _write("register.html", _ok(visitor.request("GET", "/register" + back)).text)
        env.fake.addUser("newplayer", "Correct-horse-1")
        refused = visitor.signIn("newplayer", password="wrong-Password-1")
        _write("signin-error.html", refused.text)
        _ok(visitor.signIn("newplayer"), 303)
        _write("welcome.html", _ok(visitor.request("GET", "/welcome" + back)).text)
        _write("account.html", _ok(wren.request("GET", "/account")).text)
        _write("saves.html", _ok(wren.request("GET", "/account/saves")).text)

        # Public JSON reads.
        calls = []
        for path in (
            "/v1/boards/fishe/most-money?limit=5",
            "/v1/games/fishe/achievements",
            "/v1/likes/counts",
        ):
            response = _ok(visitor.request("GET", path))
            calls.append((path, response.status, response.json()))
        _write("api.html", _terminal(calls))
    finally:
        env.stop()
        tmpdir.cleanup()
    print("wrote %s" % BUILD)


if __name__ == "__main__":
    main()
