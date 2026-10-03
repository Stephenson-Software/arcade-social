# @author Daniel McCoy Stephenson
"""The randomized protocol test of RFC 0016 §Verification.

Three simulated devices of one player play random histories against the real
server rules (saves.Saves over a real SQLite file): saves, deliberate
deletions, offline spells, failed restores, imports, kill-switch flips, cleared
site data, Stage 1 and Stage 2 devices, and the 409 races that follow from
all of that. After every step:

  1. every unit content any device ever committed is still in some device's
     local store, in a local backup, or in a server version - except content
     the player destroyed outright (a cleared browser, or a deletion) before
     it ever reached anywhere else, which no design can keep;
  2. the server's versions form one chain: each version's parent is the
     version that was head when it was stored (no upload not based on head
     was ever kept);
  3. a pull never removed a file from a local store.

Every seed is deterministic. A failing seed is printed; add it to FIXED_SEEDS
so it stays a regression test.
"""

import os
import random

import pytest

from savesclient import ROOT, Device, canonical, merge3, sha, unitName, unitsOf

from arcade_social import saves as savesModule
from arcade_social.saves_store import SavesStore
from arcade_social.savesconfig import GameSaves, SavesConfig

SEEDS = int(os.environ.get("SAVES_PROTOCOL_SEEDS", "400"))
STEPS = int(os.environ.get("SAVES_PROTOCOL_STEPS", "120"))
FIXED_SEEDS = (1, 2, 3, 7, 42)
PLAYER = 1
SLUG = "fishe"
STORE = "fishe-saves"
ORIGIN = "https://fishe.play.example.test"


class Holder(object):
    def __init__(self, value):
        self.value = value


class Api(object):
    """What a page reaches over HTTP, called in-process: the same rules, the
    same status codes."""

    def __init__(self, saves):
        self.saves = saves

    def _wrap(self, function, *arguments):
        try:
            return 200, function(*arguments)
        except savesModule.SavesError as e:
            return e.status, None

    def status(self):
        return self._wrap(self.saves.status, PLAYER, "alice", SLUG, STORE)

    def upload(self, body):
        try:
            result = self.saves.upload(PLAYER, "alice", SLUG, STORE, ORIGIN, body)
        except savesModule.SavesError as e:
            return e.status, None
        return (201 if result.created else 200), {"id": result.id}

    def version(self, versionId):
        try:
            _, saveFile = self.saves.versionFile(PLAYER, SLUG, STORE, versionId, lambda millis: "2026-10-03T00:00:00Z")
        except savesModule.SavesError as e:
            return e.status, None
        return 200, saveFile


def makeSaves(path, pull=True):
    game = GameSaves(SLUG, "on", STORE, "tak-saves", ROOT, 1000000, 100000000, pull)
    policy = savesModule.Policy("on", None, 10**9, 10**12)
    store = SavesStore(path)
    saves = savesModule.Saves(store, policy, Holder(SavesConfig({SLUG: game})))
    saves.store.enroll(PLAYER, SLUG, STORE)
    return saves


class World(object):
    def __init__(self, seed, path):
        self.random = random.Random(seed)
        self.saves = makeSaves(path)
        self.api = Api(self.saves)
        # Device c is a Stage 1 device: it never pulls on its own.
        self.devices = [Device("a", self.api), Device("b", self.api), Device("c", self.api, pull=False)]
        self.counter = 0
        self.committed = set()  # canonical bytes of every unit content ever committed
        self.released = set()  # destroyed by the player before reaching anywhere else
        self.history = []

    # --- where a unit content lives --------------------------------------------------

    def serverContents(self):
        found = set()
        with self.saves.store.reading() as connection:
            for row in connection.execute("SELECT bytes FROM blob WHERE player_id = ?", (PLAYER,)).fetchall():
                found.add(_normalize(bytes(row[0])))
        return found

    def localContents(self, exclude=None):
        found = set()
        for device in self.devices:
            if device is exclude:
                continue
            for files in unitsOf(device.store).values():
                found.add(_normalize(canonical(files)))
            for backup in device.backups:
                for files in unitsOf(backup).values():
                    found.add(_normalize(canonical(files)))
        return found

    def everywhere(self, exclude=None):
        return self.serverContents() | self.localContents(exclude)

    def check(self):
        present = self.everywhere()
        lost = [c for c in self.committed if c not in present and c not in self.released]
        assert not lost, "committed saves lost: %r\n%s" % (lost[:3], "\n".join(self.history[-25:]))
        versions = sorted(self.saves.store.versions(PLAYER, SLUG, STORE, limit=100000), key=lambda v: v["id"])
        for previous, current in zip([None] + versions, versions):
            expected = previous["id"] if previous else None
            assert current["parent"] == expected, "version %d was not based on head %r" % (current["id"], expected)

    # --- steps --------------------------------------------------------------------------

    def step(self):
        device = self.random.choice(self.devices)
        roll = self.random.random()
        before = dict(device.store)
        if roll < 0.40:
            self.counter += 1
            names = sorted(set(unitName(p) for p in device.store))
            unit = self.random.choice(names) if names and self.random.random() < 0.6 else "slot_%d" % self.random.randint(1, 6)
            content = "%s-%d-%s" % (device.name, self.counter, "x" * self.random.randint(20, 30))
            if device.save(unit, content):
                self.committed.add(_normalize(canonical({ROOT + "/" + unit + "/save.json": content})))
                self._released(device, before)
                if self.random.random() < 0.7:
                    self._log(device, "save %s -> %s" % (unit, device.afterSave()))
                else:
                    self._log(device, "save %s (upload deferred)" % unit)
        elif roll < 0.48:
            names = sorted(set(unitName(p) for p in device.store))
            if names:
                unit = self.random.choice(names)
                if device.delete(unit):
                    self._released(device, before)
                    self._log(device, "delete %s -> %s" % (unit, device.afterSave()))
        elif roll < 0.68:
            failed = self.random.random() < 0.1
            self._log(device, "load%s -> %s" % (" (restore failed)" if failed else "", device.load(sessionFailed=failed)))
            self._noFileRemoved(device, before)
        elif roll < 0.76:
            device.online = not device.online
            self._log(device, "online=%s" % device.online)
        elif roll < 0.82:
            self.saves.policy.mode = self.random.choice(("on", "on", "readonly", "off"))
            self._log(None, "kill switch %s" % self.saves.policy.mode)
        elif roll < 0.88:
            versions = self.saves.store.versions(PLAYER, SLUG, STORE, limit=1000)
            if versions:
                chosen = self.random.choice(versions)
                status, saveFile = self.api.version(chosen["id"])
                if status == 200:
                    device.importFile(saveFile["files"])
                    self._log(device, "import version %d" % chosen["id"])
                    self._noFileRemoved(device, before)
        elif roll < 0.90:
            # Clearing site data destroys what was only here: no design can keep it.
            elsewhere = self.everywhere(exclude=device)
            for files in unitsOf(device.store).values():
                content = _normalize(canonical(files))
                if content not in elsewhere:
                    self.released.add(content)
            for backup in device.backups:
                for files in unitsOf(backup).values():
                    content = _normalize(canonical(files))
                    if content not in elsewhere:
                        self.released.add(content)
            device.clearSiteData()
            self._log(device, "cleared site data")
        elif roll < 0.93 and device.paused == "load-offered":
            # Stage 1: the player presses "Load this version" on head.
            status, info = self.api.status()
            if status == 200 and info.get("head"):
                status, saveFile = self.api.version(info["head"]["id"])
                if status == 200:
                    device.importFile(saveFile["files"])
                    self._log(device, "load this version %d" % info["head"]["id"])
        else:
            self._log(device, "flush -> %s" % device.afterSave())
        self.check()

    def _released(self, device, before):
        """Content overwritten or deleted by the game before it reached
        anywhere else is the player's own doing, as it is without the cloud."""
        now = unitsOf(device.store)
        elsewhere = self.everywhere()
        for name, files in unitsOf(before).items():
            if name not in now or sha(now[name]) != sha(files):
                content = _normalize(canonical(files))
                if content not in elsewhere:
                    self.released.add(content)

    def _noFileRemoved(self, device, before):
        removed = [path for path in before if path not in device.store]
        assert not removed, "a load or import removed local files: %r" % removed

    def _log(self, device, text):
        self.history.append("%s: %s" % (device.name if device else "-", text))


def _normalize(data):
    """Unit content without its unit name, so a copy kept under a new slot
    counts as the same save."""
    import json

    files = json.loads(data.decode("utf-8"))
    return tuple(sorted((path[len(ROOT) + 1 :].split("/", 1)[1], content) for path, content in files.items()))


def run(seed, path, steps=STEPS):
    world = World(seed, path)
    for _ in range(steps):
        world.step()
    # Everyone back online with the switch on, two rounds of loads: every
    # device converges on the cloud's head without losing anything.
    world.saves.policy.mode = "on"
    for device in world.devices:
        device.online = True
    for _ in range(2):
        for device in world.devices:
            device.load()
            world.check()
    return world


@pytest.mark.parametrize("seed", FIXED_SEEDS)
def test_fixed_seeds(seed, tmp_path):
    run(seed, str(tmp_path / "saves.sqlite3"))


def test_random_seeds(tmp_path):
    for seed in range(100, 100 + SEEDS):
        path = str(tmp_path / ("saves-%d.sqlite3" % seed))
        try:
            run(seed, path)
        except AssertionError:
            print("FAILING SEED: %d" % seed)
            raise
        finally:
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.remove(path + suffix)


def test_devices_converge_after_conflicts(tmp_path):
    """Two Stage 2 devices change the same slot apart: after loads, both hold
    both copies, and so does the cloud."""
    saves = makeSaves(str(tmp_path / "s.sqlite3"))
    api = Api(saves)
    a, b = Device("a", api), Device("b", api)
    a.save("slot_1", "from-a-1")
    assert a.afterSave() == "uploaded"
    assert b.load() == "pulled"
    a.save("slot_1", "from-a-2")
    b.save("slot_1", "from-b-2")
    assert a.afterSave() == "uploaded"
    assert b.afterSave() == "merged"  # 409, merge uploaded, local untouched until the next load
    assert b.load() == "pulled"
    assert a.load() == "pulled"
    contents = sorted(a.store.values())
    assert contents == ["from-a-2", "from-b-2"]
    assert sorted(b.store.values()) == contents
    head = saves.store.head(PLAYER, SLUG, STORE)
    assert sorted(u["name"] for u in head["units"]) == ["slot_1", "slot_2"]


def test_a_deletion_stays_deleted_and_never_reaches_other_devices(tmp_path):
    saves = makeSaves(str(tmp_path / "s.sqlite3"))
    api = Api(saves)
    a, b = Device("a", api), Device("b", api)
    a.save("slot_1", "one")
    a.save("slot_2", "two")
    a.afterSave()
    b.load()
    assert sorted(b.store.values()) == ["one", "two"]
    a.delete("slot_2")
    assert a.afterSave() == "uploaded"
    assert [u["name"] for u in saves.store.head(PLAYER, SLUG, STORE)["units"]] == ["slot_1"]
    # b keeps its copy (deletions never travel), and does not put it back.
    assert b.load() == "merged"
    assert sorted(b.store.values()) == ["one", "two"]
    b.save("slot_1", "one-b")
    assert b.afterSave() == "uploaded"
    assert [u["name"] for u in saves.store.head(PLAYER, SLUG, STORE)["units"]] == ["slot_1"]
    # A change on b beats the removal.
    b.save("slot_2", "two-changed")
    assert b.afterSave() == "uploaded"
    assert [u["name"] for u in saves.store.head(PLAYER, SLUG, STORE)["units"]] == ["slot_1", "slot_2"]


def test_a_session_that_lost_track_of_a_slot_carries_it_forward(tmp_path):
    """The Night Ferry empty-slot-list case: the store lost a unit without the
    game deleting it. The upload carries it forward from the last sync."""
    saves = makeSaves(str(tmp_path / "s.sqlite3"))
    api = Api(saves)
    a = Device("a", api)
    a.save("slot_1", "one")
    a.save("slot_2", "two")
    a.afterSave()
    del a.store[ROOT + "/slot_2/save.json"]  # gone, but not by the game
    a.save("slot_1", "one-later")
    assert a.afterSave() == "uploaded"
    head = saves.store.head(PLAYER, SLUG, STORE)
    assert sorted(u["name"] for u in head["units"]) == ["slot_1", "slot_2"]


def test_merge_is_deterministic_and_idempotent():
    base = {"slot_1": {ROOT + "/slot_1/save.json": "b"}}
    head = {"slot_1": {ROOT + "/slot_1/save.json": "h"}, "slot_2": {ROOT + "/slot_2/save.json": "h2"}}
    local = {"slot_1": {ROOT + "/slot_1/save.json": "l"}}
    first, kept = merge3(base, head, local)
    second, _ = merge3(base, head, local)
    assert first == second
    assert kept == ["slot_3"]
    assert first["slot_3"] == {ROOT + "/slot_3/save.json": "l"}
    again, keptAgain = merge3(base, first, local)
    assert again == first and keptAgain == []


class Recording(object):
    def __init__(self, api, statusOverride=None):
        self.api = api
        self.calls = []
        self.statusOverride = statusOverride

    def status(self):
        self.calls.append("status")
        if self.statusOverride is not None:
            return self.statusOverride, None
        return self.api.status()

    def upload(self, body):
        self.calls.append("upload")
        return self.api.upload(body)

    def version(self, versionId):
        self.calls.append("version")
        return self.api.version(versionId)


@pytest.mark.parametrize("code", [0, 401, 403, 404, 409, 413, 422, 429, 500, 503])
def test_an_unknown_cloud_is_never_read_as_empty(tmp_path, code):
    """§4.6: any error or timeout at load is "unknown": no upload, no pull,
    the local store untouched - even with a head the device has never seen."""
    saves = makeSaves(str(tmp_path / "s.sqlite3"))
    other = Device("other", Api(saves))
    other.save("slot_1", "from-other")
    other.afterSave()
    recording = Recording(Api(saves), statusOverride=code)
    device = Device("d", recording)
    device.save("slot_1", "mine")
    before = dict(device.store)
    assert device.load() == "unknown"
    assert recording.calls == ["status"]
    assert device.store == before
