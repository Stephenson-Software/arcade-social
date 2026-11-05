# @author Daniel McCoy Stephenson
"""The UserAuth client against the fake: errors mapped, cache bounded, refresh
coalesced, the optional X-Forwarded-For."""

import base64
import http.client
import json
import socket
import threading

import pytest

from fakeuserauth import FakeUserAuth
from helpers import PASSWORD

from arcade_social import userauth


@pytest.fixture
def fake():
    server = FakeUserAuth().start()
    server.addUser("alice", PASSWORD)
    yield server
    server.stop()


def _jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).rstrip(b"=").decode("ascii")
    return "e30.%s.x" % payload


def _canned(document, status=200):
    return (status, json.dumps(document).encode("utf-8"), ())


def test_login_validate_logout(fake):
    client = userauth.UserAuthClient(fake.url)
    tokens = client.login(" Alice ", PASSWORD)
    assert tokens.username == "alice" and tokens.refresh.startswith("rt-")
    client.forget(tokens.access)
    assert client.validate(tokens.access) == "alice"
    assert client.logout(tokens.access) is True
    assert client.validate(tokens.access) is None


def test_errors_are_typed(fake):
    client = userauth.UserAuthClient(fake.url)
    with pytest.raises(userauth.Rejected):
        client.login("alice", "nope")
    with pytest.raises(userauth.Conflict):
        client.register("alice", PASSWORD)
    with pytest.raises(userauth.Invalid) as error:
        client.register("bob", "weak")
    assert error.value.message.startswith("password:")
    fake.busy = True
    with pytest.raises(userauth.Busy) as busy:
        client.login("alice", PASSWORD)
    assert busy.value.retryAfter == 42
    fake.busy = False
    fake.broken = True
    with pytest.raises(userauth.Unavailable):
        client.login("alice", PASSWORD)
    unreachable = userauth.UserAuthClient("http://127.0.0.1:9", timeout=1)
    with pytest.raises(userauth.Unavailable):
        unreachable.validate("x.y.z")


def test_validation_cache_is_bounded_by_sixty_seconds_and_expiry(fake):
    now = [1000.0]
    client = userauth.UserAuthClient(fake.url, cacheSeconds=600, clock=lambda: now[0])
    assert client.cacheSeconds == 60
    tokens = client.login("alice", PASSWORD)
    calls = fake.count("/session/validate")
    assert client.validate(tokens.access) == "alice"
    assert fake.count("/session/validate") == calls
    now[0] += 61
    assert client.validate(tokens.access) == "alice"
    assert fake.count("/session/validate") == calls + 1


def test_refresh_rotates_and_is_coalesced(fake):
    client = userauth.UserAuthClient(fake.url)
    tokens = client.login("alice", PASSWORD)
    first = client.refresh(tokens.refresh)
    again = client.refresh(tokens.refresh)  # the same old token, within the minute
    assert first is again and first.refresh != tokens.refresh
    assert fake.count("/token/refresh") == 1
    other = userauth.UserAuthClient(fake.url)
    with pytest.raises(userauth.Rejected):
        other.refresh(tokens.refresh)  # single-use at UserAuth


def test_forwarded_for_only_when_enabled(fake):
    userauth.UserAuthClient(fake.url).login("alice", PASSWORD, clientIp="192.0.2.1")
    userauth.UserAuthClient(fake.url, forwardClientIp=True).login("alice", PASSWORD, clientIp="192.0.2.1")
    logins = [headers for _, path, headers in fake.calls if path == "/login"]
    assert "X-Forwarded-For" not in logins[0]
    assert logins[1]["X-Forwarded-For"] == "192.0.2.1"


def test_token_claims_requires_a_subject():
    with pytest.raises(userauth.Unavailable):
        userauth.tokenClaims("not-a-jwt")
    with pytest.raises(userauth.Unavailable):
        userauth.tokenClaims("e30.e30.x")  # {} claims


@pytest.mark.parametrize(
    "body",
    [b"", b"<html>not json</html>", b"\xff\xfe", b"[1, 2]", b'{"message": 5}'],
    ids=["empty", "not-json", "not-utf8", "not-an-object", "message-not-text"],
)
@pytest.mark.parametrize(
    "status, error, message",
    [
        (400, userauth.Invalid, "UserAuth refused the request"),
        (401, userauth.Rejected, "unauthorized"),
        (409, userauth.Conflict, "already exists"),
    ],
)
def test_an_unusable_error_body_still_gives_the_typed_error(fake, body, status, error, message):
    fake.canned["/login"] = (status, body, ())
    with pytest.raises(error) as raised:
        userauth.UserAuthClient(fake.url).login("alice", PASSWORD)
    assert raised.value.message == message and raised.value.status == status


@pytest.mark.parametrize(
    "headers, wait",
    [
        ((("Retry-After", "7"),), 7),
        ((), 60),
        ((("Retry-After", "soon"),), 60),
        ((("Retry-After", "Wed, 21 Oct 2026 07:28:00 GMT"),), 60),
    ],
    ids=["seconds", "absent", "not-a-number", "http-date"],
)
def test_busy_waits_sixty_seconds_unless_told_a_number(fake, headers, wait):
    fake.canned["/login"] = (429, b"", headers)
    with pytest.raises(userauth.Busy) as busy:
        userauth.UserAuthClient(fake.url).login("alice", PASSWORD)
    assert busy.value.retryAfter == wait


@pytest.mark.parametrize("status", [403, 404, 503])
def test_any_other_status_is_unavailable(fake, status):
    fake.canned["/login"] = _canned({"message": "not for the player"}, status)
    with pytest.raises(userauth.Unavailable) as raised:
        userauth.UserAuthClient(fake.url).login("alice", PASSWORD)
    assert raised.value.status == status and "not for the player" not in raised.value.message


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"token": "a.b.c"},
        {"refreshToken": "rt-1"},
        {"token": "", "refreshToken": "rt-1"},
        {"token": _jwt({"sub": "alice"}), "refreshToken": ""},
        {"token": 5, "refreshToken": "rt-1"},
        {"token": "not-a-jwt", "refreshToken": "rt-1"},
        {"token": _jwt({"exp": 9999999999}), "refreshToken": "rt-1"},
    ],
    ids=[
        "empty",
        "no-refresh",
        "no-token",
        "blank-token",
        "blank-refresh",
        "token-not-text",
        "not-a-jwt",
        "no-sub",
    ],
)
def test_a_success_without_usable_tokens_is_unavailable(fake, document):
    fake.canned["/login"] = _canned(document)
    with pytest.raises(userauth.Unavailable):
        userauth.UserAuthClient(fake.url).login("alice", PASSWORD)


@pytest.mark.parametrize(
    "claims",
    [{"sub": "Alice"}, {"sub": "Alice", "exp": "tomorrow"}],
    ids=["none", "not-a-number"],
)
def test_a_token_without_a_numeric_expiry_gets_an_hour(fake, claims):
    fake.canned["/login"] = _canned({"token": _jwt(claims), "refreshToken": "rt-1"})
    tokens = userauth.UserAuthClient(fake.url, clock=lambda: 1000.0).login("alice", PASSWORD)
    assert tokens.username == "alice" and tokens.expiresAt == 4600.0


def test_refresh_results_are_kept_for_sixty_seconds_only(fake):
    now = [1000.0]
    client = userauth.UserAuthClient(fake.url, clock=lambda: now[0])
    tokens = client.login("alice", PASSWORD)
    first = client.refresh(tokens.refresh)
    now[0] += 59
    assert client.refresh(tokens.refresh) is first
    assert fake.count("/token/refresh") == 1
    now[0] += 1
    with pytest.raises(userauth.Rejected):
        client.refresh(tokens.refresh)  # asked again, and single-use at UserAuth
    assert fake.count("/token/refresh") == 2


def test_a_failed_refresh_is_not_kept(fake):
    client = userauth.UserAuthClient(fake.url)
    tokens = client.login("alice", PASSWORD)
    fake.broken = True
    with pytest.raises(userauth.Unavailable):
        client.refresh(tokens.refresh)
    fake.broken = False
    assert client.refresh(tokens.refresh).refresh != tokens.refresh


@pytest.mark.parametrize(
    "document",
    [
        {"valid": False, "username": "alice"},
        {"valid": "true", "username": "alice"},
        {"username": "alice"},
        {"valid": True},
        {"valid": True, "username": ""},
        {"valid": True, "username": 5},
    ],
    ids=[
        "invalid",
        "valid-not-boolean",
        "valid-absent",
        "no-username",
        "blank-username",
        "username-not-text",
    ],
)
def test_validate_accepts_only_an_explicit_yes(fake, document):
    fake.canned["/session/validate"] = _canned(document)
    client = userauth.UserAuthClient(fake.url)
    assert client.validate(_jwt({"sub": "alice"})) is None
    assert client.validate(_jwt({"sub": "alice"})) is None
    assert fake.count("/session/validate") == 2  # a refusal is never cached


def test_validate_normalises_the_username_and_caches_an_opaque_token_for_a_minute(fake):
    fake.canned["/session/validate"] = _canned({"valid": True, "username": "Alice"})
    now = [1000.0]
    client = userauth.UserAuthClient(fake.url, clock=lambda: now[0])
    assert client.validate("not-a-jwt") == "alice"
    now[0] += 59
    assert client.validate("not-a-jwt") == "alice"
    assert fake.count("/session/validate") == 1
    now[0] += 1
    assert client.validate("not-a-jwt") == "alice"
    assert fake.count("/session/validate") == 2


def test_the_cache_ends_when_the_token_does(fake):
    now = [1000.0]
    fake.canned["/session/validate"] = _canned({"valid": True, "username": "alice"})
    client = userauth.UserAuthClient(fake.url, clock=lambda: now[0])
    token = _jwt({"sub": "alice", "exp": 1010})
    assert client.validate(token) == "alice"
    now[0] += 9
    assert client.validate(token) == "alice"
    assert fake.count("/session/validate") == 1
    now[0] += 1
    assert client.validate(token) == "alice"
    assert fake.count("/session/validate") == 2


def test_a_revoked_token_stops_being_cached(fake):
    now = [1000.0]
    client = userauth.UserAuthClient(fake.url, clock=lambda: now[0])
    token = _jwt({"sub": "alice", "exp": 9999999999})
    fake.canned["/session/validate"] = _canned({"valid": True, "username": "alice"})
    assert client.validate(token) == "alice"
    fake.canned["/session/validate"] = _canned({"message": "token is not valid"}, 401)
    assert client.validate(token) == "alice"  # still inside the minute RFC 0013 allows
    now[0] += 60
    assert client.validate(token) is None


@pytest.mark.parametrize("status", [401, 500])
def test_logout_is_best_effort_and_always_forgets_the_token(fake, status):
    client = userauth.UserAuthClient(fake.url)
    tokens = client.login("alice", PASSWORD)
    fake.canned["/logout"] = _canned({}, status)
    assert client.logout(tokens.access) is False
    calls = fake.count("/session/validate")
    client.validate(tokens.access)
    assert fake.count("/session/validate") == calls + 1


def test_logout_survives_userauth_being_unreachable():
    assert userauth.UserAuthClient("http://127.0.0.1:9", timeout=1).logout("x.y.z") is False


def test_a_full_cache_drops_expired_entries_first():
    now = [1000.0]
    client = userauth.UserAuthClient("http://127.0.0.1:9", timeout=1, clock=lambda: now[0])
    client._remember("long", "bob", now[0] + 30)
    for index in range(10000):
        client._remember("short-%d" % index, "alice", now[0] + 1)
    now[0] += 2
    client._remember("newest", "carol", now[0] + 30)
    # No request is made for either: an unreachable UserAuth would raise.
    assert client.validate("long") == "bob"
    assert client.validate("newest") == "carol"


def test_a_full_cache_of_live_entries_is_emptied():
    now = [1000.0]
    client = userauth.UserAuthClient("http://127.0.0.1:9", timeout=1, clock=lambda: now[0])
    for index in range(10001):
        client._remember("live-%d" % index, "alice", now[0] + 30)
    assert client.validate("live-0") == "alice"
    client._remember("one-more", "bob", now[0] + 30)
    assert client.validate("one-more") == "bob"
    with pytest.raises(userauth.Unavailable):
        client.validate("live-0")  # gone from the cache, so UserAuth is asked


def _answerOnce(response):
    """A one-shot listener that answers any request with these raw bytes."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve():
        connection, _ = listener.accept()
        connection.recv(65536)
        connection.sendall(response)
        # Finish reading before closing: closing with the request's body unread
        # would reset the connection and hide what this reply does to the client.
        connection.shutdown(socket.SHUT_WR)
        while connection.recv(65536):
            pass
        connection.close()
        listener.close()

    threading.Thread(target=serve, daemon=True).start()
    return "http://127.0.0.1:%d" % listener.getsockname()[1]


@pytest.mark.xfail(
    raises=http.client.HTTPException,
    strict=True,
    reason="issue #26: _call lets http.client.HTTPException escape, so the server answers 500, not 503",
)
@pytest.mark.parametrize(
    "response",
    [b"garbage\r\n\r\n", b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{}"],
    ids=["not-http", "truncated-body"],
)
def test_a_reply_that_is_not_valid_http_is_unavailable(response):
    client = userauth.UserAuthClient(_answerOnce(response), timeout=5)
    with pytest.raises(userauth.Unavailable):
        client.login("alice", PASSWORD)
