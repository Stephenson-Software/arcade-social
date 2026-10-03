# @author Daniel McCoy Stephenson
"""Sign-in through the service's own pages, proxied to a fake UserAuth
(RFC 0013 §1): cookies, the form CSRF token, return URLs, refresh, sign-out,
registration and the rate limits in front of UserAuth's shared bucket."""

import json
from urllib.parse import urlencode

from helpers import FISHE, PASSWORD, PORTAL, SERVICE, TIDEWATER_ALIAS

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def _cookie(response, name):
    for cookie in response.all("Set-Cookie"):
        if cookie.startswith(name + "="):
            return cookie
    return None


def test_signin_page_has_csrf_token_and_strict_headers(env):
    client = env.client()
    page = client.request("GET", "/signin?return=" + FISHE + "/")
    assert page.status == 200
    csrf = _cookie(page, "__Host-play_csrf")
    assert csrf is not None
    attributes = [part.strip() for part in csrf.split(";")]
    assert "Secure" in attributes and "HttpOnly" in attributes and "Path=/" in attributes
    assert "SameSite=Strict" in attributes
    assert not any(part.lower().startswith("domain") for part in attributes)
    assert client.csrfFrom(page) == client.cookies["__Host-play_csrf"]
    policy = page.header("Content-Security-Policy")
    assert "default-src 'none'" in policy and "frame-ancestors 'none'" in policy
    assert "script-src" not in policy
    assert "form-action 'self' %s https://*.play.example.test https://tidewater.example.test" % PORTAL in policy
    assert page.header("X-Frame-Options") == "DENY"
    # Not no-referrer: under it browsers send "Origin: null" with form POSTs.
    assert page.header("Referrer-Policy") == "same-origin"
    assert 'content="same-origin"' in page.text and "no-referrer" not in page.text
    assert page.header("Cache-Control") == "no-store"
    assert '<script' not in page.text


def test_signin_sets_host_only_cookies_and_returns_to_the_game(env):
    env.fake.addUser("alice", PASSWORD)
    client = env.client()
    response = client.signIn("Alice", returnUrl=FISHE + "/play?x=1")
    assert response.status == 303
    # No display name yet: the welcome page comes first, carrying the return.
    assert response.header("Location").startswith("/welcome?")
    for name in ("__Host-play_at", "__Host-play_rt"):
        cookie = _cookie(response, name)
        assert cookie is not None, name
        attributes = [part.strip() for part in cookie.split(";")]
        assert "Secure" in attributes
        assert "HttpOnly" in attributes
        assert "SameSite=Lax" in attributes
        assert "Path=/" in attributes
        assert not any(part.lower().startswith("domain") for part in attributes), cookie
        assert any(part.startswith("Max-Age=") and int(part[8:]) > 0 for part in attributes)
    rt = _cookie(response, "__Host-play_rt")
    assert "Max-Age=%d" % (30 * 86400) in rt
    welcome = client.request("GET", response.header("Location"))
    assert welcome.status == 200 and "Choose a display name" in welcome.text
    chosen = client.form("/welcome", {"name": "Alice W", "return": FISHE + "/play?x=1"})
    assert chosen.status == 303
    assert chosen.header("Location") == FISHE + "/play?x=1"
    again = env.client()
    again.signIn("alice", returnUrl=FISHE + "/")
    # Second sign-in: a name exists, so straight back to the game.
    second = again.signIn("alice", returnUrl=FISHE + "/")
    assert second.header("Location") == FISHE + "/"


def test_wrong_password_is_generic(env):
    env.fake.addUser("alice", PASSWORD)
    client = env.client()
    response = client.signIn("alice", "Wrong-pass-1")
    assert response.status == 401
    assert "Wrong username or password." in response.text
    assert "__Host-play_at" not in client.cookies
    unknown = client.signIn("nobody", "Wrong-pass-1")
    assert unknown.status == 401 and "Wrong username or password." in unknown.text


def test_form_without_matching_csrf_token_is_refused(env):
    env.fake.addUser("alice", PASSWORD)
    client = env.client()
    client.request("GET", "/signin")
    body = urlencode({"username": "alice", "password": PASSWORD, "csrf": "x" * 43})
    response = client.request("POST", "/signin", origin=SERVICE, body=body, headers=FORM)
    assert response.status == 403
    assert env.fake.count("/login") == 0
    # No token at all, and no cookie at all.
    stranger = env.client()
    body = urlencode({"username": "alice", "password": PASSWORD})
    assert stranger.request("POST", "/signin", origin=SERVICE, body=body, headers=FORM).status == 403
    assert env.fake.count("/login") == 0


def test_form_from_another_origin_is_refused(env):
    env.fake.addUser("alice", PASSWORD)
    client = env.client()
    for origin in (FISHE, PORTAL, "https://evil.example", None, "null"):
        response = client.form("/signin", {"username": "alice", "password": PASSWORD}, origin=origin)
        assert response.status == 403, origin
    assert env.fake.count("/login") == 0


def test_return_url_is_never_an_open_redirect(env):
    social = env.social
    default = PORTAL + "/play"
    assert social.safeReturn(FISHE + "/x?y=1#z") == FISHE + "/x?y=1#z"
    assert social.safeReturn(TIDEWATER_ALIAS + "/") == TIDEWATER_ALIAS + "/"
    assert social.safeReturn(PORTAL + "/play/fishe") == PORTAL + "/play/fishe"
    assert social.safeReturn(SERVICE + "/account") == SERVICE + "/account"
    for bad in (
        "https://evil.example/",
        "http://fishe.play.example.test/",
        "https://nosuchgame.play.example.test/",
        "https://fishe.play.example.test:8443/",
        "https://user@fishe.play.example.test/",
        "https://fishe.play.example.test.evil.example/",
        "//evil.example/",
        "/relative",
        "javascript:alert(1)",
        "https://FISHE.play.example.test/",
        "https://fishe.play.example.test\\@evil.example/",
        "https://a.b.play.example.test/",
        "",
        None,
        "https://fishe.play.example.test/" + "a" * 3000,
    ):
        assert social.safeReturn(bad) == default, bad
    env.fake.addUser("bob", PASSWORD)
    client = env.client()
    client.signIn("bob")
    client.chooseName("Bobby")
    response = client.signIn("bob", returnUrl="https://evil.example/")
    assert response.header("Location") == default


def test_expired_access_token_is_refreshed_transparently(env):
    client = env.player("carol")
    access = client.cookies["__Host-play_at"]
    refresh = client.cookies["__Host-play_rt"]
    env.fake.expire(access)
    env.social.userauth.forget(access)
    response = client.api("GET", "/v1/session", origin=FISHE)
    assert response.json()["signedIn"] is True
    assert client.cookies["__Host-play_at"] != access
    assert client.cookies["__Host-play_rt"] != refresh
    newCookie = _cookie(response, "__Host-play_at")
    assert "HttpOnly" in newCookie and "Secure" in newCookie and "SameSite=Lax" in newCookie


def test_concurrent_refreshes_share_one_exchange(env):
    """Refresh tokens are single-use: two requests racing with the same old one
    must both stay signed in."""
    client = env.player("dave")
    access = client.cookies["__Host-play_at"]
    env.fake.expire(access)
    env.social.userauth.forget(access)
    import threading

    results = []

    def call():
        twin = env.client(client.ip)
        twin.cookies = dict(client.cookies)
        results.append(twin.api("GET", "/v1/session", origin=FISHE).json()["signedIn"])

    threads = [threading.Thread(target=call) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [True] * 6
    assert env.fake.count("/token/refresh") == 1


def test_revoked_refresh_token_signs_out_and_clears_cookies(env):
    client = env.player("erin")
    access = client.cookies["__Host-play_at"]
    env.fake.expire(access)
    env.social.userauth.forget(access)
    env.fake.refreshTokens.clear()
    response = client.api("GET", "/v1/session", origin=FISHE)
    assert response.json()["signedIn"] is False
    cleared = [cookie for cookie in response.all("Set-Cookie") if "Max-Age=0" in cookie]
    assert len(cleared) == 2
    assert "__Host-play_at" not in client.cookies and "__Host-play_rt" not in client.cookies


def test_validation_is_cached_at_most_sixty_seconds(env):
    client = env.player("fran")
    before = env.fake.count("/session/validate")
    for _ in range(5):
        client.api("GET", "/v1/session", origin=FISHE)
    # Login primed the cache: no validation calls at all within the minute.
    assert env.fake.count("/session/validate") == before
    assert env.social.userauth.cacheSeconds == 60


def test_logout_elsewhere_is_seen_after_the_cache(env):
    client = env.player("gail")
    access = client.cookies["__Host-play_at"]
    username, session, _ = env.fake.access[access]
    env.fake.revokedSessions.add(session)
    env.social.userauth.forget(access)  # as if the cache entry had aged out
    assert client.api("GET", "/v1/session", origin=FISHE).json()["signedIn"] is False


def test_sign_out_revokes_the_session_at_userauth(env):
    client = env.player("hank")
    access = client.cookies["__Host-play_at"]
    response = client.form("/signout", {"return": FISHE + "/"}, csrfPage="/signin")
    assert response.status == 303 and response.header("Location") == FISHE + "/"
    assert env.fake.access[access][1] in env.fake.revokedSessions
    assert "__Host-play_at" not in client.cookies
    assert env.fake.count("/logout") == 1


def test_api_sign_out_from_the_portal(env):
    client = env.player("ivan")
    response = client.api("POST", "/v1/signout", origin=PORTAL)
    assert response.status == 200 and response.json() == {"signedIn": False}
    assert client.api("GET", "/v1/session", origin=PORTAL).json()["signedIn"] is False


def test_register_proxies_to_userauth_then_signs_in(env):
    client = env.client()
    page = client.request("GET", "/register?return=" + FISHE + "/")
    assert page.status == 200 and "Create an account" in page.text
    response = client.form(
        "/register",
        {"username": "Newbie", "password": PASSWORD, "password2": PASSWORD, "return": FISHE + "/"},
        csrfPage="/register",
    )
    assert response.status == 303, response.text
    assert response.header("Location").startswith("/welcome?")
    assert "newbie" in env.fake.users
    assert "__Host-play_at" in client.cookies
    register = [headers for method, path, headers in env.fake.calls if path == "/register"][0]
    assert "X-Forwarded-For" not in register  # off unless ARCADE_SOCIAL_FORWARD_CLIENT_IP


def test_register_errors_are_shown(env):
    client = env.client()
    env.fake.addUser("taken", PASSWORD)
    mismatch = client.form("/register", {"username": "abc", "password": PASSWORD, "password2": "x"}, csrfPage="/register")
    assert mismatch.status == 400 and "do not match" in mismatch.text
    conflict = client.form(
        "/register", {"username": "taken", "password": PASSWORD, "password2": PASSWORD}, csrfPage="/register"
    )
    assert conflict.status == 409 and "taken" in conflict.text
    weak = client.form(
        "/register", {"username": "weakling", "password": "alllowercase", "password2": "alllowercase"}, csrfPage="/register"
    )
    assert weak.status == 400 and "special character" in weak.text


def test_register_is_rate_limited_per_client(env):
    client = env.client(ip="192.0.2.50")
    statuses = []
    for index in range(7):
        name = "user%d" % index
        statuses.append(
            client.form(
                "/register", {"username": name, "password": PASSWORD, "password2": PASSWORD}, csrfPage="/register"
            ).status
        )
        client.cookies.pop("__Host-play_at", None)
        client.cookies.pop("__Host-play_rt", None)
    assert statuses[:5] == [303] * 5
    assert statuses[5:] == [429, 429]
    assert len(env.fake.users) == 5


def test_signin_is_rate_limited_per_client_ip_before_userauth(env):
    """UserAuth sees one client (this service); the service limits each player."""
    env.fake.addUser("alice", PASSWORD)
    attacker = env.client(ip="192.0.2.66")
    # A different username each time, so only the per-IP limit can stop it.
    statuses = [attacker.signIn("user%d" % index, "Wrong-pass-%d" % index).status for index in range(12)]
    assert statuses[:10] == [401] * 10
    assert statuses[10:] == [429, 429]
    assert env.fake.count("/login") == 10
    # Another player, from another address, still signs in.
    env.fake.addUser("bob", PASSWORD)
    other = env.client(ip="192.0.2.67")
    assert other.signIn("bob").status == 303


def test_signin_is_rate_limited_per_username(env):
    env.fake.addUser("victim", PASSWORD)
    statuses = []
    for index in range(11):
        attacker = env.client(ip="192.0.2.%d" % (100 + index))
        statuses.append(attacker.signIn("Victim", "Wrong-pass-%d" % index).status)
    assert statuses[:10] == [401] * 10 and statuses[10] == 429


def test_userauth_busy_is_reported_not_treated_as_bad_password(env):
    env.fake.addUser("alice", PASSWORD)
    env.fake.busy = True
    response = env.client().signIn("alice")
    assert response.status == 429
    assert response.header("Retry-After") == "42"
    assert "busy" in response.text


def test_userauth_down_is_a_503(env):
    env.fake.addUser("alice", PASSWORD)
    env.fake.broken = True
    assert env.client().signIn("alice").status == 503
    client = env.client()
    env.fake.broken = False
    client = env.player("bert")
    env.fake.broken = True
    env.social.userauth.forget(client.cookies["__Host-play_at"])
    assert client.api("GET", "/v1/session", origin=FISHE).status == 503


def test_account_page_needs_sign_in_and_shows_username_privately(env):
    anonymous = env.client()
    response = anonymous.request("GET", "/account")
    assert response.status == 303 and response.header("Location").startswith("/signin?")
    client = env.player("jill", "Jill J")
    page = client.request("GET", "/account")
    assert page.status == 200 and "jill" in page.text and "Jill J" in page.text


def test_export_holds_everything(env):
    client = env.player("kate", "Kate K")
    client.api("POST", "/v1/scores/most-money", payload={"value": 10})
    client.api("POST", "/v1/likes/fishe")
    exported = client.request("GET", "/account/export")
    assert exported.status == 200
    assert "attachment" in exported.header("Content-Disposition")
    data = json.loads(exported.text)
    assert data["displayName"] == "Kate K"
    assert data["scores"][0]["value"] == 10 and data["likes"][0]["slug"] == "fishe"
    assert data["submissions"][0]["accepted"] == 1


def test_root_and_unknown_pages(env):
    client = env.client()
    assert client.request("GET", "/").header("Location") == "/account"
    assert client.request("GET", "/nope").status == 404
    assert client.request("GET", "/healthz").text == "ok\n"
