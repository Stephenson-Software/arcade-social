# @author Daniel McCoy Stephenson
"""saves.yaml: which games may keep cloud saves, and their limits (RFC 0016 §2, §6).

The file lives in the gateway repository beside boards.yaml
(config/play/saves.yaml), so turning a game's cloud saves on is reviewed like
a board is. It is parsed strictly with the standard library, like boards.yaml:
only the subset below is accepted, and anything else is an error naming its
line.

    games:
      night-ferry:                    # an arcade slug
        mode: on                      # on | readonly
        store: night-ferry-saves      # the save file's `game` (the page's IndexedDB name)
        format: tak-saves             # tak-saves | roam-saves
        root: /saves                  # every stored path is under root + "/"
        pull: true                    # optional, default false: pages may pull automatically (Stage 2)
        maxUploadBytes: 2097152       # one upload (the save file's JSON text)
        maxStoredBytes: 26214400      # every version of this game for one player, after dedupe

`games: {}` turns every game off. A game that is not listed, or is listed
with `mode: readonly`, keeps what it has: versions can be listed, downloaded
and deleted, but nothing new is enrolled or uploaded.
"""

import re

from arcade_social.boards import _indent, _scalar, _stripComment
from arcade_social.registry import RESERVED_SLUGS, SLUG_PATTERN

MODES = ("on", "readonly")
FORMATS = ("tak-saves", "roam-saves")
REQUIRED = ("mode", "store", "format", "root", "maxUploadBytes", "maxStoredBytes")
KEYS = REQUIRED + ("pull",)
# A ceiling on any one upload whatever the file says: the container has little
# memory, and the games' own import limit is 20 MiB (tak) / 50 MiB (Roam).
MAX_UPLOAD_CEILING = 50 * 1024 * 1024
MAX_STORED_CEILING = 1024 * 1024 * 1024
STORE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ROOT_PATTERN = re.compile(r"^(/[A-Za-z0-9._-]+)+$")
_KEY_VALUE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*):(?:\s+(.*))?$")
_BOOLEANS = {"true": True, "yes": True, "false": False, "no": False}


class SavesConfigError(ValueError):
    """saves.yaml is malformed or invalid. The message names the line."""


class GameSaves(object):
    __slots__ = ("slug", "mode", "store", "format", "root", "pull", "maxUploadBytes", "maxStoredBytes")

    def __init__(self, slug, mode, store, format, root, maxUploadBytes, maxStoredBytes, pull=False):
        self.slug = slug
        self.mode = mode
        self.store = store
        self.format = format
        self.root = root
        self.pull = pull
        self.maxUploadBytes = maxUploadBytes
        self.maxStoredBytes = maxStoredBytes

    def describe(self):
        return {
            "mode": self.mode,
            "store": self.store,
            "format": self.format,
            "root": self.root,
            "pull": self.pull,
            "maxUploadBytes": self.maxUploadBytes,
            "maxStoredBytes": self.maxStoredBytes,
        }


class SavesConfig(object):
    def __init__(self, games):
        self.games = dict(games)

    def get(self, slug):
        return self.games.get(slug)

    def __len__(self):
        return len(self.games)

    def __iter__(self):
        return iter(sorted(self.games))


def parse(text):
    """{slug: (lineNumber, {key: (lineNumber, text)})}"""
    games = {}
    seenHeader = False
    empty = False
    slug = None
    for lineNumber, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw:
            raise SavesConfigError("line %d: tabs are not allowed; indent with spaces" % lineNumber)
        line = _stripComment(raw).rstrip()
        if not line.strip():
            continue
        if not seenHeader:
            if line in ("games:", "games: {}"):
                seenHeader = True
                empty = line == "games: {}"
                continue
            raise SavesConfigError("line %d: the file must start with 'games:'" % lineNumber)
        if empty:
            raise SavesConfigError("line %d: content after 'games: {}'" % lineNumber)
        indent = _indent(line)
        body = line.strip()
        if indent == 2:
            if not body.endswith(":") or " " in body:
                raise SavesConfigError("line %d: expected '<slug>:' at two spaces, got %r" % (lineNumber, body))
            slug = body[:-1]
            if not SLUG_PATTERN.match(slug) or slug in RESERVED_SLUGS:
                raise SavesConfigError("line %d: %r is not a valid arcade slug" % (lineNumber, slug))
            if slug in games:
                raise SavesConfigError("line %d: %r is declared twice" % (lineNumber, slug))
            games[slug] = (lineNumber, {})
        elif indent == 4:
            if slug is None:
                raise SavesConfigError("line %d: a key outside any game" % lineNumber)
            match = _KEY_VALUE.match(body)
            if not match:
                raise SavesConfigError("line %d: expected 'key: value', got %r" % (lineNumber, body))
            key, value = match.group(1), match.group(2) or ""
            entry = games[slug][1]
            if key in entry:
                raise SavesConfigError("line %d: %r given twice for %r" % (lineNumber, key, slug))
            try:
                entry[key] = (lineNumber, _scalar(value, lineNumber))
            except ValueError as e:
                raise SavesConfigError(str(e))
        else:
            raise SavesConfigError(
                "line %d: unexpected indentation (games at 2 spaces, their keys at 4)" % lineNumber
            )
    if not seenHeader:
        raise SavesConfigError("the file is empty; it must contain at least 'games: {}'")
    return games


def _bytes(entry, key, ceiling):
    lineNumber, text = entry[key]
    if not text.isdigit() or not 1 <= int(text) <= ceiling:
        raise SavesConfigError("line %d: %s must be a whole number of bytes from 1 to %d" % (lineNumber, key, ceiling))
    return int(text)


def validate(parsed):
    games = {}
    stores = {}
    for slug, (lineNumber, entry) in parsed.items():
        for key, (keyLine, _) in entry.items():
            if key not in KEYS:
                raise SavesConfigError("line %d: unknown key %r (known: %s)" % (keyLine, key, ", ".join(KEYS)))
        missing = [key for key in REQUIRED if key not in entry]
        if missing:
            raise SavesConfigError("line %d: %r is missing %s" % (lineNumber, slug, ", ".join(missing)))
        modeLine, mode = entry["mode"]
        # YAML 1.1 reads a bare `on` as a boolean; this parser keeps it as text.
        if mode not in MODES:
            raise SavesConfigError("line %d: mode must be on or readonly, got %r" % (modeLine, mode))
        storeLine, store = entry["store"]
        if not STORE_PATTERN.match(store):
            raise SavesConfigError("line %d: store %r must match %s" % (storeLine, store, STORE_PATTERN.pattern))
        if store in stores:
            raise SavesConfigError("line %d: store %r is already used by %r" % (storeLine, store, stores[store]))
        stores[store] = slug
        formatLine, saveFormat = entry["format"]
        if saveFormat not in FORMATS:
            raise SavesConfigError("line %d: format must be one of %s, got %r" % (formatLine, ", ".join(FORMATS), saveFormat))
        rootLine, root = entry["root"]
        if not ROOT_PATTERN.match(root) or any(part in (".", "..") for part in root.split("/")):
            raise SavesConfigError("line %d: root must be an absolute path like /saves, got %r" % (rootLine, root))
        pull = False
        if "pull" in entry:
            pullLine, text = entry["pull"]
            if text.lower() not in _BOOLEANS:
                raise SavesConfigError("line %d: pull must be true or false, got %r" % (pullLine, text))
            pull = _BOOLEANS[text.lower()]
        maxUpload = _bytes(entry, "maxUploadBytes", MAX_UPLOAD_CEILING)
        maxStored = _bytes(entry, "maxStoredBytes", MAX_STORED_CEILING)
        if maxStored < maxUpload:
            raise SavesConfigError("line %d: %r has maxStoredBytes smaller than maxUploadBytes" % (lineNumber, slug))
        games[slug] = GameSaves(slug, mode, store, saveFormat, root, maxUpload, maxStored, pull)
    return SavesConfig(games)


def loads(text):
    return validate(parse(text))


def load(path):
    with open(path, "r", encoding="utf-8") as savesFile:
        return loads(savesFile.read())
