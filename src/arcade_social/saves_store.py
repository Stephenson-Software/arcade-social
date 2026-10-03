# @author Daniel McCoy Stephenson
"""The cloud-saves database (RFC 0016 §3): a second SQLite file, apart from
arcade-social.sqlite3, so a bug or migration in one cannot corrupt the other
and each is backed up on its own.

    enrollment  (player, slug, store)        who backs up which game
    blob        (player, sha256) -> bytes    one unit's canonical encoding, stored once per player
    version     one immutable copy of a player's whole save set for one game, with its parent
    unit        (version, name) -> sha256    which units a version holds
    head        (player, slug, store) -> version

`version`, `unit` and `blob` are append-only: nothing in this module updates
them, and nothing deletes them except a player's own deletion (deleteGame,
deletePlayer). `head` is the only row ever updated, only in the transaction
that inserts the version it points to, and only if it still points at the
upload's parent (compare-and-set). There is no automatic pruning.

`player_id` is arcade-social.sqlite3's player.id, referenced across files (no
foreign key can span two files). No row stores an IP address or user agent;
`device` is a random id the page generates and `device_label` a name the
player sees.
"""

import json
import sqlite3
from contextlib import contextmanager

from arcade_social.store import migrate, nowMillis

MIGRATIONS = (
    (
        1,
        "cloud saves (RFC 0016)",
        (
            """CREATE TABLE enrollment (
                player_id INTEGER NOT NULL,
                slug TEXT NOT NULL,
                store TEXT NOT NULL,
                enrolled_at INTEGER NOT NULL,
                PRIMARY KEY (player_id, slug, store)
            )""",
            """CREATE TABLE blob (
                player_id INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL,
                bytes BLOB NOT NULL,
                PRIMARY KEY (player_id, sha256)
            )""",
            """CREATE TABLE version (
                id INTEGER PRIMARY KEY,
                player_id INTEGER NOT NULL,
                slug TEXT NOT NULL,
                store TEXT NOT NULL,
                parent_id INTEGER,
                device TEXT NOT NULL,
                device_label TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                origin TEXT NOT NULL,
                kind TEXT NOT NULL,
                format TEXT NOT NULL,
                total_size INTEGER NOT NULL,
                unit_count INTEGER NOT NULL,
                removed TEXT NOT NULL DEFAULT '[]',
                pinned INTEGER NOT NULL DEFAULT 0
            )""",
            "CREATE INDEX version_game ON version (player_id, slug, store, id)",
            """CREATE TABLE unit (
                version_id INTEGER NOT NULL REFERENCES version(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL,
                PRIMARY KEY (version_id, name)
            )""",
            "CREATE INDEX unit_sha ON unit (sha256)",
            """CREATE TABLE head (
                player_id INTEGER NOT NULL,
                slug TEXT NOT NULL,
                store TEXT NOT NULL,
                version_id INTEGER NOT NULL REFERENCES version(id),
                PRIMARY KEY (player_id, slug, store)
            )""",
        ),
    ),
)


class Stale(Exception):
    """The upload's parent is not the current head. Nothing was written."""

    def __init__(self, head):
        super().__init__("stale parent")
        self.head = head


class SavesStore(object):
    def __init__(self, path, clock=nowMillis, migrations=MIGRATIONS):
        self.path = path
        self.clock = clock
        connection = self._connect()
        try:
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if path != ":memory:" and str(mode).lower() != "wal":
                raise RuntimeError("SQLite refused WAL mode for %s (got %s)" % (path, mode))
            self.migrated = migrate(connection, migrations, clock)
        finally:
            connection.close()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        # FULL: a version the server answered 201 for survives a power cut.
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def reading(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def writing(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        finally:
            connection.close()

    # --- enrollment ------------------------------------------------------------------

    def enrolled(self, playerId, slug, store):
        with self.reading() as connection:
            return self._enrolled(connection, playerId, slug, store)

    def _enrolled(self, connection, playerId, slug, store):
        return (
            connection.execute(
                "SELECT 1 FROM enrollment WHERE player_id = ? AND slug = ? AND store = ?", (playerId, slug, store)
            ).fetchone()
            is not None
        )

    def enroll(self, playerId, slug, store):
        """Idempotent. Returns True if this call enrolled the player."""
        with self.writing() as connection:
            return (
                connection.execute(
                    "INSERT OR IGNORE INTO enrollment (player_id, slug, store, enrolled_at) VALUES (?, ?, ?, ?)",
                    (playerId, slug, store, self.clock()),
                ).rowcount
                == 1
            )

    def unenroll(self, playerId, slug, store):
        """Stop backing up. Every version is kept."""
        with self.writing() as connection:
            return (
                connection.execute(
                    "DELETE FROM enrollment WHERE player_id = ? AND slug = ? AND store = ?", (playerId, slug, store)
                ).rowcount
                == 1
            )

    # --- reads ----------------------------------------------------------------------

    def _units(self, connection, versionId):
        return [
            {"name": row["name"], "sha256": row["sha256"], "size": row["size"]}
            for row in connection.execute(
                "SELECT name, sha256, size FROM unit WHERE version_id = ? ORDER BY name", (versionId,)
            ).fetchall()
        ]

    def _describe(self, connection, row):
        return {
            "id": row["id"],
            "parent": row["parent_id"],
            "createdAt": row["created_at"],
            "device": row["device"],
            "deviceLabel": row["device_label"],
            "origin": row["origin"],
            "kind": row["kind"],
            "format": row["format"],
            "totalSize": row["total_size"],
            "unitCount": row["unit_count"],
            "removed": json.loads(row["removed"]),
            "pinned": bool(row["pinned"]),
            "units": self._units(connection, row["id"]),
        }

    def _headId(self, connection, playerId, slug, store):
        row = connection.execute(
            "SELECT version_id FROM head WHERE player_id = ? AND slug = ? AND store = ?", (playerId, slug, store)
        ).fetchone()
        return row["version_id"] if row is not None else None

    def _head(self, connection, playerId, slug, store):
        headId = self._headId(connection, playerId, slug, store)
        if headId is None:
            return None
        row = connection.execute("SELECT * FROM version WHERE id = ?", (headId,)).fetchone()
        return self._describe(connection, row)

    def head(self, playerId, slug, store):
        """The newest version (with its units), or None if nothing was uploaded."""
        with self.reading() as connection:
            return self._head(connection, playerId, slug, store)

    def versions(self, playerId, slug, store, limit=200):
        with self.reading() as connection:
            rows = connection.execute(
                "SELECT * FROM version WHERE player_id = ? AND slug = ? AND store = ? ORDER BY id DESC LIMIT ?",
                (playerId, slug, store, limit),
            ).fetchall()
            return [self._describe(connection, row) for row in rows]

    def version(self, playerId, slug, store, versionId):
        """(description, {unit name: canonical bytes}) or None. Only this
        player's own version of this game: an id from another account or game
        is not found."""
        with self.reading() as connection:
            row = connection.execute(
                "SELECT * FROM version WHERE id = ? AND player_id = ? AND slug = ? AND store = ?",
                (versionId, playerId, slug, store),
            ).fetchone()
            if row is None:
                return None
            description = self._describe(connection, row)
            contents = {}
            for unit in description["units"]:
                blob = connection.execute(
                    "SELECT bytes FROM blob WHERE player_id = ? AND sha256 = ?", (playerId, unit["sha256"])
                ).fetchone()
                if blob is None:
                    raise RuntimeError("version %d refers to a missing blob" % versionId)
                contents[unit["name"]] = bytes(blob["bytes"])
            return description, contents

    def storedBytes(self, playerId, slug=None, store=None):
        """Bytes stored for a player (one game, or all), after dedupe."""
        with self.reading() as connection:
            return self._storedBytes(connection, playerId, slug, store)

    def _storedBytes(self, connection, playerId, slug=None, store=None):
        if slug is None:
            return connection.execute(
                "SELECT COALESCE(SUM(size), 0) FROM blob WHERE player_id = ?", (playerId,)
            ).fetchone()[0]
        return connection.execute(
            "SELECT COALESCE(SUM(size), 0) FROM (SELECT DISTINCT u.sha256, u.size FROM unit u "
            "JOIN version v ON v.id = u.version_id WHERE v.player_id = ? AND v.slug = ? AND v.store = ?)",
            (playerId, slug, store),
        ).fetchone()[0]

    def _gameShas(self, connection, playerId, slug, store):
        return set(
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT u.sha256 FROM unit u JOIN version v ON v.id = u.version_id "
                "WHERE v.player_id = ? AND v.slug = ? AND v.store = ?",
                (playerId, slug, store),
            ).fetchall()
        )

    def _playerShas(self, connection, playerId):
        return set(
            row[0] for row in connection.execute("SELECT sha256 FROM blob WHERE player_id = ?", (playerId,)).fetchall()
        )

    # --- the one write path -------------------------------------------------------------

    def commit(self, playerId, slug, store, parent, units, meta, check):
        """Insert a version and move head, in one transaction.

        units: {name: (sha256, canonical bytes)}. meta: device, deviceLabel,
        origin, kind, removed. check(head, newGameBytes, newPlayerBytes,
        gameBytes, playerBytes) runs inside the transaction with the current
        head and may raise to refuse; nothing is written then.

        Raises Stale if parent is not the current head (nothing written).
        Returns (versionId, created): created is False when the content equals
        head's exactly and nothing was stored."""
        with self.writing() as connection:
            head = self._head(connection, playerId, slug, store)
            headId = head["id"] if head is not None else None
            if parent != headId:
                raise Stale(head)
            if head is not None and dict((unit["name"], unit["sha256"]) for unit in head["units"]) == dict(
                (name, sha) for name, (sha, _) in units.items()
            ):
                return headId, False
            gameShas = self._gameShas(connection, playerId, slug, store)
            playerShas = self._playerShas(connection, playerId)
            newForGame = dict((sha, len(data)) for sha, data in units.values() if sha not in gameShas)
            newForPlayer = dict((sha, len(data)) for sha, data in units.values() if sha not in playerShas)
            check(
                head,
                sum(newForGame.values()),
                sum(newForPlayer.values()),
                self._storedBytes(connection, playerId, slug, store),
                self._storedBytes(connection, playerId),
            )
            for sha, data in units.values():
                if sha in newForPlayer:
                    connection.execute(
                        "INSERT INTO blob (player_id, sha256, size, bytes) VALUES (?, ?, ?, ?)",
                        (playerId, sha, len(data), sqlite3.Binary(data)),
                    )
            cursor = connection.execute(
                "INSERT INTO version (player_id, slug, store, parent_id, device, device_label, created_at, origin, "
                "kind, format, total_size, unit_count, removed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    playerId,
                    slug,
                    store,
                    parent,
                    meta["device"],
                    meta["deviceLabel"],
                    self.clock(),
                    meta["origin"],
                    meta["kind"],
                    meta["format"],
                    sum(len(data) for _, data in units.values()),
                    len(units),
                    json.dumps(sorted(meta.get("removed") or [])),
                ),
            )
            versionId = cursor.lastrowid
            for name, (sha, data) in sorted(units.items()):
                connection.execute(
                    "INSERT INTO unit (version_id, name, sha256, size) VALUES (?, ?, ?, ?)",
                    (versionId, name, sha, len(data)),
                )
            if headId is None:
                connection.execute(
                    "INSERT INTO head (player_id, slug, store, version_id) VALUES (?, ?, ?, ?)",
                    (playerId, slug, store, versionId),
                )
            else:
                moved = connection.execute(
                    "UPDATE head SET version_id = ? WHERE player_id = ? AND slug = ? AND store = ? AND version_id = ?",
                    (versionId, playerId, slug, store, headId),
                ).rowcount
                if moved != 1:
                    # Unreachable under BEGIN IMMEDIATE; refuse rather than guess.
                    raise Stale(self._head(connection, playerId, slug, store))
            return versionId, True

    def pin(self, playerId, slug, store, versionId):
        with self.writing() as connection:
            return (
                connection.execute(
                    "UPDATE version SET pinned = 1 WHERE id = ? AND player_id = ? AND slug = ? AND store = ?",
                    (versionId, playerId, slug, store),
                ).rowcount
                == 1
            )

    # --- deletion (the player's own, never automatic) ------------------------------------

    def _deleteBlobsNoLongerUsed(self, connection, playerId):
        return connection.execute(
            "DELETE FROM blob WHERE player_id = ? AND sha256 NOT IN "
            "(SELECT u.sha256 FROM unit u JOIN version v ON v.id = u.version_id WHERE v.player_id = ?)",
            (playerId, playerId),
        ).rowcount

    def deleteGame(self, playerId, slug, store):
        """Every version of one game for one player. Returns the number of versions deleted."""
        with self.writing() as connection:
            connection.execute("DELETE FROM head WHERE player_id = ? AND slug = ? AND store = ?", (playerId, slug, store))
            connection.execute(
                "DELETE FROM enrollment WHERE player_id = ? AND slug = ? AND store = ?", (playerId, slug, store)
            )
            removed = connection.execute(
                "DELETE FROM version WHERE player_id = ? AND slug = ? AND store = ?", (playerId, slug, store)
            ).rowcount
            self._deleteBlobsNoLongerUsed(connection, playerId)
            return removed

    def deletePlayer(self, playerId):
        """Everything held for a player in this file."""
        with self.writing() as connection:
            removed = 0
            for table in ("head", "enrollment", "version", "blob"):
                removed += connection.execute("DELETE FROM %s WHERE player_id = ?" % table, (playerId,)).rowcount
            return removed

    def references(self, playerId):
        """Rows that still reference a player (for tests and the operator)."""
        with self.reading() as connection:
            return sum(
                connection.execute("SELECT COUNT(*) FROM %s WHERE player_id = ?" % table, (playerId,)).fetchone()[0]
                for table in ("head", "enrollment", "version", "blob")
            )

    # --- account page and operator -------------------------------------------------------

    def games(self, playerId):
        """[(slug, store, enrolled, versions, head id, stored bytes)] for every game a player has saves or enrollment for."""
        with self.reading() as connection:
            keys = set(
                (row[0], row[1])
                for row in connection.execute(
                    "SELECT slug, store FROM version WHERE player_id = ? UNION SELECT slug, store FROM enrollment "
                    "WHERE player_id = ?",
                    (playerId, playerId),
                ).fetchall()
            )
            result = []
            for slug, store in sorted(keys):
                count = connection.execute(
                    "SELECT COUNT(*) FROM version WHERE player_id = ? AND slug = ? AND store = ?", (playerId, slug, store)
                ).fetchone()[0]
                result.append(
                    {
                        "slug": slug,
                        "store": store,
                        "enrolled": self._enrolled(connection, playerId, slug, store),
                        "versions": count,
                        "head": self._headId(connection, playerId, slug, store),
                        "storedBytes": self._storedBytes(connection, playerId, slug, store),
                    }
                )
            return result

    def counts(self):
        with self.reading() as connection:
            return dict(
                (table, connection.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])
                for table in ("enrollment", "blob", "version", "unit", "head")
            )

    def integrityCheck(self):
        with self.reading() as connection:
            return connection.execute("PRAGMA integrity_check").fetchone()[0]

    def backup(self, destination):
        source = self._connect()
        try:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
