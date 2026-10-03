# @author Daniel McCoy Stephenson
"""Cloud saves over HTTP (RFC 0016 §4, §7, §8, §Verification "Server"):
every upload rule, Origin isolation, the kill switch and per-game switch,
the account allowlist, deletion across both files, and the account page."""

import json
import threading

import pytest

from helpers import FISHE, FROG, PORTAL, SERVICE, TIDEWATER, TIDEWATER_ALIAS, Env

from arcade_social import ratelimit
from arcade_social.ratelimit import Limit

STORE = "fishe-saves"
BASE = "/v1/saves/" + STORE


@pytest.fixture
def senv(tmp_path):
    environment = Env(tmp_path, savesMode="on", savesAccounts=None)
    yield environment
    environment.stop()


def saveFile(files, game=STORE, **override):
    document = {"format": "tak-saves", "version": 1, "game": game, "exported": "2026-10-03T00:00:00Z", "files": files}
    document.update(override)
    return document


def body(parent, files, game=STORE, **extra):
    payload = {"parent": parent, "device": "device-0001", "deviceLabel": "Test browser", "file": saveFile(files, game)}
    payload.update(extra)
    return payload


def slots(**contents):
    return dict(("/saves/%s/save.json" % name, value) for name, value in contents.items())


def enrolled(env, username="alice", origin=FISHE, store=STORE):
    client = env.player(username)
    response = client.api("POST", "/v1/saves/%s/enroll" % store, origin=origin)
    assert response.status == 200, response.text
    return client


def counts(env):
    return env.social.savesStore.counts()


def head(client, origin=FISHE, store=STORE):
    response = client.api("GET", "/v1/saves/" + store, origin=origin)
    assert response.status == 200, response.text
    return response.json()["head"]


# --- the kill switch ----------------------------------------------------------------


def test_off_by_default_every_endpoint_answers_503(env):
    client = env.player("alice")
    for method, path in (
        ("GET", BASE),
        ("PUT", BASE),
        ("DELETE", BASE),
        ("POST", BASE + "/enroll"),
        ("DELETE", BASE + "/enroll"),
        ("GET", BASE + "/versions"),
        ("GET", BASE + "/versions/1"),
        ("POST", BASE + "/versions/1/pin"),
    ):
        response = client.api(method, path, payload={} if method != "GET" else None)
        assert response.status == 503, (method, path, response.text)
        assert response.json()["saves"] == "off"
        assert "head" not in response.json()


def test_readonly_keeps_reads_downloads_and_deletion(senv):
    client = enrolled(senv)
    first = client.api("PUT", BASE, payload=body(None, slots(slot_1="one")))
    assert first.status == 201
    versionId = first.json()["id"]
    senv.social.saves.policy.mode = "readonly"
    refused = client.api("PUT", BASE, payload=body(versionId, slots(slot_1="two")))
    assert refused.status == 503 and refused.json()["saves"] == "paused"
    assert client.api("POST", BASE + "/enroll").status == 503
    assert client.api("POST", BASE + "/versions/%d/pin" % versionId).status == 503
    status = client.api("GET", BASE)
    assert status.status == 200
    assert status.json()["head"]["id"] == versionId and status.json()["writable"] is False
    assert client.api("GET", BASE + "/versions").status == 200
    assert client.api("GET", BASE + "/versions/%d" % versionId).json()["files"] == slots(slot_1="one")
    assert client.api("DELETE", BASE + "/enroll").status == 200
    assert client.api("DELETE", BASE, payload={"confirm": "delete"}).json()["deleted"] == 1


def test_a_readonly_or_unlisted_game_keeps_what_it_has(senv):
    client = enrolled(senv)
    versionId = client.api("PUT", BASE, payload=body(None, slots(slot_1="one"))).json()["id"]
    frog = client.api("POST", "/v1/saves/frog-saves/enroll", origin=FROG)
    assert frog.status == 503 and frog.json()["saves"] == "paused"
    # fishe leaves saves.yaml: read-only, and its versions are still reachable.
    with open(senv.savesPath, "w") as handle:
        handle.write("games: {}\n")
    import os
    import time

    os.utime(senv.savesPath, (time.time() + 5, time.time() + 5))
    status = client.api("GET", BASE)
    assert status.status == 200 and status.json()["writable"] is False
    assert status.json()["head"]["id"] == versionId
    assert client.api("GET", BASE + "/versions/%d" % versionId).json()["files"] == slots(slot_1="one")
    assert client.api("PUT", BASE, payload=body(versionId, slots(slot_1="two"))).status == 503


def test_the_account_allowlist(tmp_path):
    env = Env(tmp_path, savesMode="on", savesAccounts=frozenset(["alice"]))
    try:
        alice = enrolled(env, "alice")
        assert alice.api("GET", BASE).json()["allowed"] is True
        bob = env.player("bob")
        assert bob.api("GET", BASE).json()["allowed"] is False
        refused = bob.api("POST", BASE + "/enroll")
        assert refused.status == 403 and refused.json()["saves"] == "not-allowed"
        upload = bob.api("PUT", BASE, payload=body(None, slots(slot_1="x")))
        assert upload.status == 403 and upload.json()["saves"] == "not-allowed"
    finally:
        env.stop()


def test_an_empty_allowlist_is_nobody(tmp_path):
    env = Env(tmp_path, savesMode="on", savesAccounts=frozenset())
    try:
        client = env.player("alice")
        assert client.api("POST", BASE + "/enroll").json()["saves"] == "not-allowed"
    finally:
        env.stop()


def test_a_full_database_refuses_uploads_for_everyone(senv):
    client = enrolled(senv)
    senv.social.saves.policy.databaseMaxBytes = 1
    refused = client.api("PUT", BASE, payload=body(None, slots(slot_1="one")))
    assert refused.status == 503 and refused.json()["saves"] == "full"


# --- status, enrollment -------------------------------------------------------------------


def test_signed_out_and_not_enrolled(senv):
    anonymous = senv.client()
    assert anonymous.api("GET", BASE).status == 401
    client = senv.player("alice")
    status = client.api("GET", BASE).json()
    assert status["enrolled"] is False and "head" not in status
    assert client.api("PUT", BASE, payload=body(None, slots(slot_1="x"))).json()["saves"] == "not-enrolled"
    assert client.api("POST", BASE + "/enroll").json()["newlyEnrolled"] is True
    assert client.api("POST", BASE + "/enroll").json()["newlyEnrolled"] is False
    status = client.api("GET", BASE).json()
    # head null means "enrolled, nothing uploaded" - and only ever with 200 + enrolled.
    assert status["enrolled"] is True and status["head"] is None and status["pull"] is True


def test_stop_backing_up_keeps_the_versions(senv):
    client = enrolled(senv)
    client.api("PUT", BASE, payload=body(None, slots(slot_1="one")))
    assert client.api("DELETE", BASE + "/enroll").json()["enrolled"] is False
    assert counts(senv)["version"] == 1
    assert client.api("PUT", BASE, payload=body(1, slots(slot_1="two"))).json()["saves"] == "not-enrolled"


# --- the upload rules -------------------------------------------------------------------------


def test_upload_and_download_round_trip(senv):
    client = enrolled(senv)
    files = slots(slot_1="one", slot_2="two")
    files["/saves/slot_2/meta.bin"] = {"base64": "AAEC/w=="}
    files["/saves/settings.json"] = "{}"
    created = client.api("PUT", BASE, payload=body(None, files, kind="enroll"))
    assert created.status == 201 and created.json()["created"] is True
    versionId = created.json()["id"]
    current = head(client)
    assert current["id"] == versionId and current["parent"] is None and current["kind"] == "enroll"
    assert [unit["name"] for unit in current["units"]] == ["settings.json", "slot_1", "slot_2"]
    assert current["deviceLabel"] == "Test browser" and current["origin"] == FISHE
    download = client.api("GET", BASE + "/versions/%d?download=1" % versionId)
    assert "attachment" in download.header("Content-Disposition")
    document = download.json()
    assert document["files"] == files
    assert document["format"] == "tak-saves" and document["version"] == 1 and document["game"] == STORE
    # The same content again stores nothing.
    before = counts(senv)
    same = client.api("PUT", BASE, payload=body(versionId, files))
    assert same.status == 200 and same.json() == {"created": False, "id": versionId}
    assert counts(senv) == before


def test_a_stale_parent_is_409_and_writes_nothing(senv):
    client = enrolled(senv)
    first = client.api("PUT", BASE, payload=body(None, slots(slot_1="one"))).json()["id"]
    second = client.api("PUT", BASE, payload=body(first, slots(slot_1="two"))).json()["id"]
    before = counts(senv)
    for parent in (first, None, 999):
        stale = client.api("PUT", BASE, payload=body(parent, slots(slot_1="three")))
        assert stale.status == 409, parent
        assert stale.json()["saves"] == "stale" and stale.json()["head"]["id"] == second
    assert counts(senv) == before
    assert head(client)["id"] == second


def test_dropping_a_unit_needs_confirm_removed(senv):
    client = enrolled(senv)
    first = client.api("PUT", BASE, payload=body(None, slots(slot_1="one", slot_2="two", slot_3="three"))).json()["id"]
    before = counts(senv)
    dropped = client.api("PUT", BASE, payload=body(first, slots(slot_1="one", slot_2="two")))
    assert dropped.status == 422
    assert dropped.json()["saves"] == "removed" and dropped.json()["removed"] == ["slot_3"]
    wrong = client.api("PUT", BASE, payload=body(first, slots(slot_1="one", slot_2="two"), confirmRemoved=["slot_2"]))
    assert wrong.status == 422 and wrong.json()["saves"] == "confirm-mismatch"
    extra = client.api(
        "PUT", BASE, payload=body(first, slots(slot_1="one", slot_2="two"), confirmRemoved=["slot_3", "slot_1"])
    )
    assert extra.status == 422
    nothingDropped = client.api(
        "PUT", BASE, payload=body(first, slots(slot_1="1", slot_2="2", slot_3="3"), confirmRemoved=["slot_3"])
    )
    assert nothingDropped.status == 422
    assert counts(senv) == before
    ok = client.api("PUT", BASE, payload=body(first, slots(slot_1="one", slot_2="two"), confirmRemoved=["slot_3"]))
    assert ok.status == 201
    assert [unit["name"] for unit in head(client)["units"]] == ["slot_1", "slot_2"]
    assert head(client)["removed"] == ["slot_3"]
    # History keeps the deleted unit.
    assert client.api("GET", BASE + "/versions/%d" % first).json()["files"]["/saves/slot_3/save.json"] == "three"


def test_an_empty_upload_never_replaces_saves(senv):
    client = enrolled(senv)
    first = client.api("PUT", BASE, payload=body(None, slots(slot_1="one"))).json()["id"]
    empty = client.api("PUT", BASE, payload=body(first, {}))
    assert empty.status == 422 and empty.json()["removed"] == ["slot_1"]
    # Before anything was uploaded, an empty store stores nothing.
    other = enrolled(senv, "bob")
    nothing = other.api("PUT", BASE, payload=body(None, {}))
    assert nothing.status == 200 and nothing.json()["id"] is None
    assert other.api("GET", BASE).json()["head"] is None


def test_halving_the_saves_needs_confirm_shrink(senv):
    client = enrolled(senv)
    first = client.api("PUT", BASE, payload=body(None, slots(slot_1="x" * 2000, slot_2="y" * 2000))).json()["id"]
    shrink = client.api("PUT", BASE, payload=body(first, slots(slot_1="x", slot_2="y")))
    assert shrink.status == 422 and shrink.json()["saves"] == "shrink"
    ok = client.api("PUT", BASE, payload=body(first, slots(slot_1="x", slot_2="y"), confirmShrink=True))
    assert ok.status == 201


def test_quota_refuses_and_never_prunes(senv):
    client = enrolled(senv)
    parent = None
    statuses = []
    for index in range(6):
        response = client.api("PUT", BASE, payload=body(parent, slots(slot_1=("%d" % index) * 150000)))
        statuses.append(response.status)
        if response.status == 201:
            parent = response.json()["id"]
        else:
            assert response.status == 413 and response.json()["saves"] == "quota"
    # Three ~150 KB versions fit in 600000 bytes; the fourth would not.
    assert statuses == [201, 201, 201, 413, 413, 413]
    assert counts(senv)["version"] == 3
    assert client.api("GET", BASE).json()["quota"]["storedBytes"] <= 600000


def test_the_player_wide_quota(tmp_path):
    env = Env(tmp_path, savesMode="on", savesAccounts=None, savesPlayerMaxBytes=250000)
    try:
        client = enrolled(env)
        first = client.api("PUT", BASE, payload=body(None, slots(slot_1="a" * 150000)))
        assert first.status == 201
        client.api("POST", "/v1/saves/tidewater-saves/enroll", origin=TIDEWATER)
        second = client.api(
            "PUT", "/v1/saves/tidewater-saves", origin=TIDEWATER, payload=body(None, slots(slot_1="b" * 150000), game="tidewater-saves")
        )
        assert second.status == 413
    finally:
        env.stop()


def test_a_body_over_the_upload_limit_is_refused_unread(senv):
    client = enrolled(senv)
    response = client.api("PUT", BASE, payload=body(None, slots(slot_1="z" * 300000)))
    assert response.status == 413 and response.json()["saves"] == "too-large"
    assert counts(senv)["version"] == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["file"].update(format="roam-saves"),
        lambda b: b["file"].update(version=2),
        lambda b: b["file"].update(version="1"),
        lambda b: b["file"].update(game="tidewater-saves"),
        lambda b: b["file"].update(files=[]),
        lambda b: b["file"]["files"].update({"/other/slot_1/save.json": "x"}),
        lambda b: b["file"]["files"].update({"/saves/../etc/passwd": "x"}),
        lambda b: b["file"]["files"].update({"/saves/slot_1//save.json": "x"}),
        lambda b: b["file"]["files"].update({"/saves/slot_1/\u0001": "x"}),
        lambda b: b["file"]["files"].update({"/saves/slot_1/a\\b": "x"}),
        lambda b: b["file"]["files"].update({"/saves/slot_2/save.json": {"base64": "not base64!"}}),
        lambda b: b["file"]["files"].update({"/saves/slot_2/save.json": {"base64": "AA=="}, "/saves/slot_3/x": 3}),
        lambda b: b["file"]["files"].update({"/saves/slot_2/save.json": {"base64": "AA==", "extra": 1}}),
        lambda b: b.update(device="x"),
        lambda b: b.update(kind="overwrite"),
        lambda b: b.update(parent=True),
        lambda b: b.update(confirmRemoved="slot_1"),
        lambda b: b.update(surprise=1),
        lambda b: b.pop("file"),
    ],
)
def test_invalid_uploads_are_400_and_store_nothing(senv, mutate):
    client = enrolled(senv)
    payload = body(None, slots(slot_1="one"))
    mutate(payload)
    response = client.api("PUT", BASE, payload=payload)
    assert response.status == 400, response.text
    assert response.json()["saves"] == "invalid"
    assert counts(senv)["version"] == 0


def test_lone_surrogates_are_refused(senv):
    client = enrolled(senv)
    raw = json.dumps(body(None, slots(slot_1="placeholder"))).replace("placeholder", "\\ud800")
    response = client.request(
        "PUT", BASE, origin=FISHE, body=raw, headers={"Content-Type": "application/json", "X-Play-Client": "1"}
    )
    assert response.status == 400


def test_racing_uploads_one_wins_the_other_gets_409(senv):
    client = enrolled(senv)
    first = client.api("PUT", BASE, payload=body(None, slots(slot_1="one"))).json()["id"]
    results = []

    def upload(text):
        results.append(client.api("PUT", BASE, payload=body(first, slots(slot_1=text))).status)

    threads = [threading.Thread(target=upload, args=("racer-%d" % n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [201, 409, 409, 409]
    assert counts(senv)["version"] == 2


def test_the_hourly_upload_cap(senv, monkeypatch):
    monkeypatch.setattr(ratelimit, "SAVES_UPLOADS_PER_GAME", Limit("saves-uploads-test", 2, 3600))
    client = enrolled(senv)
    parent = None
    for text in ("a", "b"):
        parent = client.api("PUT", BASE, payload=body(parent, slots(slot_1=text))).json()["id"]
    capped = client.api("PUT", BASE, payload=body(parent, slots(slot_1="c")))
    assert capped.status == 429 and capped.header("Retry-After")


def test_pin(senv):
    client = enrolled(senv)
    versionId = client.api("PUT", BASE, payload=body(None, slots(slot_1="one"))).json()["id"]
    assert client.api("POST", BASE + "/versions/%d/pin" % versionId).json()["pinned"] is True
    assert client.api("GET", BASE + "/versions").json()["versions"][0]["pinned"] is True
    assert client.api("POST", BASE + "/versions/999/pin").status == 404


# --- who may see what -------------------------------------------------------------------------


def test_origin_isolation(senv):
    alice = enrolled(senv)
    versionId = alice.api("PUT", BASE, payload=body(None, slots(slot_1="one"))).json()["id"]
    # Another game's page names fishe's store: not its own, so not found.
    other = alice.api("GET", BASE, origin=TIDEWATER)
    assert other.status == 404
    assert alice.api("GET", BASE + "/versions/%d" % versionId, origin=TIDEWATER).status == 404
    assert alice.api("GET", "/v1/saves/tidewater-saves/versions/%d" % versionId, origin=TIDEWATER).status == 404
    assert alice.api("PUT", BASE, origin=TIDEWATER, payload=body(versionId, slots(slot_1="x"))).status == 404
    # The portal and foreign origins get nothing.
    assert alice.api("GET", BASE, origin=PORTAL).status == 403
    assert alice.api("GET", BASE, origin="https://evil.example").status == 403
    assert alice.api("GET", BASE, origin=None).status == 403
    assert alice.api("DELETE", BASE, origin=PORTAL, payload={"confirm": "delete"}).status == 403
    # Another player cannot read alice's version by id.
    bob = enrolled(senv, "bob")
    assert bob.api("GET", BASE + "/versions/%d" % versionId).status == 404
    assert bob.api("GET", BASE).json()["head"] is None
    assert bob.api("GET", BASE + "/versions").json()["versions"] == []


def test_an_alias_origin_reaches_its_games_namespace(senv):
    client = enrolled(senv, origin=TIDEWATER, store="tidewater-saves")
    path = "/v1/saves/tidewater-saves"
    versionId = client.api("PUT", path, origin=TIDEWATER, payload=body(None, slots(slot_1="one"), game="tidewater-saves")).json()["id"]
    viaAlias = client.api("GET", path, origin=TIDEWATER_ALIAS)
    assert viaAlias.status == 200 and viaAlias.json()["head"]["id"] == versionId
    stale = client.api("PUT", path, origin=TIDEWATER_ALIAS, payload=body(None, slots(slot_1="alias"), game="tidewater-saves"))
    assert stale.status == 409  # the alias store meets the slug store: keep-both happens in the page


def test_preflight_allows_put_from_a_game(senv):
    response = senv.client().request(
        "OPTIONS", BASE, origin=FISHE, headers={"Access-Control-Request-Method": "PUT"}
    )
    assert response.status == 204
    assert "PUT" in response.header("Access-Control-Allow-Methods")


# --- deletion -------------------------------------------------------------------------------------


def test_deleting_one_games_cloud_saves(senv):
    client = enrolled(senv)
    client.api("PUT", BASE, payload=body(None, slots(slot_1="one")))
    assert client.api("DELETE", BASE, payload={}).status == 400
    assert client.api("DELETE", BASE, payload={"confirm": "delete"}).json()["deleted"] == 1
    assert counts(senv) == {"blob": 0, "enrollment": 0, "head": 0, "unit": 0, "version": 0}


def test_delete_me_removes_saves_from_both_files(senv):
    client = enrolled(senv)
    client.api("PUT", BASE, payload=body(None, slots(slot_1="one")))
    playerId = senv.social.store.player("alice")["id"]
    assert senv.social.savesStore.references(playerId) > 0
    assert client.api("DELETE", "/v1/me", payload={"confirm": "delete"}).status == 200
    assert senv.social.savesStore.references(playerId) == 0
    assert senv.social.store.player("alice") is None


def test_a_failure_between_the_two_files_leaves_the_saves_deletable(senv, monkeypatch):
    client = enrolled(senv)
    client.api("PUT", BASE, payload=body(None, slots(slot_1="one")))
    playerId = senv.social.store.player("alice")["id"]
    original = senv.social.store.deletePlayer

    def broken(username):
        raise RuntimeError("injected")

    monkeypatch.setattr(senv.social.store, "deletePlayer", broken)
    assert client.api("DELETE", "/v1/me", payload={"confirm": "delete"}).status == 500
    # The saves went first; the player row is still there to find and retry.
    assert senv.social.savesStore.references(playerId) == 0
    assert senv.social.store.player("alice") is not None
    monkeypatch.setattr(senv.social.store, "deletePlayer", original)
    assert client.api("DELETE", "/v1/me", payload={"confirm": "delete"}).status == 200


# --- the account page --------------------------------------------------------------------------------


def test_account_saves_page_download_and_delete(senv):
    client = enrolled(senv)
    files = slots(slot_1="one")
    versionId = client.api("PUT", BASE, payload=body(None, files)).json()["id"]
    page = client.request("GET", "/account/saves")
    assert page.status == 200 and "Fishe" in page.text or "FishE" in page.text
    assert "script" not in page.text.lower()
    link = "/account/saves/download?slug=fishe&store=fishe-saves&id=%d" % versionId
    assert link.replace("&", "&amp;") in page.text
    download = client.request("GET", link)
    assert download.status == 200 and json.loads(download.text)["files"] == files
    assert "attachment" in download.header("Content-Disposition")
    other = senv.player("bob")
    assert other.request("GET", link).status == 404
    refused = client.form("/account/saves/delete", {"slug": "fishe", "store": STORE, "confirm": "nope"}, csrfPage="/account/saves")
    assert refused.status == 400 and counts(senv)["version"] == 1
    deleted = client.form("/account/saves/delete", {"slug": "fishe", "store": STORE, "confirm": "delete"}, csrfPage="/account/saves")
    assert deleted.status == 200 and "Deleted 1 cloud version" in deleted.text
    assert counts(senv)["version"] == 0


def test_account_export_lists_cloud_saves(senv):
    client = enrolled(senv)
    client.api("PUT", BASE, payload=body(None, slots(slot_1="one")))
    exported = json.loads(client.request("GET", "/account/export").text)
    assert exported["cloudSaves"][0]["slug"] == "fishe"
    assert exported["cloudSaves"][0]["versions"][0]["unitCount"] == 1


def test_account_page_says_when_saves_are_off(env):
    client = env.player("alice")
    assert "unavailable" in client.request("GET", "/account/saves").text


def test_account_delete_form_also_deletes_cloud_saves(senv):
    client = enrolled(senv)
    client.api("PUT", BASE, payload=body(None, slots(slot_1="one")))
    playerId = senv.social.store.player("alice")["id"]
    response = client.form("/account/delete", {"confirm": "delete"}, csrfPage="/account")
    assert response.status == 200
    assert senv.social.savesStore.references(playerId) == 0


def test_an_online_backup_during_a_burst_of_uploads_restores_identical_heads(senv, tmp_path):
    from arcade_social.saves_store import SavesStore

    clients = [enrolled(senv, name) for name in ("alice", "bob", "carol")]
    stop = threading.Event()

    def burst(client):
        parent = None
        count = 0
        while not stop.is_set() and count < 40:
            count += 1
            response = client.api("PUT", BASE, payload=body(parent, slots(slot_1="v%d" % count, slot_2="w" * 500)))
            if response.status == 201:
                parent = response.json()["id"]

    threads = [threading.Thread(target=burst, args=(client,)) for client in clients]
    for thread in threads:
        thread.start()
    copies = []
    for index in range(3):
        destination = str(tmp_path / ("backup-%d.sqlite3" % index))
        senv.social.savesStore.backup(destination)
        copies.append(destination)
    stop.set()
    for thread in threads:
        thread.join()
    for destination in copies:
        assert SavesStore(destination).integrityCheck() == "ok"
    final = str(tmp_path / "final.sqlite3")
    senv.social.savesStore.backup(final)
    restored = SavesStore(final)
    for name in ("alice", "bob", "carol"):
        playerId = senv.social.store.player(name)["id"]
        assert restored.head(playerId, "fishe", STORE) == senv.social.savesStore.head(playerId, "fishe", STORE)
