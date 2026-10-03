# @author Daniel McCoy Stephenson
"""Display names: what leaderboards show instead of the UserAuth username.

The owner decided (2026-10-03) that a player's UserAuth username stays
private and that leaderboards show a separate display name, chosen on first
sign-in. The rules:

- 3 to 20 characters: ASCII letters and digits, with single spaces, '_', '-'
  or '.' between them. ASCII only, so a name cannot borrow look-alike letters
  from another script.
- Unique by a folded key: lowercase, separators dropped, and the confusable
  characters 0/o and 1/i/l folded together, so "Dan_S", "dan s" and "DanS"
  are one name, and "Dan1el" collides with "Daniel".
- A small impersonation guard: names that claim to be staff or the owner
  ("admin", "moderator", "official", the owner's names) are refused for
  everyone but operators. A short profanity list is refused for everyone.
  Both are deliberately small; the operator can still exclude an account.
"""

import re

MIN_LENGTH = 3
MAX_LENGTH = 20
_SHAPE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9 _.\-]*[A-Za-z0-9])?$")
_DOUBLE_SEPARATOR = re.compile(r"[ _.\-]{2,}")
_FOLD = str.maketrans({"0": "o", "1": "l", "i": "l"})

# Refused when the folded key CONTAINS one of these (impersonation of staff or
# the owner). Folded with the same rules, so "Adm1n" is caught.
_IMPERSONATION_PARTS = ("admin", "moderator", "official", "danielstephenson", "dmccoystephenson")
# Refused when the folded key IS one of these.
_IMPERSONATION_WHOLE = (
    "mod",
    "mods",
    "root",
    "system",
    "support",
    "staff",
    "owner",
    "operator",
    "arcade",
    "anonymous",
    "deleted",
    "null",
    "undefined",
)
# A short list of unambiguous profanity, matched inside the folded key.
_PROFANITY = ("fuck", "shit", "cunt", "nigger", "nigga", "faggot", "whore", "rape")


def fold(text):
    return text.lower().translate(_FOLD)


def nameKey(name):
    """The uniqueness key of a display name."""
    return re.sub(r"[^a-z0-9]", "", fold(name))


_FOLDED_PARTS = tuple(nameKey(part) for part in _IMPERSONATION_PARTS)
_FOLDED_WHOLE = frozenset(nameKey(word) for word in _IMPERSONATION_WHOLE)
_FOLDED_PROFANITY = tuple(nameKey(word) for word in _PROFANITY)


class InvalidName(ValueError):
    """The name breaks a rule. The message says which, for the player."""


def check(name, operator=False):
    """Return the name, trimmed, or raise InvalidName."""
    if not isinstance(name, str):
        raise InvalidName("A display name is required.")
    name = name.strip()
    if not MIN_LENGTH <= len(name) <= MAX_LENGTH:
        raise InvalidName("A display name is %d to %d characters long." % (MIN_LENGTH, MAX_LENGTH))
    if not _SHAPE.match(name) or _DOUBLE_SEPARATOR.search(name):
        raise InvalidName(
            "Use letters and digits, with single spaces, underscores, hyphens or dots between them."
        )
    key = nameKey(name)
    if len(key) < MIN_LENGTH:
        raise InvalidName("A display name needs at least %d letters or digits." % MIN_LENGTH)
    if any(word in key for word in _FOLDED_PROFANITY):
        raise InvalidName("Please choose a different name.")
    if not operator and (key in _FOLDED_WHOLE or any(part in key for part in _FOLDED_PARTS)):
        raise InvalidName("That name is reserved; please choose a different one.")
    return name
