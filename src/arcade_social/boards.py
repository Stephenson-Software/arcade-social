# @author Daniel McCoy Stephenson
"""boards.yaml: each game's leaderboards and achievements (RFC 0014 §1).

The file lives in the gateway repository beside arcade's registry
(config/play/boards.yaml) so that a board, and the limits a score on it must
respect, is reviewed like a slug is. It is written by hand, so - like arcade's
games.yaml - it is parsed strictly with the standard library: only the subset
below is accepted, and anything else is an error naming its line.

    games:
      fishe:                          # an arcade slug
        boards:
          - id: most-money            # ^[a-z][a-z0-9-]{1,30}$, never renamed once used
            title: Most money in one save
            order: desc               # desc: higher is better; asc: lower is better
            unit: dollars             # optional, display only
            integer: true             # optional, default false
            min: 0                    # required: anything outside min..max is refused
            max: 100000000
            maxPerHour: 30            # optional, default 30: submissions per player per board
        achievements:
          - id: first-catch
            title: First Catch
            description: Catch your first fish
            hidden: false             # optional: hide title/description until unlocked

`games: {}` is an empty file. Board and achievement ids are permanent: renaming
one orphans its records. Removing one hides it; its records are kept.
"""

import math
import re

from arcade_social.registry import RESERVED_SLUGS, SLUG_PATTERN

ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
ORDERS = ("asc", "desc")
BOARD_REQUIRED = ("id", "title", "order", "min", "max")
BOARD_KEYS = BOARD_REQUIRED + ("unit", "integer", "maxPerHour")
ACHIEVEMENT_REQUIRED = ("id", "title", "description")
ACHIEVEMENT_KEYS = ACHIEVEMENT_REQUIRED + ("hidden",)
SECTIONS = ("boards", "achievements")
DEFAULT_MAX_PER_HOUR = 30
MAX_TITLE = 80
MAX_DESCRIPTION = 200
MAX_UNIT = 20
_BOOLEANS = {"true": True, "yes": True, "on": True, "false": False, "no": False, "off": False}
_NUMBER = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?$")
_KEY_VALUE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*):(?:\s+(.*))?$")


class BoardsError(ValueError):
    """boards.yaml is malformed or invalid. The message names the line."""


class Board(object):
    __slots__ = ("id", "title", "order", "unit", "integer", "min", "max", "maxPerHour")

    def __init__(self, id, title, order, min, max, unit=None, integer=False, maxPerHour=DEFAULT_MAX_PER_HOUR):
        self.id = id
        self.title = title
        self.order = order
        self.unit = unit
        self.integer = integer
        self.min = min
        self.max = max
        self.maxPerHour = maxPerHour

    def better(self, value, than):
        """True if value beats than in this board's order (a tie does not)."""
        return value > than if self.order == "desc" else value < than

    def describe(self):
        return {
            "id": self.id,
            "title": self.title,
            "order": self.order,
            "unit": self.unit,
            "integer": self.integer,
            "min": self.min,
            "max": self.max,
            "maxPerHour": self.maxPerHour,
        }


class Achievement(object):
    __slots__ = ("id", "title", "description", "hidden")

    def __init__(self, id, title, description, hidden=False):
        self.id = id
        self.title = title
        self.description = description
        self.hidden = hidden


class GameDeclarations(object):
    def __init__(self, slug, boards, achievements):
        self.slug = slug
        self.boards = dict((board.id, board) for board in boards)
        self.boardOrder = [board.id for board in boards]
        self.achievements = dict((achievement.id, achievement) for achievement in achievements)
        self.achievementOrder = [achievement.id for achievement in achievements]

    def board(self, boardId):
        return self.boards.get(boardId)

    def achievement(self, achievementId):
        return self.achievements.get(achievementId)

    def listBoards(self):
        return [self.boards[boardId] for boardId in self.boardOrder]

    def listAchievements(self):
        return [self.achievements[achievementId] for achievementId in self.achievementOrder]


class Declarations(object):
    """Every game's declarations, by slug."""

    def __init__(self, games):
        self.games = dict(games)

    def get(self, slug):
        return self.games.get(slug)

    def __len__(self):
        return len(self.games)

    def __iter__(self):
        return iter(sorted(self.games))


def _stripComment(line):
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
        raise BoardsError("line %d: a value is required" % lineNumber)
    if text[0] in "\"'":
        if len(text) < 2 or text[-1] != text[0]:
            raise BoardsError("line %d: unterminated quoted value %s" % (lineNumber, text))
        inner = text[1:-1]
        if text[0] in inner:
            raise BoardsError("line %d: escaped quotes are not supported: %s" % (lineNumber, text))
        return inner
    if text[0] in "[{&*!|>%@`":
        raise BoardsError("line %d: unsupported value %s" % (lineNumber, text))
    return text


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def parse(text):
    """Parse boards.yaml text into {slug: (lineNumber, {section: [(lineNumber, dict)]})}."""
    games = {}
    seenHeader = False
    empty = False
    slug = None
    section = None
    item = None
    for lineNumber, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw:
            raise BoardsError("line %d: tabs are not allowed; indent with spaces" % lineNumber)
        line = _stripComment(raw).rstrip()
        if not line.strip():
            continue
        if not seenHeader:
            if line in ("games:", "games: {}"):
                seenHeader = True
                empty = line == "games: {}"
                continue
            raise BoardsError("line %d: the file must start with 'games:'" % lineNumber)
        if empty:
            raise BoardsError("line %d: content after 'games: {}'" % lineNumber)
        indent = _indent(line)
        body = line.strip()
        if indent == 2:
            if not body.endswith(":") or " " in body:
                raise BoardsError("line %d: expected '<slug>:' at two spaces, got %r" % (lineNumber, body))
            slug = body[:-1]
            if not SLUG_PATTERN.match(slug) or slug in RESERVED_SLUGS:
                raise BoardsError("line %d: %r is not a valid arcade slug" % (lineNumber, slug))
            if slug in games:
                raise BoardsError("line %d: %r is declared twice" % (lineNumber, slug))
            games[slug] = (lineNumber, {})
            section = item = None
        elif indent == 4:
            if slug is None:
                raise BoardsError("line %d: a section outside any game" % lineNumber)
            if body not in ("boards:", "achievements:"):
                raise BoardsError(
                    "line %d: expected 'boards:' or 'achievements:' at four spaces, got %r" % (lineNumber, body)
                )
            section = body[:-1]
            if section in games[slug][1]:
                raise BoardsError("line %d: %r given twice for %r" % (lineNumber, section, slug))
            games[slug][1][section] = []
            item = None
        elif indent == 6 and body.startswith("- "):
            if section is None:
                raise BoardsError("line %d: an item outside 'boards:' or 'achievements:'" % lineNumber)
            item = {}
            games[slug][1][section].append((lineNumber, item))
            _keyValue(body[2:], item, lineNumber)
        elif indent == 8:
            if item is None:
                raise BoardsError("line %d: a key outside any '- ' item" % lineNumber)
            _keyValue(body, item, lineNumber)
        else:
            raise BoardsError(
                "line %d: unexpected indentation (games at 2 spaces, sections at 4, "
                "'- ' items at 6, their keys at 8)" % lineNumber
            )
    if not seenHeader:
        raise BoardsError("the file is empty; it must contain at least 'games: {}'")
    return games


def _keyValue(body, item, lineNumber):
    match = _KEY_VALUE.match(body)
    if not match:
        raise BoardsError("line %d: expected 'key: value', got %r" % (lineNumber, body))
    key, value = match.group(1), match.group(2) or ""
    if key in item:
        raise BoardsError("line %d: %r given twice in one item" % (lineNumber, key))
    item[key] = (lineNumber, _scalar(value, lineNumber))


def _boolean(entry, key, default):
    if key not in entry:
        return default
    lineNumber, text = entry[key]
    value = _BOOLEANS.get(text.lower())
    if value is None:
        raise BoardsError("line %d: %s must be true or false, got %r" % (lineNumber, key, text))
    return value


def _number(entry, key, integer):
    lineNumber, text = entry[key]
    if not _NUMBER.match(text):
        raise BoardsError("line %d: %s must be a number, got %r" % (lineNumber, key, text))
    if integer:
        if "." in text or "e" in text.lower():
            raise BoardsError("line %d: %s must be a whole number on an integer board" % (lineNumber, key))
        return int(text)
    value = float(text)
    if not math.isfinite(value):
        raise BoardsError("line %d: %s must be finite" % (lineNumber, key))
    return value if ("." in text or "e" in text.lower()) else int(text)


def _text(entry, key, limit):
    lineNumber, text = entry[key]
    if len(text) > limit:
        raise BoardsError("line %d: %s is longer than %d characters" % (lineNumber, key, limit))
    return text


def _checkKeys(entry, known, required, lineNumber, what):
    for key, (keyLine, _) in entry.items():
        if key not in known:
            raise BoardsError("line %d: unknown %s key %r (known: %s)" % (keyLine, what, key, ", ".join(known)))
    missing = [key for key in required if key not in entry]
    if missing:
        raise BoardsError("line %d: %s is missing %s" % (lineNumber, what, ", ".join(missing)))


def _id(entry, seen, lineNumber, what):
    idLine, identifier = entry["id"]
    if not ID_PATTERN.match(identifier):
        raise BoardsError("line %d: %s id %r must match %s" % (idLine, what, identifier, ID_PATTERN.pattern))
    if identifier in seen:
        raise BoardsError("line %d: %s id %r is already declared on line %d" % (idLine, what, identifier, seen[identifier]))
    seen[identifier] = lineNumber
    return identifier


def _board(lineNumber, entry, seen):
    _checkKeys(entry, BOARD_KEYS, BOARD_REQUIRED, lineNumber, "board")
    identifier = _id(entry, seen, lineNumber, "board")
    orderLine, order = entry["order"]
    if order not in ORDERS:
        raise BoardsError("line %d: order must be asc or desc, got %r" % (orderLine, order))
    integer = _boolean(entry, "integer", False)
    low = _number(entry, "min", integer)
    high = _number(entry, "max", integer)
    if low > high:
        raise BoardsError("line %d: board %r has min greater than max" % (lineNumber, identifier))
    maxPerHour = DEFAULT_MAX_PER_HOUR
    if "maxPerHour" in entry:
        rateLine, text = entry["maxPerHour"]
        if not text.isdigit() or not 1 <= int(text) <= 3600:
            raise BoardsError("line %d: maxPerHour must be a whole number from 1 to 3600" % rateLine)
        maxPerHour = int(text)
    return Board(
        id=identifier,
        title=_text(entry, "title", MAX_TITLE),
        order=order,
        unit=_text(entry, "unit", MAX_UNIT) if "unit" in entry else None,
        integer=integer,
        min=low,
        max=high,
        maxPerHour=maxPerHour,
    )


def _achievement(lineNumber, entry, seen):
    _checkKeys(entry, ACHIEVEMENT_KEYS, ACHIEVEMENT_REQUIRED, lineNumber, "achievement")
    return Achievement(
        id=_id(entry, seen, lineNumber, "achievement"),
        title=_text(entry, "title", MAX_TITLE),
        description=_text(entry, "description", MAX_DESCRIPTION),
        hidden=_boolean(entry, "hidden", False),
    )


def validate(parsed):
    games = {}
    for slug, (lineNumber, sections) in parsed.items():
        seenBoards, seenAchievements = {}, {}
        boardList = [_board(line, entry, seenBoards) for line, entry in sections.get("boards", [])]
        achievementList = [
            _achievement(line, entry, seenAchievements) for line, entry in sections.get("achievements", [])
        ]
        if not boardList and not achievementList:
            raise BoardsError("line %d: %r declares no boards and no achievements" % (lineNumber, slug))
        games[slug] = GameDeclarations(slug, boardList, achievementList)
    return Declarations(games)


def loads(text):
    return validate(parse(text))


def load(path):
    with open(path, "r", encoding="utf-8") as boardsFile:
        return loads(boardsFile.read())
