# @author Daniel McCoy Stephenson
"""Live presence in the walkable arcade hall (RFC 0030 §4), in memory only.

A signed-in player who has turned on "Show me in the hall" sends their pose
about once a second; anyone with the hall open sees every opted-in player seen
in the last TTL seconds, under their display name. A pose is:

    position x, z (hall metres, rounded to 0.25 m), facing yaw (rounded to
    10 degrees), the cabinet being played (a slug or None) and the hall's
    layout key

Poses live in one dict in this process, keyed by player id, behind a lock.
They are never written to SQLite, a file, a log or a backup, and a restart
empties them. Each entry carries a random id made when the player appears and
kept only while they stay present, so a viewer can follow one figure for a
visit but cannot link two visits, and never learns a player id or username.
"""

import math
import secrets
import threading
import time

TTL_SECONDS = 10
# What one GET returns at most, newest first.
MAX_PEOPLE = 50
# Live entries held at most: beyond this a newcomer is refused until some expire.
MAX_ENTRIES = 1000
# |x| and |z| bound, in hall metres.
MAX_COORDINATE = 200.0
POSITION_STEP = 0.25
YAW_STEP = math.pi / 18  # 10 degrees


class Full(RuntimeError):
    """Too many people are present already."""


def quantise(x, z, yaw):
    """The pose as shared: position to 0.25 m, facing to 10 degrees in (-pi, pi]."""

    def step(value, size):
        return round(round(value / size) * size, 4)

    yaw = math.atan2(math.sin(yaw), math.cos(yaw))
    yaw = step(yaw, YAW_STEP)
    if yaw <= -math.pi:
        yaw = round(yaw + 2 * math.pi, 4)
    return step(x, POSITION_STEP) + 0.0, step(z, POSITION_STEP) + 0.0, yaw + 0.0


class _Entry(object):
    __slots__ = ("id", "name", "x", "z", "yaw", "cabinet", "layout", "seen")


class Presence(object):
    def __init__(self, clock=time.monotonic, ttl=TTL_SECONDS, maxEntries=MAX_ENTRIES):
        self._clock = clock
        self.ttl = ttl
        self._maxEntries = maxEntries
        self._lock = threading.Lock()
        self._entries = {}

    def update(self, playerId, name, x, z, yaw, cabinet, layout):
        """Record a player's pose. Returns their opaque presence id. Raises
        Full when the table is at its cap and the player is not in it."""
        x, z, yaw = quantise(x, z, yaw)
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(playerId)
            if entry is None:
                if len(self._entries) >= self._maxEntries:
                    raise Full("too many people in the hall")
                entry = _Entry()
                entry.id = secrets.token_urlsafe(9)
                self._entries[playerId] = entry
            entry.name = name
            entry.x, entry.z, entry.yaw = x, z, yaw
            entry.cabinet = cabinet
            entry.layout = layout
            entry.seen = now
            return entry.id

    def remove(self, playerId):
        """Forget a player now (opt-out, sign-out, deletion, exclusion)."""
        with self._lock:
            return self._entries.pop(playerId, None) is not None

    def snapshot(self, layout, limit=None):
        """[(playerId, person)] seen within the TTL in this layout, newest first.
        The player id is for the caller's checks only; it is never sent."""
        now = self._clock()
        with self._lock:
            self._prune(now)
            live = [(playerId, entry) for playerId, entry in self._entries.items() if entry.layout == layout]
            live.sort(key=lambda item: item[1].seen, reverse=True)
            return [
                (
                    playerId,
                    {"id": e.id, "name": e.name, "x": e.x, "z": e.z, "yaw": e.yaw, "cabinet": e.cabinet},
                )
                for playerId, e in (live if limit is None else live[:limit])
            ]

    def _prune(self, now):
        for playerId in [p for p, e in self._entries.items() if now - e.seen >= self.ttl]:
            del self._entries[playerId]

    def __len__(self):
        with self._lock:
            self._prune(self._clock())
            return len(self._entries)
