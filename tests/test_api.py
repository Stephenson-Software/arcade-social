# @author Daniel McCoy Stephenson
"""The JSON API: who may call it (Origin, CORS, CSRF), scores and leaderboards
(RFC 0014), achievements, likes (RFC 0015), deletion and operator tools."""

import json

import pytest

from helpers import FISHE, FROG, PORTAL, SERVICE, TIDEWATER, TIDEWATER_ALIAS, WRITE_HEADERS


def _acao(response):
    return response.header("Access-Control-Allow-Origin")


# --- CORS and Origin --------------------------------------------------------------


@pytest.mark.parametrize("origin", [FISHE, FROG, TIDEWATER, TIDEWATER_ALIAS, PORTAL])
def test_allowed_origins_get_credentialed_cors(env, origin):
    client = env.player("alice")
    response = client.api("GET", "/v1/session", origin=origin)
    assert response.status == 200
    assert _acao(response) == origin
    assert response.header("Access-Control-Allow-Credentials") == "true"
    assert "Origin" in response.header("Vary")
    assert response.json()["signedIn"] is True


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "https://nosuchgame.play.example.test",
        "http://fishe.play.example.test",
        "https://fishe.play.example.test:444",
        "https://fishe.play.example.test.evil.example",
        "https://x.fishe.play.example.test",
        "https://api.play.example.test",  # the service itself is not a game
        "https://example.test.evil.example",
        "null",
        None,
    ],
)
def test_other_origins_get_no_credentialed_access(env, origin):
    client = env.player("alice")
    session = client.api("GET", "/v1/session", origin=origin)
    assert session.status == 403
    assert _acao(session) is None
    assert session.header("Access-Control-Allow-Credentials") is None
    like = client.api("POST", "/v1/likes/fishe", origin=origin)
    assert like.status == 403
    assert env.social.store.likeCounts() == {}


def test_preflight_allowed_and_refused(env):
    client = env.client()
    ok = client.request(
        "OPTIONS",
        "/v1/likes/fishe",
        origin=PORTAL,
        headers={"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type,x-play-client"},
    )
    assert ok.status == 204
    assert _acao(ok) == PORTAL
    assert ok.header("Access-Control-Allow-Credentials") == "true"
    assert "X-Play-Client" in ok.header("Access-Control-Allow-Headers")
    assert set(ok.header("Access-Control-Allow-Methods").split(", ")) == {"POST", "DELETE"}
    refused = client.request(
        "OPTIONS", "/v1/likes/fishe", origin="https://evil.example", headers={"Access-Control-Request-Method": "POST"}
    )
    assert refused.status == 403 and _acao(refused) is None
    wrongMethod = client.request(
        "OPTIONS", "/v1/likes/fishe", origin=PORTAL, headers={"Access-Control-Request-Method": "PUT"}
    )
    assert wrongMethod.status == 403
    admin = client.request(
        "OPTIONS", "/v1/admin/exclude/x", origin=PORTAL, headers={"Access-Control-Request-Method": "POST"}
    )
    assert admin.status == 403 and _acao(admin) is None


def test_public_reads_are_open_without_credentials(env):
    client = env.client()
    for path in ("/v1/likes/counts", "/v1/boards/fishe/most-money", "/v1/games/fishe/achievements", "/v1/games/fishe/boards"):
        response = client.request("GET", path, origin="https://evil.example")
        assert response.status == 200, path
        assert _acao(response) == "*"
        assert response.header("Access-Control-Allow-Credentials") is None
        none = client.request("GET", path)
        assert none.status == 200 and _acao(none) == "*"


# --- CSRF on cookie writes -------------------------------------------------------------


def test_writes_need_the_custom_header_and_json(env):
    client = env.player("alice")
    noHeader = client.api("POST", "/v1/likes/fishe", headers={"X-Play-Client": ""})
    assert noHeader.status == 403
    wrongValue = client.api("POST", "/v1/likes/fishe", headers={"X-Play-Client": "yes"})
    assert wrongValue.status == 403
    form = client.api("POST", "/v1/likes/fishe", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert form.status == 415
    text = client.api("POST", "/v1/likes/fishe", headers={"Content-Type": "text/plain"})
    assert text.status == 415
    assert env.social.store.likeCounts() == {}
    ok = client.api("POST", "/v1/likes/fishe", headers={"Content-Type": "application/json; charset=utf-8"})
    assert ok.status == 200


def test_write_without_origin_is_refused(env):
    client = env.player("alice")
    response = client.request("POST", "/v1/likes/fishe", headers=WRITE_HEADERS, body="{}")
    assert response.status == 403
    assert env.social.store.likeCounts() == {}


def test_signed_out_writes_are_401(env):
    client = env.client()
    response = client.api("POST", "/v1/likes/fishe", origin=PORTAL)
    assert response.status == 401
    assert response.json()["signIn"].endswith("/signin")


def test_unknown_fields_and_bad_json_are_refused(env):
    client = env.player("alice")
    assert client.api("POST", "/v1/scores/most-money", payload={"value": 1, "extra": 2}).status == 400
    bad = client.request("POST", "/v1/scores/most-money", origin=FISHE, headers=WRITE_HEADERS, body="{nope")
    assert bad.status == 400
    array = client.request("POST", "/v1/scores/most-money", origin=FISHE, headers=WRITE_HEADERS, body="[1]")
    assert array.status == 400
    nan = client.request("POST", "/v1/scores/most-money", origin=FISHE, headers=WRITE_HEADERS, body='{"value": NaN}')
    assert nan.status == 400
    big = client.request(
        "POST", "/v1/scores/most-money", origin=FISHE, headers=WRITE_HEADERS, body='{"value": 1, "run": "%s"}' % ("a" * 20000)
    )
    assert big.status == 413


def test_unknown_routes_and_methods(env):
    client = env.client()
    assert client.request("GET", "/v1/nope").status == 404
    response = client.request("PUT", "/v1/session", origin=PORTAL)
    assert response.status == 405 and response.header("Allow") == "GET"


# --- scores (RFC 0014) ---------------------------------------------------------------------


def test_a_score_counts_for_the_origins_game_only(env):
    client = env.player("alice")
    response = client.api("POST", "/v1/scores/most-money", origin=FISHE, payload={"value": 500})
    assert response.status == 200
    body = response.json()
    assert body == {
        "slug": "fishe",
        "board": "most-money",
        "best": 500,
        "improved": True,
        "rank": 1,
        "verified": False,
        "notice": "Scores are reported by players' browsers and are not verified.",
    }
    # The same board name does not exist for frog-hopper: the Origin picks the game.
    assert client.api("POST", "/v1/scores/most-money", origin=FROG, payload={"value": 500}).status == 404
    # The portal cannot report scores.
    assert client.api("POST", "/v1/scores/most-money", origin=PORTAL, payload={"value": 500}).status == 403


def test_score_bounds_type_and_integer_checks(env):
    client = env.player("alice")
    for value in (-1, 100000001, 1.5, "10", True, None, [1]):
        response = client.api("POST", "/v1/scores/most-money", payload={"value": value})
        assert response.status in (400, 422), value
    assert client.api("POST", "/v1/scores/most-money", payload={}).status == 400
    assert client.api("POST", "/v1/scores/most-money", payload={"value": 7.0}).json()["best"] == 7
    assert client.api("POST", "/v1/scores/most-money", payload={"value": 0}).status == 200
    assert client.api("POST", "/v1/scores/most-money", payload={"value": 100000000}).status == 200
    refused = client.api("POST", "/v1/scores/most-money", payload={"value": 100000001})
    assert refused.status == 422 and refused.json()["max"] == 100000000
    log = env.social.store.submissionLog("fishe", "most-money", 100)
    assert any(row["accepted"] == 0 and row["reason"] == "outside min..max" for row in log)
    frog = client.api("POST", "/v1/scores/fastest", origin=FROG, payload={"value": 1.4})
    assert frog.status == 422
    assert client.api("POST", "/v1/scores/fastest", origin=FROG, payload={"value": 12.25, "run": "r-1"}).status == 200
    assert client.api("POST", "/v1/scores/fastest", origin=FROG, payload={"value": 12, "run": "bad run!"}).status == 400


def test_scores_need_a_display_name(env):
    client = env.player("nameless", name=False)
    response = client.api("POST", "/v1/scores/most-money", payload={"value": 5})
    assert response.status == 409
    assert response.json()["welcome"].endswith("/welcome")
    session = client.api("GET", "/v1/session").json()
    assert session["needsDisplayName"] is True and session["displayName"] is None


def test_best_is_kept_in_the_boards_order(env):
    client = env.player("alice")
    assert client.api("POST", "/v1/scores/most-money", payload={"value": 100}).json()["improved"] is True
    worse = client.api("POST", "/v1/scores/most-money", payload={"value": 50}).json()
    assert worse["improved"] is False and worse["best"] == 100
    same = client.api("POST", "/v1/scores/most-money", payload={"value": 100}).json()
    assert same["improved"] is False
    assert client.api("POST", "/v1/scores/most-money", payload={"value": 101}).json()["best"] == 101
    # asc board: lower is better
    assert client.api("POST", "/v1/scores/fastest", origin=FROG, payload={"value": 30}).json()["best"] == 30
    assert client.api("POST", "/v1/scores/fastest", origin=FROG, payload={"value": 40}).json()["best"] == 30
    assert client.api("POST", "/v1/scores/fastest", origin=FROG, payload={"value": 20.5}).json()["best"] == 20.5


def test_max_per_hour_caps_submissions(env):
    client = env.player("alice")
    statuses = [client.api("POST", "/v1/scores/most-money", payload={"value": index}).status for index in range(7)]
    assert statuses == [200] * 5 + [429, 429]
    assert len(env.social.store.submissionLog("fishe", "most-money", 100)) == 5


def test_leaderboard_ordering_ties_and_ranks(env):
    clock = {"now": 1000000}
    env.social.store.clock = lambda: clock["now"]
    players = {}
    for name, value in (("ann", 300), ("ben", 500), ("cat", 300), ("dan", 100), ("eve", 500), ("fay", 200)):
        clock["now"] += 1000
        players[name] = env.player(name, name.capitalize() + "X")
        assert players[name].api("POST", "/v1/scores/most-money", payload={"value": value}).status == 200
    top = env.client().request("GET", "/v1/boards/fishe/most-money?limit=10").json()
    rows = [(entry["rank"], entry["displayName"], entry["value"]) for entry in top["entries"]]
    # Equal values share a rank (1, 1, 3, 3, 5, 6); whoever got there first is listed first.
    assert rows == [
        (1, "BenX", 500),
        (1, "EveX", 500),
        (3, "AnnX", 300),
        (3, "CatX", 300),
        (5, "FayX", 200),
        (6, "DanX", 100),
    ]
    assert top["total"] == 6 and top["verified"] is False and "not verified" in top["notice"]
    assert top["board"]["order"] == "desc"
    assert all(entry["achievedAt"].endswith("Z") for entry in top["entries"])
    assert "username" not in json.dumps(top) and "ann" not in [entry["displayName"] for entry in top["entries"]]
    limited = env.client().request("GET", "/v1/boards/fishe/most-money?limit=3").json()["entries"]
    assert [entry["displayName"] for entry in limited] == ["BenX", "EveX", "AnnX"]
    assert env.client().request("GET", "/v1/boards/fishe/most-money?limit=0").status == 400
    assert env.client().request("GET", "/v1/boards/fishe/most-money?limit=101").status == 400
    assert env.client().request("GET", "/v1/boards/fishe/nope").status == 404
    assert env.client().request("GET", "/v1/boards/nogame/most-money").status == 404
    # Around me: Fay (rank 5) with a window of 1 sees Cat, herself and Dan.
    around = players["fay"].api("GET", "/v1/boards/fishe/most-money/around-me?window=1").json()
    assert [(entry["rank"], entry["displayName"]) for entry in around["entries"]] == [(3, "CatX"), (5, "FayX"), (6, "DanX")]
    assert around["me"] == [entry["id"] for entry in around["entries"] if entry["displayName"] == "FayX"][0]
    # Eve's window starts mid-board on a tie: the first row's rank is counted, not assumed.
    eve = players["eve"].api("GET", "/v1/boards/fishe/most-money/around-me?window=0")
    assert eve.status == 400
    eve = players["eve"].api("GET", "/v1/boards/fishe/most-money/around-me?window=1").json()
    assert [(entry["rank"], entry["displayName"]) for entry in eve["entries"]] == [(1, "BenX"), (1, "EveX"), (3, "AnnX")]
    cat = players["cat"].api("GET", "/v1/boards/fishe/most-money/around-me?window=1").json()
    assert [(entry["rank"], entry["displayName"]) for entry in cat["entries"]] == [(3, "AnnX"), (3, "CatX"), (5, "FayX")]
    # A game page may ask only about its own game.
    assert players["fay"].api("GET", "/v1/boards/fishe/most-money/around-me", origin=FROG).status == 403
    assert players["fay"].api("GET", "/v1/boards/fishe/most-money/around-me", origin=PORTAL).status == 200


def test_ascending_board_ranks(env):
    for name, value in (("ann", 30.5), ("ben", 12.0), ("cat", 99)):
        env.player(name).api("POST", "/v1/scores/fastest", origin=FROG, payload={"value": value})
    rows = env.client().request("GET", "/v1/boards/frog-hopper/fastest").json()["entries"]
    assert [(entry["rank"], entry["value"]) for entry in rows] == [(1, 12), (2, 30.5), (3, 99)]


def test_me_shows_own_bests_and_unlocks(env):
    client = env.player("alice", "Alice A")
    client.api("POST", "/v1/scores/most-money", payload={"value": 42})
    client.api("POST", "/v1/achievements/first-catch")
    me = client.api("GET", "/v1/me/fishe").json()
    assert me["displayName"] == "Alice A"
    assert me["bests"][0]["board"] == "most-money" and me["bests"][0]["value"] == 42 and me["bests"][0]["rank"] == 1
    assert me["achievements"][0]["achievement"] == "first-catch" and me["achievements"][0]["title"] == "First Catch"
    assert client.api("GET", "/v1/me/fishe", origin=FROG).status == 403
    assert client.api("GET", "/v1/me/fishe", origin=PORTAL).status == 200
    assert env.client().api("GET", "/v1/me/fishe", origin=PORTAL).status == 401


# --- achievements --------------------------------------------------------------------------


def test_unlock_is_idempotent_and_percentages(env):
    alice = env.player("alice")
    first = alice.api("POST", "/v1/achievements/first-catch").json()
    assert first["newlyUnlocked"] is True and first["unlocked"] is True and first["verified"] is False
    again = alice.api("POST", "/v1/achievements/first-catch").json()
    assert again["newlyUnlocked"] is False and again["unlockedAt"] == first["unlockedAt"]
    bob = env.player("bob")
    bob.api("POST", "/v1/scores/most-money", payload={"value": 1})
    carl = env.player("carl")
    carl.api("POST", "/v1/achievements/first-catch")
    carl.api("POST", "/v1/achievements/secret")
    listed = env.client().request("GET", "/v1/games/fishe/achievements").json()
    assert listed["players"] == 3
    byId = dict((achievement["id"], achievement) for achievement in listed["achievements"])
    assert byId["first-catch"]["players"] == 2 and byId["first-catch"]["percent"] == 66.7
    assert byId["secret"]["percent"] == 33.3
    # Hidden achievements keep their title and description private.
    assert byId["secret"]["title"] == "Hidden achievement" and byId["secret"]["description"] is None
    assert alice.api("POST", "/v1/achievements/nope").status == 404
    assert alice.api("POST", "/v1/achievements/first-catch", origin=PORTAL).status == 403
    # frog-hopper declares no achievements of that name.
    assert alice.api("POST", "/v1/achievements/first-catch", origin=FROG).status == 404


def test_signed_out_unlock_is_401_and_records_nothing(env):
    response = env.client().api("POST", "/v1/achievements/first-catch")
    assert response.status == 401
    assert env.client().request("GET", "/v1/games/fishe/achievements").json()["players"] == 0


# --- likes (RFC 0015) ---------------------------------------------------------------------------


def test_like_and_unlike_are_idempotent(env):
    client = env.player("alice")
    assert client.api("POST", "/v1/likes/fishe", origin=PORTAL).json() == {"slug": "fishe", "count": 1, "liked": True}
    assert client.api("POST", "/v1/likes/fishe", origin=PORTAL).json() == {"slug": "fishe", "count": 1, "liked": True}
    other = env.player("bob")
    assert other.api("POST", "/v1/likes/fishe", origin=PORTAL).json()["count"] == 2
    assert client.api("DELETE", "/v1/likes/fishe", origin=PORTAL).json() == {"slug": "fishe", "count": 1, "liked": False}
    assert client.api("DELETE", "/v1/likes/fishe", origin=PORTAL).json() == {"slug": "fishe", "count": 1, "liked": False}
    counts = env.client().request("GET", "/v1/likes/counts").json()
    assert counts == {"fishe": 1}


def test_like_origin_rules(env):
    client = env.player("alice")
    # The portal may like any registry game; a game only itself.
    assert client.api("POST", "/v1/likes/frog-hopper", origin=PORTAL).status == 200
    assert client.api("POST", "/v1/likes/fishe", origin=FISHE).status == 200
    assert client.api("POST", "/v1/likes/frog-hopper", origin=FISHE).status == 403
    assert client.api("POST", "/v1/likes/tidewater", origin=TIDEWATER_ALIAS).status == 200
    assert client.api("POST", "/v1/likes/nosuchgame", origin=PORTAL).status == 404
    assert client.api("POST", "/v1/likes/Bad_Slug", origin=PORTAL).status == 404
    assert sorted(client.api("GET", "/v1/likes/me", origin=PORTAL).json()) == ["fishe", "frog-hopper", "tidewater"]
    assert env.client().api("GET", "/v1/likes/me", origin=PORTAL).status == 401


def test_my_likes_are_private(env):
    alice = env.player("alice")
    alice.api("POST", "/v1/likes/fishe", origin=PORTAL)
    bob = env.player("bob")
    assert bob.api("GET", "/v1/likes/me", origin=PORTAL).json() == []
    assert alice.api("GET", "/v1/likes/me", origin="https://evil.example").status == 403


def test_likes_survive_a_game_leaving_the_registry(env):
    client = env.player("alice")
    client.api("POST", "/v1/likes/frog-hopper", origin=PORTAL)
    client.api("POST", "/v1/likes/fishe", origin=PORTAL)
    import os
    import time

    text = open(env.registryPath).read()
    start = text.index("  - slug: frog-hopper")
    with open(env.registryPath, "w") as handle:
        handle.write(text[:start])
    later = time.time() + 5
    os.utime(env.registryPath, (later, later))
    assert env.social.registry.get("frog-hopper") is None
    # Not counted on /play any more, but kept and still listed with the raw slug.
    assert env.client().request("GET", "/v1/likes/counts").json() == {"fishe": 1}
    assert client.api("GET", "/v1/likes/me", origin=PORTAL).json() == ["frog-hopper", "fishe"]
    assert env.social.store.likeCounts()["frog-hopper"] == 1
    # It cannot be liked again, but it can still be removed.
    assert client.api("POST", "/v1/likes/frog-hopper", origin=PORTAL).status == 404
    assert client.api("DELETE", "/v1/likes/frog-hopper", origin=PORTAL).status == 200
    assert client.api("GET", "/v1/likes/me", origin=PORTAL).json() == ["fishe"]


# --- deletion -------------------------------------------------------------------------------------


def test_delete_one_games_data(env):
    client = env.player("alice")
    client.api("POST", "/v1/scores/most-money", payload={"value": 9})
    client.api("POST", "/v1/achievements/first-catch")
    client.api("POST", "/v1/likes/fishe")
    client.api("POST", "/v1/scores/fastest", origin=FROG, payload={"value": 9})
    assert client.api("DELETE", "/v1/me/frog-hopper", origin=FISHE).status == 403
    response = client.api("DELETE", "/v1/me/fishe", origin=FISHE)
    assert response.json() == {"slug": "fishe", "deleted": 3}
    assert client.api("GET", "/v1/me/fishe").json()["bests"] == []
    # Likes and other games are untouched.
    assert env.client().request("GET", "/v1/likes/counts").json() == {"fishe": 1}
    assert env.client().request("GET", "/v1/boards/frog-hopper/fastest").json()["total"] == 1


def test_delete_everything_via_api_and_account_page(env):
    client = env.player("alice", "Alice A")
    client.api("POST", "/v1/scores/most-money", payload={"value": 9})
    client.api("POST", "/v1/likes/fishe")
    assert client.api("DELETE", "/v1/me", origin=PORTAL, payload={}).status == 400
    response = client.api("DELETE", "/v1/me", origin=PORTAL, payload={"confirm": "delete"})
    assert response.json() == {"deleted": True}
    assert env.social.store.player("alice") is None
    assert env.client().request("GET", "/v1/likes/counts").json() == {}
    assert env.client().request("GET", "/v1/boards/fishe/most-money").json()["total"] == 0
    # The released name is free at once.
    bob = env.player("bob", "Alice A")
    assert bob.api("GET", "/v1/session").json()["displayName"] == "Alice A"
    # The account page path also signs out.
    bob.api("POST", "/v1/likes/fishe")
    refused = bob.form("/account/delete", {"confirm": "nope"}, csrfPage="/account")
    assert refused.status == 400
    deleted = bob.form("/account/delete", {"confirm": "delete"}, csrfPage="/account")
    assert deleted.status == 200 and "deleted" in deleted.text
    assert env.social.store.player("bob") is None
    assert "__Host-play_at" not in bob.cookies


# --- operator tools -----------------------------------------------------------------------------------


def test_operator_endpoints(env):
    cheat = env.player("cheater", "Cheater")
    entry = cheat.api("POST", "/v1/scores/most-money", payload={"value": 99999999})
    assert entry.status == 200
    entryId = env.client().request("GET", "/v1/boards/fishe/most-money").json()["entries"][0]["id"]
    boss = env.player("boss", "The Boss")
    # Reads: operator only, never from another origin, never CORS.
    found = boss.request("GET", "/v1/admin/entries/%d" % entryId)
    assert found.status == 200 and found.json()["username"] == "cheater" and _acao(found) is None
    assert boss.request("GET", "/v1/admin/entries/%d" % entryId, origin=PORTAL).status == 403
    assert cheat.request("GET", "/v1/admin/entries/%d" % entryId).status == 403
    assert env.client().request("GET", "/v1/admin/entries/%d" % entryId).status == 401
    # Writes: from the service's own origin, with the CSRF headers.
    assert boss.api("POST", "/v1/admin/exclude/cheater", origin=PORTAL, payload={}).status == 403
    assert boss.api("POST", "/v1/admin/exclude/cheater", origin=FISHE, payload={}).status == 403
    assert boss.request("POST", "/v1/admin/exclude/cheater", origin=SERVICE, body="{}").status == 403
    # Without any Origin, even with the CSRF headers.
    assert boss.request("POST", "/v1/admin/exclude/cheater", headers=WRITE_HEADERS, body="{}").status == 403
    assert env.social.store.exclusions() == []
    assert cheat.api("POST", "/v1/admin/exclude/cheater", origin=SERVICE, payload={}).status == 403
    excluded = boss.api("POST", "/v1/admin/exclude/Cheater", origin=SERVICE, payload={"reason": "forged"})
    assert excluded.json() == {"excluded": "cheater"}
    assert env.client().request("GET", "/v1/boards/fishe/most-money").json()["total"] == 0
    assert cheat.api("POST", "/v1/scores/most-money", payload={"value": 5}).json()["rank"] is None
    assert boss.request("GET", "/v1/admin/exclusions").json()["exclusions"][0]["reason"] == "forged"
    assert boss.api("DELETE", "/v1/admin/exclude/cheater", origin=SERVICE).status == 200
    assert env.client().request("GET", "/v1/boards/fishe/most-money").json()["total"] == 1
    assert boss.api("DELETE", "/v1/admin/entries/%d" % entryId, origin=SERVICE).json() == {"deleted": entryId}
    assert env.client().request("GET", "/v1/boards/fishe/most-money").json()["total"] == 0
    assert boss.api("DELETE", "/v1/admin/entries/%d" % entryId, origin=SERVICE).status == 404
    log = boss.request("GET", "/v1/admin/log/fishe/most-money").json()["submissions"]
    assert [row["value"] for row in log] == [5, 99999999]
    cheat.api("POST", "/v1/likes/fishe")
    assert boss.api("DELETE", "/v1/admin/likes/cheater", origin=SERVICE).json() == {"deleted": 1}
    assert env.client().request("GET", "/v1/likes/counts").json() == {}


def test_reserved_display_names_allowed_for_operators_only(env):
    impostor = env.player("someone", name=False)
    refused = impostor.chooseName("Admin")
    assert refused.status == 400 and "reserved" in refused.text
    boss = env.player("boss", name=False)
    assert boss.chooseName("Admin").status == 303


def test_huge_values_are_refused_before_storage(env):
    client = env.player("alice")
    for value in (10**30, -(10**30), 2**53):
        response = client.api("POST", "/v1/scores/most-money", payload={"value": value})
        assert response.status == 400, value
    assert client.api("POST", "/v1/scores/most-money", payload={"value": 1e300}).status == 400
    frog = client.api("POST", "/v1/scores/fastest", origin=FROG, payload={"value": 1e300})
    assert frog.status == 422  # a float board stores floats; the bounds refuse it


def test_reads_never_create_a_player_row(env):
    env.fake.addUser("reader", "Correct-horse-1")
    client = env.client()
    client.signIn("reader")
    env.social.store.deletePlayer("reader")
    assert client.api("GET", "/v1/me/fishe").json()["bests"] == []
    assert client.api("GET", "/v1/boards/fishe/most-money/around-me").json()["entries"] == []
    assert client.api("DELETE", "/v1/likes/fishe", origin=PORTAL).json()["liked"] is False
    assert client.api("DELETE", "/v1/me/fishe").json()["deleted"] == 0
    assert env.social.store.player("reader") is None
