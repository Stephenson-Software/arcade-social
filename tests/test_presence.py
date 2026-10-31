# @author Daniel McCoy Stephenson
"""Live presence in the arcade hall (RFC 0030 §4): opt-in, signed-in, in
memory only, 10 s TTL, opaque ids, display names only; deleted, opted-out and
excluded accounts vanish at once."""

import json
import math
import os
import random
import threading

import pytest

from arcade_social import presence as presenceModule
from arcade_social import ratelimit
from helpers import FISHE, HALL, HALL_ARCADE, PASSWORD, PORTAL, SERVICE

LAYOUT = "abc123"


def _pose(x=1.0, z=-6.5, yaw=2.36, cabinet=None, layout=LAYOUT):
    return {"x": x, "z": z, "yaw": yaw, "cabinet": cabinet, "layout": layout}


def _people(client, layout=LAYOUT, origin=HALL):
    response = client.api("GET", "/v1/presence?layout=" + layout, origin=origin)
    assert response.status == 200, response.text
    return response.json()["people"]


def _optedIn(env, username, name):
    client = env.player(username, name)
    assert client.api("POST", "/v1/presence/optin", origin=HALL).json() == {"optedIn": True}
    return client


class Clock(object):
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# --- opt-in ------------------------------------------------------------------------


def test_presence_is_off_until_the_player_opts_in(env):
    alice = env.player("alice", "Alice")
    assert alice.api("GET", "/v1/presence/me", origin=HALL).json() == {"optedIn": False}
    refused = alice.api("POST", "/v1/presence", origin=HALL, payload=_pose())
    assert refused.status == 403 and refused.json()["presence"] == "not-opted-in"
    assert _people(env.client()) == []
    assert alice.api("POST", "/v1/presence/optin", origin=HALL).json() == {"optedIn": True}
    assert alice.api("GET", "/v1/presence/me", origin=HALL_ARCADE).json() == {"optedIn": True}
    assert alice.api("POST", "/v1/presence", origin=HALL, payload=_pose()).status == 200
    assert [person["name"] for person in _people(env.client())] == ["Alice"]
    assert alice.api("DELETE", "/v1/presence/optin", origin=HALL).json() == {"optedIn": False}
    assert _people(env.client()) == []  # opting out removes the live pose at once
    assert alice.api("POST", "/v1/presence", origin=HALL, payload=_pose()).status == 403


def test_signed_out_callers(env):
    client = env.client()
    assert client.api("GET", "/v1/presence/me", origin=HALL).status == 401
    assert client.api("POST", "/v1/presence/optin", origin=HALL).status == 401
    assert client.api("POST", "/v1/presence", origin=HALL, payload=_pose()).status == 401
    # watching needs no account
    assert _people(client) == []


def test_opt_in_needs_a_display_name(env):
    nameless = env.player("nameless", False)
    response = nameless.api("POST", "/v1/presence/optin", origin=HALL)
    assert response.status == 409 and "welcome" in response.json()
    assert nameless.api("GET", "/v1/presence/me", origin=HALL).json() == {"optedIn": False}


def test_the_opt_in_flag_is_persisted_exported_and_deleted(env):
    alice = _optedIn(env, "alice", "Alice")
    assert env.social.store.player("alice")["hall_presence"] == 1
    exported = alice.request("GET", "/account/export")
    assert json.loads(exported.text)["hallPresence"] is True
    # nothing about where the player stood is in the export, or anywhere on disk
    alice.api("POST", "/v1/presence", origin=HALL, payload=_pose(x=123.25, z=-77.75))
    assert "123.25" not in exported.text
    with open(env.config.databasePath, "rb") as handle:
        assert b"123.25" not in handle.read()
    assert alice.api("DELETE", "/v1/me", origin=PORTAL, payload={"confirm": "delete"}).status == 200
    assert env.social.store.player("alice") is None
    assert _people(env.client()) == []
    assert len(env.social.presence) == 0


# --- who may call ------------------------------------------------------------------


def test_only_the_hall_and_the_portal_may_use_presence(env):
    alice = _optedIn(env, "alice", "Alice")
    assert alice.api("POST", "/v1/presence", origin=PORTAL, payload=_pose()).status == 200
    for origin in (FISHE, "https://evil.example", SERVICE, None):
        assert alice.api("POST", "/v1/presence", origin=origin, payload=_pose()).status == 403, origin
        assert alice.api("GET", "/v1/presence?layout=" + LAYOUT, origin=origin).status == 403, origin
        assert alice.api("POST", "/v1/presence/optin", origin=origin).status == 403, origin
    listing = alice.api("GET", "/v1/presence?layout=" + LAYOUT, origin=HALL)
    assert listing.header("Access-Control-Allow-Origin") == HALL
    assert listing.header("Cache-Control") == "no-store"
    ok = alice.request(
        "OPTIONS", "/v1/presence", origin=HALL_ARCADE, headers={"Access-Control-Request-Method": "POST"}
    )
    assert ok.status == 204


# --- what is shared ----------------------------------------------------------------


def test_only_the_display_name_and_a_coarse_pose_are_shared(env):
    alice = _optedIn(env, "alice", "Alice")
    response = alice.api("POST", "/v1/presence", origin=HALL, payload=_pose(x=1.13, z=-6.6, yaw=0.2, cabinet="fishe"))
    assert response.status == 200
    sent = response.json()
    assert sent["ok"] is True and sent["ttl"] == 10
    people = _people(env.client())
    assert len(people) == 1
    person = people[0]
    assert set(person) == {"id", "name", "x", "z", "yaw", "cabinet"}
    assert person["id"] == sent["id"]
    assert (person["name"], person["x"], person["z"], person["cabinet"]) == ("Alice", 1.25, -6.5, "fishe")
    assert abs(person["yaw"] - math.pi / 18) < 1e-3  # 0.2 rad rounds to 10 degrees
    body = json.dumps(people)
    assert "alice" not in body  # never the username
    player = env.social.store.player("alice")
    assert str(player["id"]) != person["id"] and len(person["id"]) >= 12


def test_ids_are_per_presence_session_not_per_player():
    clock = Clock()
    table = presenceModule.Presence(clock=clock)
    first = table.update(7, "Alice", 0, 0, 0, None, LAYOUT)
    clock.now += 5
    assert table.update(7, "Alice", 1, 1, 0, None, LAYOUT) == first  # same visit, same id
    clock.now += 10
    assert table.snapshot(LAYOUT) == []  # expired
    second = table.update(7, "Alice", 0, 0, 0, None, LAYOUT)
    assert second != first  # a new visit cannot be linked to the last
    table.remove(7)
    assert table.update(7, "Alice", 0, 0, 0, None, LAYOUT) not in (first, second)


def test_entries_expire_after_the_ttl():
    clock = Clock()
    table = presenceModule.Presence(clock=clock)
    table.update(1, "A", 0, 0, 0, None, LAYOUT)
    clock.now += 9.9
    assert len(table.snapshot(LAYOUT)) == 1
    clock.now += 0.1
    assert table.snapshot(LAYOUT) == [] and len(table) == 0


def test_the_layout_key_filters_and_is_validated(env):
    alice = _optedIn(env, "alice", "Alice")
    alice.api("POST", "/v1/presence", origin=HALL, payload=_pose(layout="one"))
    assert len(_people(env.client(), layout="one")) == 1
    assert _people(env.client(), layout="two") == []
    for bad in ("", "a" * 65, "has%20space", "sl%2Fash", "x%00"):
        assert env.client().api("GET", "/v1/presence?layout=" + bad, origin=HALL).status == 400, bad
    assert env.client().api("GET", "/v1/presence", origin=HALL).status == 400
    assert alice.api("POST", "/v1/presence", origin=HALL, payload=_pose(layout="")).status == 400


@pytest.mark.parametrize(
    "change",
    [
        {"x": 200.01},
        {"z": -201},
        {"x": "1"},
        {"x": True},
        {"x": None},
        {"yaw": "north"},
        {"yaw": 1e9},
        {"cabinet": "nosuchgame"},
        {"cabinet": "Bad Slug"},
        {"cabinet": 3},
    ],
)
def test_bad_poses_are_refused(env, change):
    alice = _optedIn(env, "alice", "Alice")
    pose = _pose()
    pose.update(change)
    assert alice.api("POST", "/v1/presence", origin=HALL, payload=pose).status == 400
    assert _people(env.client()) == []


def test_non_finite_and_unknown_fields_are_refused(env):
    alice = _optedIn(env, "alice", "Alice")
    raw = '{"x": NaN, "z": 0, "yaw": 0, "cabinet": null, "layout": "abc123"}'
    response = alice.request("POST", "/v1/presence", origin=HALL, body=raw, headers={
        "Content-Type": "application/json", "X-Play-Client": "1"})
    assert response.status == 400
    raw = raw.replace("NaN", "Infinity")
    response = alice.request("POST", "/v1/presence", origin=HALL, body=raw, headers={
        "Content-Type": "application/json", "X-Play-Client": "1"})
    assert response.status == 400
    pose = _pose()
    pose["at"] = "fishe"
    assert alice.api("POST", "/v1/presence", origin=HALL, payload=pose).status == 400
    edge = _pose(x=200, z=-200)
    assert alice.api("POST", "/v1/presence", origin=HALL, payload=edge).status == 200


def test_the_listing_is_bounded_newest_first():
    clock = Clock()
    table = presenceModule.Presence(clock=clock, maxEntries=3)
    for playerId in range(3):
        table.update(playerId, "P%d" % playerId, 0, 0, 0, None, LAYOUT)
        clock.now += 1
    assert [person["name"] for _, person in table.snapshot(LAYOUT)] == ["P2", "P1", "P0"]
    with pytest.raises(presenceModule.Full):
        table.update(99, "X", 0, 0, 0, None, LAYOUT)
    table.update(0, "P0", 0, 0, 0, None, LAYOUT)  # someone already present may still move


def test_a_get_returns_at_most_fifty_people(env):
    store = env.social.store
    players = []
    for index in range(60):
        player = store.ensurePlayer("user%d" % index)
        store.setDisplayName("user%d" % index, "User %d" % index, "user %d" % index, 30, 30)
        store.setHallPresence(player["id"], True)
        players.append(player["id"])
    # all poses at once, after the slow database set-up, so none has expired
    for index, playerId in enumerate(players):
        env.social.presence.update(playerId, "User %d" % index, index % 50, 0, 0, None, LAYOUT)
    assert len(_people(env.client())) == presenceModule.MAX_PEOPLE


# --- vanishing at once ---------------------------------------------------------------


def test_exclusion_removes_a_present_player_and_refuses_them(env):
    bad = _optedIn(env, "bad", "Baddie")
    bad.api("POST", "/v1/presence", origin=HALL, payload=_pose())
    assert len(_people(env.client())) == 1
    boss = env.player("boss", "Boss")
    excluded = boss.api("POST", "/v1/admin/exclude/bad", origin=SERVICE, payload={"reason": "trolling"})
    assert excluded.status == 200
    assert _people(env.client()) == []
    assert len(env.social.presence) == 0
    refused = bad.api("POST", "/v1/presence", origin=HALL, payload=_pose())
    assert refused.status == 403 and refused.json()["presence"] == "excluded"
    again = bad.api("POST", "/v1/presence/optin", origin=HALL)
    assert again.status == 403 and again.json()["presence"] == "excluded"


def test_an_exclusion_made_elsewhere_hides_the_player_on_the_next_listing(env):
    # The admin command line writes the exclusion straight to the database,
    # in another process, so the listing checks it on every read.
    bad = _optedIn(env, "bad", "Baddie")
    bad.api("POST", "/v1/presence", origin=HALL, payload=_pose())
    env.social.store.exclude("bad", None)
    assert _people(env.client()) == []


def test_account_deletion_from_the_page_removes_the_pose(env):
    alice = _optedIn(env, "alice", "Alice")
    alice.api("POST", "/v1/presence", origin=HALL, payload=_pose())
    response = alice.form("/account/delete", {"confirm": "delete"}, csrfPage="/account")
    assert response.status == 200
    assert _people(env.client()) == [] and len(env.social.presence) == 0


def test_leaving_removes_the_pose_but_keeps_the_opt_in(env):
    alice = _optedIn(env, "alice", "Alice")
    alice.api("POST", "/v1/presence", origin=HALL, payload=_pose())
    assert alice.api("DELETE", "/v1/presence", origin=HALL).json() == {"ok": True}
    assert _people(env.client()) == []
    assert alice.api("GET", "/v1/presence/me", origin=HALL).json() == {"optedIn": True}


# --- rate limits ---------------------------------------------------------------------


def test_posts_are_capped_per_player(env):
    alice = _optedIn(env, "alice", "Alice")
    statuses = [alice.api("POST", "/v1/presence", origin=HALL, payload=_pose()).status for _ in range(6)]
    # two per one-second window; six quick posts cannot all pass
    assert statuses.count(429) >= 2 and statuses[0] == 200
    assert ratelimit.PRESENCE_POSTS_PER_PLAYER.count == 2 and ratelimit.PRESENCE_POSTS_PER_PLAYER.seconds == 1


def test_presence_posts_do_not_use_up_the_general_write_budget(env):
    limiter = env.social.limiter
    alice = _optedIn(env, "alice", "Alice")
    alice.api("POST", "/v1/presence", origin=HALL, payload=_pose())
    assert ("writes-ip", alice.ip) in limiter._windows
    before = limiter._windows[("writes-ip", alice.ip)][1]
    alice.api("POST", "/v1/presence", origin=HALL, payload=_pose())
    assert limiter._windows[("writes-ip", alice.ip)][1] == before
    assert ("presence-ip", alice.ip) in limiter._windows


# --- the account page ----------------------------------------------------------------


def test_the_account_page_toggle(env):
    alice = env.player("alice", "Alice")
    page = alice.request("GET", "/account")
    assert "Show me in the arcade hall" in page.text and "Off: nobody sees you" in page.text
    assert "never written to disk or logged" in page.text
    on = alice.form("/account/presence", {"show": "on"}, csrfPage="/account")
    assert on.status == 200 and "You now appear in the arcade hall" in on.text
    assert alice.api("GET", "/v1/presence/me", origin=HALL).json() == {"optedIn": True}
    assert "Stop showing me" in alice.request("GET", "/account").text
    alice.api("POST", "/v1/presence", origin=HALL, payload=_pose())
    off = alice.form("/account/presence", {"show": "off"}, csrfPage="/account")
    assert off.status == 200 and "no longer appear" in off.text
    assert alice.api("GET", "/v1/presence/me", origin=HALL).json() == {"optedIn": False}
    assert _people(env.client()) == []
    # the form's own CSRF rules apply
    forged = alice.form("/account/presence", {"show": "on"}, origin=HALL, csrfPage="/account")
    assert forged.status == 403
    assert alice.api("GET", "/v1/presence/me", origin=HALL).json() == {"optedIn": False}


def test_an_excluded_account_cannot_turn_it_on_from_the_page(env):
    bad = env.player("bad", "Baddie")
    env.social.store.exclude("bad", None)
    response = bad.form("/account/presence", {"show": "on"}, csrfPage="/account")
    assert response.status == 403 and "cannot appear" in response.text
    assert env.social.store.player("bad")["hall_presence"] == 0


# --- concurrency -------------------------------------------------------------------


def test_the_in_memory_table_is_safe_under_concurrent_use():
    table = presenceModule.Presence(maxEntries=10000)
    errors = []
    barrier = threading.Barrier(16)

    def worker(seed):
        rng = random.Random(seed)
        try:
            barrier.wait()
            for step in range(2000):
                playerId = rng.randrange(64)
                action = rng.random()
                if action < 0.6:
                    table.update(playerId, "P%d" % playerId, rng.uniform(-200, 200), rng.uniform(-200, 200),
                                 rng.uniform(-10, 10), None, LAYOUT)
                elif action < 0.8:
                    table.remove(playerId)
                else:
                    people = table.snapshot(LAYOUT)
                    ids = [person["id"] for _, person in people]
                    assert len(ids) == len(set(ids)) <= 64
                    for _, person in people:
                        assert abs(person["x"]) <= 200 and abs(person["z"]) <= 200
        except Exception as e:  # pragma: no cover - reported below
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(seed,)) for seed in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    for playerId in range(64):
        table.update(playerId, "P", 0, 0, 0, None, LAYOUT)
    assert len(table) == 64 and len(table.snapshot(LAYOUT)) == 64


def test_concurrent_posts_and_listings_through_the_server(env):
    clients = [_optedIn(env, "user%d" % index, "User %d" % index) for index in range(6)]
    errors = []

    def poster(client, index):
        for step in range(5):
            response = client.api("POST", "/v1/presence", origin=HALL, payload=_pose(x=index, z=step))
            if response.status not in (200, 429):
                errors.append(response.status)

    def watcher():
        for _ in range(10):
            response = env.client().api("GET", "/v1/presence?layout=" + LAYOUT, origin=HALL)
            if response.status != 200:
                errors.append(response.status)

    threads = [threading.Thread(target=poster, args=(client, index)) for index, client in enumerate(clients)]
    threads += [threading.Thread(target=watcher) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(_people(env.client())) == 6


# --- the kill switch (ARCADE_SOCIAL_HALL_PRESENCE=off) ------------------------------------------


def test_the_kill_switch_empties_the_hall_and_refuses_poses_but_keeps_the_opt_in(env):
    alice = _optedIn(env, "alice", "Alice")
    bob = env.player("bob", "Bob")
    assert alice.api("POST", "/v1/presence", origin=HALL, payload=_pose()).status == 200
    assert len(env.social.presence) == 1
    env.config.hallPresence = "off"
    listing = env.client().api("GET", "/v1/presence?layout=" + LAYOUT, origin=HALL)
    assert listing.status == 200
    assert listing.json() == {"people": [], "ttl": presenceModule.TTL_SECONDS, "disabled": True}
    assert len(env.social.presence) == 0  # the in-memory table is cleared
    # a bad layout is still a bad request
    assert env.client().api("GET", "/v1/presence?layout=", origin=HALL).status == 400
    refused = alice.api("POST", "/v1/presence", origin=HALL, payload=_pose())
    assert refused.status == 503 and refused.json()["disabled"] is True and refused.json()["error"]
    assert refused.header("Retry-After") == "300"
    optIn = bob.api("POST", "/v1/presence/optin", origin=HALL)
    assert optIn.status == 503 and optIn.json()["disabled"] is True
    assert env.social.store.player("bob")["hall_presence"] == 0
    # the opt-in flag on the player row is kept, and reported with the switch
    assert alice.api("GET", "/v1/presence/me", origin=HALL).json() == {"optedIn": True, "disabled": True}
    assert env.social.store.player("alice")["hall_presence"] == 1
    # leaving and opting out still work while it is off
    assert alice.api("DELETE", "/v1/presence", origin=HALL).json() == {"ok": True}
    assert len(env.social.presence) == 0
    # back on: the kept opt-in works again without asking the player twice
    env.config.hallPresence = "on"
    assert alice.api("GET", "/v1/presence/me", origin=HALL).json() == {"optedIn": True}
    assert alice.api("POST", "/v1/presence", origin=HALL, payload=_pose()).status == 200
    assert [person["name"] for person in _people(env.client())] == ["Alice"]
    env.config.hallPresence = "off"
    assert alice.api("DELETE", "/v1/presence/optin", origin=HALL).json() == {"optedIn": False}
    assert env.social.store.player("alice")["hall_presence"] == 0


def test_the_kill_switch_on_the_account_page(env):
    alice = env.player("alice", "Alice")
    env.config.hallPresence = "off"
    page = alice.request("GET", "/account")
    assert "switched off for now" in page.text
    on = alice.form("/account/presence", {"show": "on"}, csrfPage="/account")
    assert on.status == 503 and "switched off" in on.text
    assert env.social.store.player("alice")["hall_presence"] == 0
    off = alice.form("/account/presence", {"show": "off"}, csrfPage="/account")
    assert off.status == 200 and "no longer appear" in off.text
    env.config.hallPresence = "on"
    assert "switched off for now" not in alice.request("GET", "/account").text
