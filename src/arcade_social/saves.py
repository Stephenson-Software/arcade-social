# @author Daniel McCoy Stephenson
"""Cloud saves (RFC 0016): the rules, apart from HTTP.

The server never parses a game's save contents. It checks the envelope - the
same save file the games' own "Download my saves" button writes - splits it
into units, and keeps every version it is sent, append-only:

    {"format": "tak-saves", "version": 1, "game": "<store>",
     "exported": "<ISO time>", "files": {"<root>/<unit>/...": <content>, ...}}

<content> is a string, or {"base64": "..."} for bytes. A *unit* is the first
path segment under the root: a `slot_N` directory for a SaveFileManager game,
one file for a console game, one world for Roam. A unit's identity is the
SHA-256 of its canonical encoding (canonical()).

Upload rules (§4.1), in order, and nothing is written when any refuses:

    400 the save file is invalid (a port of the games' own parseImport)
    409 `parent` is not the current head                  -> {head}
    422 the upload drops a unit that head has, or halves the total size,
        unless confirmRemoved names exactly the dropped units
        (or confirmShrink is set for a shrink that drops nothing)
    413 a quota would be exceeded (old versions are never deleted to make room)
    200 the content equals head's exactly: nothing stored
    201 a new version, and head moves to it

No error is ever reported as an empty head (§4.6): a head of null is only
ever sent with 200 and enrolled true.
"""

import base64
import binascii
import hashlib
import json
import os
import re

from arcade_social.saves_store import Stale
from arcade_social.savesconfig import STORE_PATTERN as STORE_NAME, GameSaves

OFF = "off"
READONLY = "readonly"
ON = "on"
MODES = (OFF, READONLY, ON)
FORMAT_VERSION = 1
MAX_FILES = 5000
MAX_PATH_LENGTH = 1024
KINDS = ("upload", "merge", "restore", "enroll")
UPLOADS_PER_HOUR = 120
_DEVICE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_UNIT = re.compile(r"^[^\x00-\x1f\\/]{1,255}$")
_CONTROL = re.compile(r"[\x00-\x1f\\]")
_BASE64 = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")


class SavesError(Exception):
    """A refusal. `code` is machine-readable for the page; nothing was written."""

    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


def canonical(files):
    """A unit's canonical bytes: its {path: content} map as compact JSON with
    sorted keys, UTF-8. A page computes the same bytes with
    JSON.stringify over the sorted map, so both sides agree on the hash."""
    try:
        return json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise SavesError(400, "invalid", "a save contains text that is not valid Unicode")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def checkPath(path, root):
    """Why a path may not be stored, or None. tak's saves.js checkPath."""
    if not isinstance(path, str) or not path:
        return "an empty path"
    if len(path) > MAX_PATH_LENGTH:
        return "a path that is too long"
    if not path.startswith(root + "/"):
        return "a path outside %s/" % root
    if _CONTROL.search(path):
        return "a path with a control character or backslash"
    for part in path[len(root) + 1 :].split("/"):
        if part in ("", ".", ".."):
            return "a path with an empty, . or .. part"
    return None


def _checkContent(path, value):
    if isinstance(value, str):
        return
    if isinstance(value, dict) and list(value) == ["base64"] and isinstance(value["base64"], str):
        text = value["base64"]
        if _BASE64.match(text) and len(text) % 4 == 0:
            try:
                base64.b64decode(text, validate=True)
                return
            except (binascii.Error, ValueError):
                pass
    raise SavesError(400, "invalid", "the save file is damaged: %s cannot be read" % path[:120])


def parseSaveFile(document, game):
    """Validate a save file (already JSON-decoded) for one game. Returns
    {unit name: {path: content}}. Raises SavesError(400)."""
    if not isinstance(document, dict) or document.get("format") != game.format:
        raise SavesError(400, "invalid", "this is not a %s file" % game.format)
    version = document.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise SavesError(400, "invalid", "the save file has no readable version")
    if version != FORMAT_VERSION:
        raise SavesError(400, "invalid", "the save file's version (%d) is not one this service reads" % version)
    if document.get("game") != game.store:
        raise SavesError(400, "invalid", "the save file is for another store than %s" % game.store)
    exported = document.get("exported")
    if exported is not None and not isinstance(exported, str):
        raise SavesError(400, "invalid", "exported must be a string")
    files = document.get("files")
    if not isinstance(files, dict):
        raise SavesError(400, "invalid", "the save file has no list of files")
    if len(files) > MAX_FILES:
        raise SavesError(400, "invalid", "the save file holds too many files")
    units = {}
    for path, value in files.items():
        problem = checkPath(path, game.root)
        if problem:
            raise SavesError(400, "invalid", "the save file contains %s (%s)" % (problem, path[:80]))
        _checkContent(path, value)
        name = path[len(game.root) + 1 :].split("/", 1)[0]
        units.setdefault(name, {})[path] = value
    return units


def encodeUnits(units):
    """{name: {path: content}} -> {name: (sha256, canonical bytes)}"""
    encoded = {}
    for name, files in units.items():
        data = canonical(files)
        encoded[name] = (sha256(data), data)
    return encoded


def saveFile(game, description, contents):
    """A stored version as a save file the games' import accepts."""
    files = {}
    for name in sorted(contents):
        files.update(json.loads(contents[name].decode("utf-8")))
    return {
        "format": description["format"],
        "version": FORMAT_VERSION,
        "game": game.store,
        "exported": description["createdAtIso"],
        "files": files,
        "cloudVersion": description["id"],
    }


class Uploaded(object):
    __slots__ = ("id", "created")

    def __init__(self, versionId, created):
        self.id = versionId
        self.created = created


class Policy(object):
    """Which accounts and games may do what, from the environment switches and
    saves.yaml (§7)."""

    def __init__(self, mode, accounts, playerMaxBytes, databaseMaxBytes):
        if mode not in MODES:
            raise ValueError("ARCADE_SOCIAL_SAVES must be off, readonly or on, got %r" % mode)
        self.mode = mode
        # None: everyone ("*"). An empty set: nobody.
        self.accounts = accounts
        self.playerMaxBytes = playerMaxBytes
        self.databaseMaxBytes = databaseMaxBytes

    def allowed(self, username):
        return self.accounts is None or (username or "").lower() in self.accounts

    def writable(self, game):
        """Why new uploads and enrollments are refused for a game, or None."""
        if self.mode == OFF:
            return SavesError(503, "off", "cloud saves are off")
        if self.mode == READONLY:
            return SavesError(503, "paused", "cloud saves are paused; nothing new is being backed up")
        if game is None or game.mode != ON:
            return SavesError(503, "paused", "cloud saves for this game are read-only")
        return None


def validateUpload(body, game):
    """The PUT body -> (parent, meta, units, confirmRemoved, confirmShrink). Raises SavesError(400)."""
    if not isinstance(body, dict):
        raise SavesError(400, "invalid", "the body must be a JSON object")
    unknown = sorted(set(body) - {"parent", "device", "deviceLabel", "kind", "file", "confirmRemoved", "confirmShrink"})
    if unknown:
        raise SavesError(400, "invalid", "unknown field(s): %s" % ", ".join(unknown))
    parent = body.get("parent")
    if parent is not None and (isinstance(parent, bool) or not isinstance(parent, int) or parent < 1):
        raise SavesError(400, "invalid", "parent must be a version id or null")
    device = body.get("device")
    if not isinstance(device, str) or not _DEVICE.match(device):
        raise SavesError(400, "invalid", "device must be 8-64 letters, digits and dashes")
    label = body.get("deviceLabel", "")
    if not isinstance(label, str) or len(label) > 60 or _CONTROL.search(label):
        raise SavesError(400, "invalid", "deviceLabel must be at most 60 printable characters")
    kind = body.get("kind", "upload")
    if kind not in KINDS:
        raise SavesError(400, "invalid", "kind must be one of %s" % ", ".join(KINDS))
    confirmRemoved = body.get("confirmRemoved", [])
    if not isinstance(confirmRemoved, list) or not all(isinstance(name, str) for name in confirmRemoved):
        raise SavesError(400, "invalid", "confirmRemoved must be a list of unit names")
    if len(set(confirmRemoved)) != len(confirmRemoved):
        raise SavesError(400, "invalid", "confirmRemoved names a unit twice")
    confirmShrink = body.get("confirmShrink", False)
    if not isinstance(confirmShrink, bool):
        raise SavesError(400, "invalid", "confirmShrink must be true or false")
    if "file" not in body:
        raise SavesError(400, "invalid", "file is required")
    units = parseSaveFile(body["file"], game)
    return parent, {"device": device, "deviceLabel": label.strip() or "A browser", "kind": kind}, units, confirmRemoved, confirmShrink


def checkAgainstHead(head, encoded, confirmRemoved, confirmShrink):
    """Rule 3 (§4.1): never let an upload drop a unit, or halve the saves,
    without saying so. Raises SavesError(422)."""
    headUnits = dict((unit["name"], unit) for unit in head["units"]) if head else {}
    dropped = sorted(name for name in headUnits if name not in encoded)
    confirmed = sorted(confirmRemoved)
    if confirmed != dropped:
        if dropped and not confirmed:
            raise SavesError(
                422,
                "removed",
                "this upload leaves out saves your account has; nothing was stored",
                removed=dropped,
            )
        raise SavesError(
            422,
            "confirm-mismatch",
            "confirmRemoved must name exactly the saves this upload leaves out",
            removed=dropped,
        )
    if head and not dropped and not confirmShrink:
        before = head["totalSize"]
        after = sum(len(data) for _, data in encoded.values())
        if before > 0 and after * 2 < before:
            raise SavesError(
                422,
                "shrink",
                "this upload is less than half the size of your account's saves; nothing was stored",
                before=before,
                after=after,
            )


class Saves(object):
    """The saves rules over a SavesStore, for one process."""

    def __init__(self, store, policy, gamesHolder, databaseSize=None):
        self.store = store
        self.policy = policy
        self.gamesHolder = gamesHolder
        self._databaseSize = databaseSize

    @property
    def games(self):
        return self.gamesHolder.value

    def databaseSize(self):
        if self._databaseSize is not None:
            return self._databaseSize()
        total = 0
        for suffix in ("", "-wal"):
            try:
                total += os.path.getsize(self.store.path + suffix)
            except OSError:
                pass
        return total

    def game(self, slug, store):
        """The game's saves.yaml entry for this store. A game that is not
        listed (or left saves.yaml) is read-only, but a player's stored
        versions stay reachable: it is described by its store name alone."""
        game = self.games.get(slug)
        if game is not None:
            if game.store != store:
                raise SavesError(404, "unknown-store", "this game keeps no cloud saves in %s" % store)
            return game
        if not STORE_NAME.match(store):
            raise SavesError(404, "unknown-store", "no such store")
        return GameSaves(slug, READONLY, store, None, None, 0, 0, False)

    def requireOn(self):
        if self.policy.mode == OFF:
            raise SavesError(503, "off", "cloud saves are off")

    def status(self, playerId, username, slug, store):
        game = self.game(slug, store)
        enrolled = playerId is not None and self.store.enrolled(playerId, slug, store)
        refusal = self.policy.writable(game)
        payload = {
            "enrolled": enrolled,
            "allowed": self.policy.allowed(username),
            "writable": refusal is None,
            "reason": refusal.code if refusal is not None else None,
            "pull": game.pull,
            "quota": {"maxUploadBytes": game.maxUploadBytes, "maxStoredBytes": game.maxStoredBytes},
        }
        if enrolled:
            payload["head"] = self.store.head(playerId, slug, store)
            payload["quota"]["storedBytes"] = self.store.storedBytes(playerId, slug, store)
        return payload

    def enroll(self, playerId, username, slug, store):
        game = self.game(slug, store)
        refusal = self.policy.writable(game)
        if refusal is not None:
            raise refusal
        if not self.policy.allowed(username):
            raise SavesError(403, "not-allowed", "cloud saves are not open to this account yet")
        return self.store.enroll(playerId, slug, store)

    def upload(self, playerId, username, slug, store, origin, body):
        game = self.game(slug, store)
        refusal = self.policy.writable(game)
        if refusal is not None:
            raise refusal
        if not self.policy.allowed(username):
            raise SavesError(403, "not-allowed", "cloud saves are not open to this account yet")
        if not self.store.enrolled(playerId, slug, store):
            raise SavesError(403, "not-enrolled", "turn on cloud backup for this game first")
        if self.databaseSize() >= self.policy.databaseMaxBytes:
            raise SavesError(503, "full", "cloud saves are full for now; nothing new is being backed up")
        parent, meta, units, confirmRemoved, confirmShrink = validateUpload(body, game)
        encoded = encodeUnits(units)
        if not encoded and parent is None:
            # Enrolled with nothing to back up: there is nothing to keep.
            head = self.store.head(playerId, slug, store)
            if head is None:
                return Uploaded(None, False)
        meta["origin"] = origin
        meta["format"] = game.format
        meta["removed"] = list(confirmRemoved)
        policy = self.policy

        def check(head, newGameBytes, newPlayerBytes, gameBytes, playerBytes):
            checkAgainstHead(head, encoded, confirmRemoved, confirmShrink)
            if gameBytes + newGameBytes > game.maxStoredBytes:
                raise SavesError(
                    413,
                    "quota",
                    "your cloud storage for this game is full; nothing was stored and no old version was deleted",
                    storedBytes=gameBytes,
                    maxStoredBytes=game.maxStoredBytes,
                )
            if playerBytes + newPlayerBytes > policy.playerMaxBytes:
                raise SavesError(
                    413,
                    "quota",
                    "your cloud storage is full; nothing was stored and no old version was deleted",
                    storedBytes=playerBytes,
                    maxStoredBytes=policy.playerMaxBytes,
                )

        try:
            versionId, created = self.store.commit(playerId, slug, store, parent, encoded, meta, check)
        except Stale as e:
            raise SavesError(409, "stale", "your account has newer saves than this upload was based on", head=e.head)
        return Uploaded(versionId, created)

    def versionFile(self, playerId, slug, store, versionId, isoTime):
        game = self.game(slug, store)
        found = self.store.version(playerId, slug, store, versionId)
        if found is None:
            raise SavesError(404, "no-version", "no such version")
        description, contents = found
        description["createdAtIso"] = isoTime(description["createdAt"])
        return description, saveFile(game, description, contents)
