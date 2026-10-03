# @author Daniel McCoy Stephenson
"""The UserAuth client against the fake: errors mapped, cache bounded, refresh
coalesced, the optional X-Forwarded-For."""

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
