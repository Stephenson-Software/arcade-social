# @author Daniel McCoy Stephenson
"""The registry: games.yaml, one entry per hosted game (RFC 0006 §3).

The file lives in the gateway repository and is written by hand, so it is
parsed strictly: only the small YAML subset below is accepted, and anything
else (an unknown key, a misplaced indent, a duplicate slug) is an error naming
its line rather than a guess. Parsing it with the standard library keeps the
service dependency-free (RFC 0006 Goals: "stdlib Python, one volume, no
database").

    games:
      - slug: tidewater                      # ^[a-z][a-z0-9-]{1,30}$
        title: Tidewater
        repo: Stephenson-Software/Tidewater
        owner: dmccoystephenson              # optional
        token_sha256: "<64 hex chars>"       # sha256 of the upload token
        aliases: [tidewater.danielstephenson.dev]   # optional, flow list only
        kind: tak                            # optional: tak (default) or static
        isolation: on                        # optional: on/off (see below)

`games: []` is an empty registry.

kind (RFC 0012): `tak` is the original bundle - index.html + game.zip +
version.txt, with /tak/ served from game.zip (RFC 0006). `static` is any
directory with index.html at its root (a pygbag, Emscripten or plain
HTML/JS build), served as files.

isolation: whether responses carry COOP/COEP/CORP. A tak game needs it
(SharedArrayBuffer carries its input) and may not turn it off; a static game
defaults to off, because builds that load a runtime from a CDN without a
Cross-Origin-Resource-Policy header (pygbag does) cannot run under
require-corp. Turn it on for a static build that needs SharedArrayBuffer
(an Emscripten build with pthreads).
"""

import re

SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
TOKEN_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
HOSTNAME_PATTERN = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# Labels the service itself uses or that would be confusing as a game.
RESERVED_SLUGS = frozenset(("play", "www", "api", "arcade", "admin", "static"))

REQUIRED_KEYS = ("slug", "title", "repo", "token_sha256")
OPTIONAL_KEYS = ("owner", "aliases", "kind", "isolation")
KINDS = ("tak", "static")
_SWITCH = {"on": True, "true": True, "yes": True, "off": False, "false": False, "no": False}
KNOWN_KEYS = REQUIRED_KEYS + OPTIONAL_KEYS


class RegistryError(ValueError):
    """The registry file is malformed or invalid. The message names the line."""


class Game(object):
    __slots__ = ("slug", "title", "repo", "owner", "tokenSha256", "aliases", "kind", "isolation")

    def __init__(self, slug, title, repo, tokenSha256, owner=None, aliases=(), kind="tak", isolation=None):
        self.slug = slug
        self.title = title
        self.repo = repo
        self.owner = owner
        self.tokenSha256 = tokenSha256
        self.aliases = tuple(aliases)
        self.kind = kind
        self.isolation = (kind == "tak") if isolation is None else bool(isolation)

    def __repr__(self):
        return "Game(%r)" % self.slug


class Registry(object):
    """The parsed, validated registry: games by slug, and by alias host."""

    def __init__(self, games):
        self.games = {}
        self.aliases = {}
        for game in games:
            self.games[game.slug] = game
            for alias in game.aliases:
                self.aliases[alias] = game

    def get(self, slug):
        return self.games.get(slug)

    def forAlias(self, host):
        return self.aliases.get(host)

    def __iter__(self):
        return iter(sorted(self.games.values(), key=lambda game: game.slug))

    def __len__(self):
        return len(self.games)


def _stripComment(line):
    """Drop a trailing # comment that is not inside quotes."""
    quote = None
    for index, character in enumerate(line):
        if quote:
            if character == quote:
                quote = None
        elif character in "\"'":
            quote = character
        elif character == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
    return line


def _scalar(text, lineNumber):
    text = text.strip()
    if not text:
        raise RegistryError("line %d: a value is required" % lineNumber)
    if text[0] in "\"'":
        if len(text) < 2 or text[-1] != text[0]:
            raise RegistryError("line %d: unterminated quoted value %s" % (lineNumber, text))
        inner = text[1:-1]
        if text[0] in inner:
            raise RegistryError("line %d: escaped quotes are not supported: %s" % (lineNumber, text))
        return inner
    if text[0] in "[{&*!|>%@`":
        raise RegistryError("line %d: unsupported value %s" % (lineNumber, text))
    return text


def _flowList(text, lineNumber):
    text = text.strip()
    if not (text.startswith("[") and text.endswith("]")):
        raise RegistryError("line %d: expected a [flow, list], got %s" % (lineNumber, text))
    inner = text[1:-1].strip()
    if not inner:
        return []
    return [_scalar(part, lineNumber) for part in inner.split(",")]


_KEY_VALUE = re.compile(r"^([a-z_0-9]+):(?:\s+(.*))?$")


def parse(text):
    """Parse registry text into a list of (lineNumber, dict) entries."""
    entries = []
    current = None
    seenHeader = False
    for lineNumber, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw:
            raise RegistryError("line %d: tabs are not allowed; indent with spaces" % lineNumber)
        line = _stripComment(raw).rstrip()
        if not line.strip():
            continue
        if not seenHeader:
            if line == "games:":
                seenHeader = True
                continue
            if line == "games: []":
                seenHeader = True
                current = "empty"
                continue
            raise RegistryError("line %d: the file must start with 'games:'" % lineNumber)
        if current == "empty":
            raise RegistryError("line %d: content after 'games: []'" % lineNumber)
        if line.startswith("  - "):
            body = line[4:]
            current = {}
            entries.append((lineNumber, current))
        elif line.startswith("    ") and not line.startswith("     "):
            if current is None:
                raise RegistryError("line %d: a key outside any '- ' entry" % lineNumber)
            body = line[4:]
        else:
            raise RegistryError(
                "line %d: unexpected indentation (entries are '  - key: value', "
                "their keys '    key: value')" % lineNumber
            )
        match = _KEY_VALUE.match(body)
        if not match:
            raise RegistryError("line %d: expected 'key: value', got %r" % (lineNumber, body))
        key, value = match.group(1), match.group(2) or ""
        if key not in KNOWN_KEYS:
            raise RegistryError(
                "line %d: unknown key %r (known: %s)" % (lineNumber, key, ", ".join(KNOWN_KEYS))
            )
        if key in current:
            raise RegistryError("line %d: %r given twice in one entry" % (lineNumber, key))
        current[key] = _flowList(value, lineNumber) if key == "aliases" else _scalar(value, lineNumber)
    if not seenHeader:
        raise RegistryError("the registry is empty; it must contain at least 'games: []'")
    return entries


def validate(entries, domain=None):
    """Turn parsed entries into a Registry, or raise RegistryError.

    domain, when given, is the arcade's parent domain (play.example.com): an
    alias under it would shadow a slug's own host and is refused."""
    games = []
    slugs = {}
    aliasOwners = {}
    for lineNumber, entry in entries:
        missing = [key for key in REQUIRED_KEYS if key not in entry]
        if missing:
            raise RegistryError("line %d: entry is missing %s" % (lineNumber, ", ".join(missing)))
        slug = entry["slug"]
        if not SLUG_PATTERN.match(slug):
            raise RegistryError(
                "line %d: slug %r must match %s" % (lineNumber, slug, SLUG_PATTERN.pattern)
            )
        if slug in RESERVED_SLUGS:
            raise RegistryError("line %d: slug %r is reserved" % (lineNumber, slug))
        if slug in slugs:
            raise RegistryError(
                "line %d: slug %r is already registered on line %d" % (lineNumber, slug, slugs[slug])
            )
        slugs[slug] = lineNumber
        if not TOKEN_HASH_PATTERN.match(entry["token_sha256"]):
            raise RegistryError(
                "line %d: token_sha256 for %r must be 64 lowercase hex characters" % (lineNumber, slug)
            )
        if not REPO_PATTERN.match(entry["repo"]):
            raise RegistryError("line %d: repo %r must be owner/name" % (lineNumber, entry["repo"]))
        aliases = [alias.lower() for alias in entry.get("aliases", [])]
        for alias in aliases:
            if not HOSTNAME_PATTERN.match(alias):
                raise RegistryError("line %d: alias %r is not a hostname" % (lineNumber, alias))
            if domain and (alias == domain or alias.endswith("." + domain)):
                raise RegistryError(
                    "line %d: alias %r is under %s, where slugs live" % (lineNumber, alias, domain)
                )
            if alias in aliasOwners:
                raise RegistryError(
                    "line %d: alias %r is already claimed by %r" % (lineNumber, alias, aliasOwners[alias])
                )
            aliasOwners[alias] = slug
        kind = entry.get("kind", "tak")
        if kind not in KINDS:
            raise RegistryError(
                "line %d: kind %r for %r must be one of %s" % (lineNumber, kind, slug, ", ".join(KINDS))
            )
        isolation = None
        if "isolation" in entry:
            isolation = _SWITCH.get(entry["isolation"].lower())
            if isolation is None:
                raise RegistryError(
                    "line %d: isolation for %r must be on or off, got %r" % (lineNumber, slug, entry["isolation"])
                )
            if kind == "tak" and not isolation:
                raise RegistryError(
                    "line %d: %r is a tak game, which needs isolation (SharedArrayBuffer carries "
                    "its input); it cannot be turned off" % (lineNumber, slug)
                )
        games.append(
            Game(
                slug=slug,
                title=entry["title"],
                repo=entry["repo"],
                tokenSha256=entry["token_sha256"],
                owner=entry.get("owner"),
                aliases=aliases,
                kind=kind,
                isolation=isolation,
            )
        )
    return Registry(games)


def load(path, domain=None):
    with open(path, "r", encoding="utf-8") as registryFile:
        text = registryFile.read()
    return validate(parse(text), domain=domain)


def loads(text, domain=None):
    return validate(parse(text), domain=domain)
