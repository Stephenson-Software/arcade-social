# @author Daniel McCoy Stephenson
"""A reference cloud-saves client: the page algorithm of RFC 0016 §4, in
Python, with a dict for IndexedDB and a dict for localStorage.

tak's /tak/cloud.js implements the same steps; this copy exists so the
randomized protocol test (test_saves_protocol.py) can run thousands of
three-device histories against the real server rules in seconds.

A store is {path: content}. A unit is the first path segment under ROOT.

Per device, kept across reloads (localStorage):
    sync            {"id": version id, "units": {name: sha}} - the version the
                    local store equalled when it last uploaded or pulled
    pendingDeleted  {name}: units the game deleted on purpose, not yet uploaded
    deletedElsewhere {name: sha}: local units the cloud no longer has; left
                    out of uploads while unchanged (RFC 0016 §4.5)
    missing         {name: sha}: units this store lost without the game deleting
                    them, carried forward from the cloud; if one comes back
                    with different content (a new game started in a slot that
                    looked empty), both are kept - it never replaces the lost one

The rules this client keeps, and the test checks:
    - an error is never read as "nothing in the cloud" (§4.6)
    - a unit missing locally is carried forward unless the game deleted it (§4.5)
    - a conflict keeps both copies (§4.4); nothing is ever newest-wins
    - a pull backs the local store up and reads the backup back first, and
      writes put-only: it never deletes a local file (§4.3)
"""

import hashlib
import json
import re

ROOT = "/saves"
_SLOT = re.compile(r"^slot_([0-9]+)$")
MAX_SLOTS = 99


def unitName(path):
    return path[len(ROOT) + 1 :].split("/", 1)[0]


def unitsOf(files):
    units = {}
    for path, content in files.items():
        units.setdefault(unitName(path), {})[path] = content
    return units


def canonical(unitFiles):
    return json.dumps(unitFiles, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha(unitFiles):
    return hashlib.sha256(canonical(unitFiles)).hexdigest()


def shas(units):
    return dict((name, sha(files)) for name, files in units.items())


def rename(unitFiles, old, new):
    prefix = ROOT + "/" + old
    return dict((ROOT + "/" + new + path[len(prefix) :], content) for path, content in unitFiles.items())


def contentKey(name, unitFiles):
    """A unit's content without its name, so a copy kept under another slot
    is recognised as the same save."""
    return sha(rename(unitFiles, name, "_"))


class Unresolvable(Exception):
    """A conflict whose second copy cannot be given a new name. Nothing is
    written; the page pauses and says so."""


def merge3(base, head, local):
    """Per-unit three-way merge (RFC 0016 §4.4). Each argument is
    {name: unitFiles}. Returns (merged {name: unitFiles}, keptTwice [names])."""
    baseSha, headSha, localSha = shas(base), shas(head), shas(local)
    merged = {}
    conflicts = []
    for name in sorted(set(base) | set(head) | set(local)):
        b, h, l = baseSha.get(name), headSha.get(name), localSha.get(name)
        if h == l:
            if h is not None:
                merged[name] = head[name]
        elif b == h:
            if l is not None:
                merged[name] = local[name]  # only local changed (or removed it on purpose)
        elif b == l:
            if h is not None:
                merged[name] = head[name]  # only the cloud changed
        elif h is None:
            merged[name] = local[name]  # removed in the cloud, changed here: a change beats a removal
        elif l is None:
            merged[name] = head[name]  # removed here, changed in the cloud
        else:
            merged[name] = head[name]
            conflicts.append(name)
    present = set(contentKey(name, files) for name, files in merged.items())
    taken = set(base) | set(head) | set(local) | set(merged)
    kept = []
    for name in conflicts:
        key = contentKey(name, local[name])
        if key in present:
            continue  # this copy is already in the merged set under some name
        match = _SLOT.match(name)
        if not match:
            raise Unresolvable(name)
        free = next(("slot_%d" % n for n in range(1, MAX_SLOTS + 1) if "slot_%d" % n not in taken), None)
        if free is None:
            raise Unresolvable(name)
        taken.add(free)
        merged[free] = rename(local[name], name, free)
        present.add(key)
        kept.append(free)
    return merged, kept


class Device(object):
    def __init__(self, name, api, pull=True):
        self.name = name
        self.api = api
        self.pull = pull
        self.store = {}  # IndexedDB: {path: content}
        self.backups = []  # <idb>.tak-backups: [{path: content}]
        self.state = {"sync": None, "pendingDeleted": [], "deletedElsewhere": {}, "missing": {}}  # localStorage
        self.sessionFailed = False  # the Worker's restore failed this session (tak's nosave)
        self.paused = None  # why uploads stop until the next load
        self.online = True

    # --- what the game does ---------------------------------------------------------

    def save(self, unit, content):
        """The game commits a save: the Worker's sync writes the unit's file."""
        if self.sessionFailed:
            return False  # no sync from a session whose restore failed
        for path in [p for p in self.store if unitName(p) == unit]:
            if path != ROOT + "/" + unit + "/save.json":
                del self.store[path]
        self.store[ROOT + "/" + unit + "/save.json"] = content
        return True

    def delete(self, unit):
        """The game deletes a slot (tak#22: the Worker names the paths)."""
        if self.sessionFailed:
            return False
        paths = [p for p in self.store if unitName(p) == unit]
        if not paths:
            return False
        for path in paths:
            del self.store[path]
        if unit not in self.state["pendingDeleted"]:
            self.state["pendingDeleted"].append(unit)
        return True

    def importFile(self, files):
        """Load saves from a file: back up, read back, put-only."""
        if self.store:
            self.backups.append(dict(self.store))
        self.store.update(files)

    def clearSiteData(self):
        self.store = {}
        self.backups = []
        self.state = {"sync": None, "pendingDeleted": [], "deletedElsewhere": {}, "missing": {}}

    # --- the client ---------------------------------------------------------------------

    def _call(self, method, *arguments):
        if not self.online:
            return 0, None
        return getattr(self.api, method)(*arguments)

    def _view(self, baseUnits):
        """The local units as the cloud should see them: what is stored, minus
        units deleted elsewhere and unchanged here, plus units missing here
        that the game did not delete (carried forward from the last sync)."""
        local = unitsOf(self.store)
        view = {}
        elsewhere = self.state["deletedElsewhere"]
        for name, files in local.items():
            if name in elsewhere and elsewhere[name] == sha(files):
                continue
            view[name] = files
        sync = self.state["sync"]
        if sync is not None:
            for name in sync["units"]:
                if name not in local and name not in self.state["pendingDeleted"] and name not in elsewhere:
                    if baseUnits is None:
                        return None  # content needed and not fetched
                    view[name] = baseUnits[name]
        return view

    def _needsBase(self):
        sync = self.state["sync"]
        if sync is None:
            return False
        local = unitsOf(self.store)
        return any(
            name not in local and name not in self.state["pendingDeleted"] and name not in self.state["deletedElsewhere"]
            for name in sync["units"]
        )

    def _fetch(self, versionId):
        status, payload = self._call("version", versionId)
        if status != 200:
            return None
        return unitsOf(payload["files"])

    def _put(self, parent, units, kind, headUnits):
        removed = sorted(name for name in headUnits if name not in units)
        files = {}
        for unitFiles in units.values():
            files.update(unitFiles)
        body = {
            "parent": parent,
            "device": "device-" + self.name,
            "deviceLabel": self.name,
            "kind": kind,
            "file": {"format": "tak-saves", "version": 1, "game": "fishe-saves", "files": files},
        }
        if removed:
            body["confirmRemoved"] = removed
        return self._call("upload", body)

    def _synced(self, versionId, units):
        self.state["sync"] = {"id": versionId, "units": shas(units)}
        self.state["pendingDeleted"] = []
        for name in units:
            self.state["deletedElsewhere"].pop(name, None)  # the cloud has it again

    def load(self, sessionFailed=False):
        """A page load: a new session, then the sync that runs before the
        Worker starts (Stage 2 when pull is on)."""
        self.sessionFailed = sessionFailed
        self.paused = None
        if sessionFailed:
            return "restore-failed"
        for _ in range(3):
            outcome = self._syncOnce(allowPull=True)
            if outcome != "retry":
                return outcome
        return "retry"

    def afterSave(self):
        """The upload that trails a committed save, mid-session (no pull)."""
        if self.sessionFailed:
            return "skipped"
        if self.paused:
            local = unitsOf(self.store)
            self.state["pendingDeleted"] = [n for n in self.state["pendingDeleted"] if n not in local]
            self._recordMissing(local)  # paused, but a loss is still recorded now
            return "skipped"
        outcome = self._syncOnce(allowPull=False)
        return outcome

    def _recordMissing(self, local):
        sync = self.state["sync"]
        if sync is None:
            return
        for name in sync["units"]:
            if name in local or name in self.state["pendingDeleted"] or name in self.state["deletedElsewhere"]:
                continue
            self.state["missing"].setdefault(name, sync["units"][name])

    def _syncOnce(self, allowPull):
        # The store is read before any network, so a loss is recorded even
        # while the cloud cannot be reached.
        local = unitsOf(self.store)
        # A slot the game deleted and then wrote again is not deleted.
        self.state["pendingDeleted"] = [n for n in self.state["pendingDeleted"] if n not in local]
        self._recordMissing(local)
        status, info = self._call("status")
        if status != 200 or not info.get("enrolled"):
            return "unknown"  # never "nothing in the cloud"
        reappeared = []
        for name in list(self.state["missing"]):
            if name in self.state["pendingDeleted"]:
                del self.state["missing"][name]
            elif name in local:
                if sha(local[name]) == self.state["missing"][name]:
                    del self.state["missing"][name]
                else:
                    reappeared.append(name)
        head = info.get("head")
        sync = self.state["sync"]
        headUnitShas = dict((u["name"], u["sha256"]) for u in head["units"]) if head else {}
        if head is None:
            view = unitsOf(self.store)
            for name, files in list(view.items()):
                if self.state["deletedElsewhere"].get(name) == sha(files):
                    del view[name]
            if not view:
                return "empty"
            if not info.get("writable"):
                return "paused"
            code, payload = self._put(None, view, "enroll", {})
            if code in (200, 201):
                self._synced(payload["id"], view)
                return "uploaded"
            return "retry" if code == 409 else "failed"
        if sync is not None and sync["id"] == head["id"] and not reappeared:
            baseUnits = self._fetch(sync["id"]) if self._needsBase() else {}
            if baseUnits is None:
                return "unknown"
            view = self._view(baseUnits)
            outcome = "in-sync"
            if shas(view) != sync["units"]:
                if not info.get("writable"):
                    return "paused"
                code, payload = self._put(head["id"], view, "upload", headUnitShas)
                if code not in (200, 201):
                    return "retry" if code == 409 else "failed"
                self._synced(payload["id"], view)
                outcome = "uploaded"
            # Units the cloud has that this store lost come back at load:
            # put-only into absent paths, nothing overwritten.
            lost = dict((name, files) for name, files in view.items() if name not in local)
            if allowPull and lost and self.pull:
                self.backups.append(dict(self.store))
                for files in lost.values():
                    self.store.update(files)
                return "pulled"
            return outcome
        # The cloud moved on (or this device has never synced): merge.
        if sync is not None:
            base = self._fetch(sync["id"])
            if base is None:
                return "unknown"
        else:
            base = {}
        headUnits = self._fetch(head["id"])
        if headUnits is None:
            return "unknown"
        view = self._view(base)
        mergeBase = dict(base)
        for name in reappeared:
            mergeBase.pop(name, None)  # as if never seen: the cloud's copy stays, this one goes to a free slot
        try:
            merged, kept = merge3(mergeBase, headUnits, view)
        except Unresolvable:
            self.paused = "unresolvable"
            return "paused"
        newHead = head["id"]
        if shas(merged) != headUnitShas:
            if not info.get("writable"):
                return "paused"
            code, payload = self._put(head["id"], merged, "merge", headUnitShas)
            if code == 409:
                return "retry"
            if code not in (200, 201):
                return "failed"
            newHead = payload["id"]
        if not allowPull:
            # Mid-session: the cloud now holds everything; the local store is
            # brought up to date on the next load, before the game starts.
            self.paused = "reload-to-sync"
            return "merged"
        local = unitsOf(self.store)
        changed = dict((n, f) for n, f in merged.items() if n not in local or sha(local[n]) != sha(f))
        if changed and not self.pull:
            self.paused = "load-offered"  # Stage 1: the player loads it by hand
            return "offered"
        if changed:
            # A pull is an import: back up, read back, put-only, read back.
            backup = dict(self.store)
            self.backups.append(backup)
            assert self.backups[-1] == backup
            for files in changed.values():
                self.store.update(files)
            for files in changed.values():
                for path, content in files.items():
                    assert self.store[path] == content
        for name, files in unitsOf(self.store).items():
            if name not in merged:
                self.state["deletedElsewhere"][name] = sha(files)
        for name in list(self.state["deletedElsewhere"]):
            if name in merged:
                del self.state["deletedElsewhere"][name]
        self._synced(newHead, merged)
        return "pulled" if changed else "merged"
