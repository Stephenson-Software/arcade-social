# @author Daniel McCoy Stephenson
"""The walkable arcade hall as a caller: portal-class reads and likes, never
scores, achievements, cloud saves or deletion; a valid sign-in return."""

import pytest

from helpers import FISHE, HALL, HALL_ARCADE, PORTAL


def _acao(response):
    return response.header("Access-Control-Allow-Origin")


@pytest.mark.parametrize("origin", [HALL, HALL_ARCADE])
def test_the_hall_reads_the_session_and_likes_with_credentialed_cors(env, origin):
    client = env.player("alice", "Alice")
    session = client.api("GET", "/v1/session", origin=origin)
    assert session.status == 200 and session.json()["signedIn"] is True
    assert session.json()["displayName"] == "Alice"
    assert _acao(session) == origin
    assert session.header("Access-Control-Allow-Credentials") == "true"
    assert "Origin" in session.header("Vary")
    # like and unlike any registry game, as the portal can
    assert client.api("POST", "/v1/likes/fishe", origin=origin).json() == {"slug": "fishe", "count": 1, "liked": True}
    assert client.api("POST", "/v1/likes/frog-hopper", origin=origin).status == 200
    assert client.api("POST", "/v1/likes/nosuchgame", origin=origin).status == 404
    mine = client.api("GET", "/v1/likes/me", origin=origin)
    assert mine.status == 200 and mine.json() == ["fishe", "frog-hopper"]
    assert _acao(mine) == origin
    assert client.api("DELETE", "/v1/likes/fishe", origin=origin).json()["liked"] is False
    assert client.api("GET", "/v1/me/fishe", origin=origin).status == 200
    assert client.api("GET", "/v1/boards/fishe/most-money/around-me", origin=origin).status == 200
    public = client.api("GET", "/v1/likes/counts", origin=origin)
    assert public.status == 200 and _acao(public) == origin


@pytest.mark.parametrize("origin", [HALL, HALL_ARCADE])
def test_the_hall_can_never_submit_scores_or_achievements(env, origin):
    client = env.player("bob", "Bob")
    score = client.api("POST", "/v1/scores/most-money", origin=origin, payload={"value": 500})
    assert score.status == 403
    unlock = client.api("POST", "/v1/achievements/first-catch", origin=origin)
    assert unlock.status == 403
    assert env.social.store.top("fishe", env.social.boards.get("fishe").board("most-money"), 10)[1] == 0
    assert client.api("GET", "/v1/me/fishe", origin=FISHE).json()["achievements"] == []
    # nor cloud saves, nor deleting the player's data
    assert client.api("GET", "/v1/saves/fishe-saves", origin=origin).status == 403
    assert client.api("POST", "/v1/saves/fishe-saves/enroll", origin=origin).status == 403
    assert client.api("DELETE", "/v1/me/fishe", origin=origin).status == 403
    assert client.api("DELETE", "/v1/me", origin=origin, payload={"confirm": "delete"}).status == 403
    assert env.social.store.player("bob") is not None
    # the same writes from the game itself still work
    assert client.api("POST", "/v1/scores/most-money", origin=FISHE, payload={"value": 500}).status == 200


def test_hall_preflights_follow_what_the_hall_may_do(env):
    client = env.client()

    def preflight(path, method, origin=HALL):
        return client.request("OPTIONS", path, origin=origin, headers={"Access-Control-Request-Method": method})

    ok = preflight("/v1/likes/fishe", "POST")
    assert ok.status == 204 and _acao(ok) == HALL
    assert preflight("/v1/session", "GET").status == 204
    for path, method in (
        ("/v1/scores/most-money", "POST"),
        ("/v1/achievements/first-catch", "POST"),
        ("/v1/saves/fishe-saves", "PUT"),
        ("/v1/me", "DELETE"),
        ("/v1/admin/exclude/x", "POST"),
    ):
        refused = preflight(path, method)
        assert refused.status == 403 and _acao(refused) is None, path
    # the portal may delete the player's data, so the refusal is the hall's own rule
    assert preflight("/v1/me", "DELETE", origin=PORTAL).status == 204


def test_without_hall_origins_configured_the_hall_is_nobody(tmp_path):
    from helpers import Env

    env = Env(tmp_path, hallOrigins=())
    try:
        client = env.player("carol")
        response = client.api("GET", "/v1/session", origin=HALL)
        assert response.status == 403 and _acao(response) is None
        assert env.social.safeReturn(HALL + "/hall/") == PORTAL + "/play"
    finally:
        env.stop()


def test_sign_in_returns_to_the_hall(env):
    social = env.social
    target = HALL + "/hall/?at=fishe"
    assert social.safeReturn(target) == target
    assert social.safeReturn(HALL_ARCADE + "/") == HALL_ARCADE + "/"
    assert social.safeReturn(HALL_ARCADE) == HALL_ARCADE + "/"
    for bad in ("http://play.example.test/hall/", "https://play.example.test:8443/hall/",
                "https://x@play.example.test/hall/", "https://play.example.test.evil.example/hall/"):
        assert social.safeReturn(bad) == PORTAL + "/play", bad
    page = env.client().request("GET", "/signin?return=" + target)
    assert page.status == 200
    csp = page.header("Content-Security-Policy")
    assert HALL in csp.split() and HALL_ARCADE in csp.split()
    env.fake.addUser("dana", "Correct-horse-1")
    client = env.client()
    first = client.signIn("dana", returnUrl=target)
    assert first.status == 303 and first.header("Location").startswith("/welcome?")
    chosen = client.form("/welcome", {"name": "Dana", "return": target})
    assert chosen.status == 303 and chosen.header("Location") == target
    again = env.client()
    assert again.signIn("dana", returnUrl=target).header("Location") == target
