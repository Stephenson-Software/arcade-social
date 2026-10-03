# @author Daniel McCoy Stephenson
"""The SQLite store: players, display names, scores, achievements and likes.

One file on the /data volume, in WAL mode, opened per request (sqlite3 is in
the standard library; opening a connection is cheap). Every write runs in
BEGIN IMMEDIATE, so concurrent writers queue on SQLite's lock instead of
failing half way.

The schema is created by numbered, forward-only migrations recorded in
`schema_version`. A number, once released, is never reused or edited (a reused
Flyway number blocked the dansplugins.com likes deploy, RFC 0015 §Rollout); a
change is a new migration. A database newer than this code is refused rather
than used, so a rollback to an older image can never write to a schema it does
not understand.

No IP address, user agent or device identifier is stored anywhere.
"""

import sqlite3
import time
from contextlib import contextmanager

MIGRATIONS = (
    (
        1,
        "players and display names",
        (
            # The local mirror of UserAuth accounts (as DPC's users table and
            # RFC 0015's player_user): created on first sign-in, keyed by the
            # UserAuth username, which is never shown publicly.
            """CREATE TABLE player (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT,
                display_key TEXT UNIQUE,
                name_changed_at INTEGER,
                created_at INTEGER NOT NULL
            )""",
            # A released display name stays reserved for its previous holder for
            # a while, so a name change cannot be followed by someone else
            # taking the old name and passing as its owner.
            """CREATE TABLE name_hold (
                display_key TEXT PRIMARY KEY,
                player_id INTEGER NOT NULL REFERENCES player(id) ON DELETE CASCADE,
                released_at INTEGER NOT NULL
            )""",
        ),
    ),
    (
        2,
        "scores and achievements (RFC 0014)",
        (
            """CREATE TABLE score_best (
                id INTEGER PRIMARY KEY,
                player_id INTEGER NOT NULL REFERENCES player(id) ON DELETE CASCADE,
                slug TEXT NOT NULL,
                board TEXT NOT NULL,
                value NUMERIC NOT NULL,
                achieved_at INTEGER NOT NULL,
                UNIQUE (player_id, slug, board)
            )""",
            "CREATE INDEX score_best_board ON score_best (slug, board, value)",
            # Every submission, kept 90 days, so the operator can see what was
            # sent when cleaning a board (RFC 0014 §3).
            """CREATE TABLE score_log (
                id INTEGER PRIMARY KEY,
                player_id INTEGER NOT NULL REFERENCES player(id) ON DELETE CASCADE,
                slug TEXT NOT NULL,
                board TEXT NOT NULL,
                value NUMERIC,
                run TEXT,
                received_at INTEGER NOT NULL,
                accepted INTEGER NOT NULL,
                reason TEXT
            )""",
            "CREATE INDEX score_log_rate ON score_log (player_id, slug, board, received_at)",
            "CREATE INDEX score_log_age ON score_log (received_at)",
            """CREATE TABLE unlock (
                player_id INTEGER NOT NULL REFERENCES player(id) ON DELETE CASCADE,
                slug TEXT NOT NULL,
                achievement TEXT NOT NULL,
                unlocked_at INTEGER NOT NULL,
                PRIMARY KEY (player_id, slug, achievement)
            )""",
            "CREATE INDEX unlock_achievement ON unlock (slug, achievement)",
            # Hidden from every public board and percentage (RFC 0014 §3). Keyed
            # by username so an account can be excluded before it plays.
            """CREATE TABLE exclusion (
                username TEXT PRIMARY KEY,
                reason TEXT,
                created_at INTEGER NOT NULL
            )""",
        ),
    ),
    (
        3,
        "likes (RFC 0015)",
        (
            # One like per account per game, by constraint, as DPC's V10 likes
            # table does with UNIQUE (user_id, target_type, target_id).
            """CREATE TABLE game_like (
                id INTEGER PRIMARY KEY,
                player_id INTEGER NOT NULL REFERENCES player(id) ON DELETE CASCADE,
                slug TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                CONSTRAINT uq_game_like UNIQUE (player_id, slug)
            )""",
            "CREATE INDEX idx_game_like_slug ON game_like (slug)",
        ),
    ),
)

_EXCLUDED = "(SELECT p.id FROM player p JOIN exclusion e ON e.username = p.username)"


class SchemaTooNew(RuntimeError):
    """The database was written by a newer arcade-social."""


class NameTaken(ValueError):
    pass


class NameHeld(ValueError):
    pass


class NameTooSoon(ValueError):
    def __init__(self, nextChangeAt):
        super().__init__("too soon")
        self.nextChangeAt = nextChangeAt


def nowMillis():
    return int(time.time() * 1000)


def migrate(connection, migrations=MIGRATIONS, clock=nowMillis):
    """Apply every migration newer than the database, in one transaction.
    Returns (before, after)."""
    numbers = [number for number, _, _ in migrations]
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError("migrations must be numbered 1, 2, 3 ... without gaps: %r" % numbers)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at INTEGER NOT NULL)"
    )
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
        if current > len(numbers):
            raise SchemaTooNew(
                "the database is at schema version %d; this arcade-social knows only up to %d. "
                "Refusing to touch it: run the newer image, or restore a backup." % (current, len(numbers))
            )
        for number, description, statements in migrations:
            if number <= current:
                continue
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_version (version, description, applied_at) VALUES (?, ?, ?)",
                (number, description, clock()),
            )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    return current, len(numbers)


class Store(object):
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
        # FULL: a committed score, like or name survives a power cut, not only
        # a crash of the process. The write rate here is tiny.
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

    def schemaVersion(self):
        with self.reading() as connection:
            return connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]

    # --- players ----------------------------------------------------------------

    def _player(self, connection, username):
        return connection.execute("SELECT * FROM player WHERE username = ?", (username,)).fetchone()

    def _ensurePlayer(self, connection, username):
        connection.execute(
            "INSERT OR IGNORE INTO player (username, created_at) VALUES (?, ?)", (username, self.clock())
        )
        return self._player(connection, username)

    def player(self, username):
        """The player row for a UserAuth username, or None. Never creates one."""
        with self.reading() as connection:
            return self._player(connection, username)

    def ensurePlayer(self, username):
        with self.writing() as connection:
            return self._ensurePlayer(connection, username)

    def setDisplayName(self, username, name, key, changeDays, holdDays):
        """Set or change a player's display name. Raises NameTaken, NameHeld or
        NameTooSoon. Returns the player row."""
        now = self.clock()
        with self.writing() as connection:
            player = self._ensurePlayer(connection, username)
            if player["display_key"] == key:
                # Same name (perhaps re-cased or re-spaced): not a change.
                connection.execute("UPDATE player SET display_name = ? WHERE id = ?", (name, player["id"]))
                return self._player(connection, username)
            if player["display_name"] is not None and player["name_changed_at"] is not None:
                nextChange = player["name_changed_at"] + changeDays * 86400000
                if now < nextChange:
                    raise NameTooSoon(nextChange)
            hold = connection.execute(
                "SELECT player_id, released_at FROM name_hold WHERE display_key = ?", (key,)
            ).fetchone()
            if hold is not None and hold["player_id"] != player["id"]:
                if now < hold["released_at"] + holdDays * 86400000:
                    raise NameHeld(key)
            if player["display_key"] is not None:
                connection.execute(
                    "INSERT OR REPLACE INTO name_hold (display_key, player_id, released_at) VALUES (?, ?, ?)",
                    (player["display_key"], player["id"], now),
                )
            connection.execute("DELETE FROM name_hold WHERE display_key = ?", (key,))
            try:
                connection.execute(
                    "UPDATE player SET display_name = ?, display_key = ?, name_changed_at = ? WHERE id = ?",
                    (name, key, now if player["display_name"] is not None else None, player["id"]),
                )
            except sqlite3.IntegrityError:
                raise NameTaken(key)
            return self._player(connection, username)

    def deletePlayer(self, username):
        """Delete everything this service holds about an account: its mirror
        row and, by cascade, its names on hold, scores, submission log,
        unlocks and likes. Returns True if there was anything to delete."""
        with self.writing() as connection:
            cursor = connection.execute("DELETE FROM player WHERE username = ?", (username,))
            return cursor.rowcount > 0

    def summary(self, username):
        """Counts of what is held for an account (the account page)."""
        with self.reading() as connection:
            player = self._player(connection, username)
            if player is None:
                return {"scores": 0, "unlocks": 0, "likes": 0}
            pid = player["id"]
            return {
                "scores": connection.execute("SELECT COUNT(*) FROM score_best WHERE player_id = ?", (pid,)).fetchone()[0],
                "unlocks": connection.execute("SELECT COUNT(*) FROM unlock WHERE player_id = ?", (pid,)).fetchone()[0],
                "likes": connection.execute("SELECT COUNT(*) FROM game_like WHERE player_id = ?", (pid,)).fetchone()[0],
            }

    def export(self, username):
        """Everything held about an account, for the player to download."""
        with self.reading() as connection:
            player = self._player(connection, username)
            if player is None:
                return None
            pid = player["id"]

            def rows(sql):
                return [dict(row) for row in connection.execute(sql, (pid,)).fetchall()]

            return {
                "username": player["username"],
                "displayName": player["display_name"],
                "createdAt": player["created_at"],
                "displayNameChangedAt": player["name_changed_at"],
                "scores": rows("SELECT slug, board, value, achieved_at FROM score_best WHERE player_id = ? ORDER BY slug, board"),
                "submissions": rows(
                    "SELECT slug, board, value, run, received_at, accepted, reason FROM score_log "
                    "WHERE player_id = ? ORDER BY received_at"
                ),
                "achievements": rows("SELECT slug, achievement, unlocked_at FROM unlock WHERE player_id = ? ORDER BY slug, unlocked_at"),
                "likes": rows("SELECT slug, created_at FROM game_like WHERE player_id = ? ORDER BY created_at"),
            }

    # --- scores -------------------------------------------------------------------

    def submissionsInLastHour(self, playerId, slug, board):
        with self.reading() as connection:
            return self._recent(connection, playerId, slug, board)

    def _recent(self, connection, playerId, slug, board):
        return connection.execute(
            "SELECT COUNT(*) FROM score_log WHERE player_id = ? AND slug = ? AND board = ? AND received_at > ?",
            (playerId, slug, board, self.clock() - 3600000),
        ).fetchone()[0]

    def logRefused(self, playerId, slug, board, value, run, reason):
        with self.writing() as connection:
            connection.execute(
                "INSERT INTO score_log (player_id, slug, board, value, run, received_at, accepted, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                (playerId, slug, board, value, run, self.clock(), reason),
            )

    def submitScore(self, playerId, slug, board, value, run=None):
        """Record a submission and keep the player's best. Returns
        {"best", "improved", "rank"}, or None when the board's maxPerHour for
        this player is used up (nothing is recorded then)."""
        now = self.clock()
        with self.writing() as connection:
            if self._recent(connection, playerId, slug, board.id) >= board.maxPerHour:
                return None
            connection.execute(
                "INSERT INTO score_log (player_id, slug, board, value, run, received_at, accepted, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, NULL)",
                (playerId, slug, board.id, value, run, now),
            )
            existing = connection.execute(
                "SELECT id, value FROM score_best WHERE player_id = ? AND slug = ? AND board = ?",
                (playerId, slug, board.id),
            ).fetchone()
            improved = existing is None or board.better(value, existing["value"])
            if existing is None:
                connection.execute(
                    "INSERT INTO score_best (player_id, slug, board, value, achieved_at) VALUES (?, ?, ?, ?, ?)",
                    (playerId, slug, board.id, value, now),
                )
            elif improved:
                connection.execute(
                    "UPDATE score_best SET value = ?, achieved_at = ? WHERE id = ?", (value, now, existing["id"])
                )
            best = value if improved else existing["value"]
            return {"best": best, "improved": improved, "rank": self._rank(connection, playerId, slug, board, best)}

    def _rank(self, connection, playerId, slug, board, value):
        """Competition rank (1, 2, 2, 4): one more than the number of visible
        entries strictly better. None for an excluded player."""
        excluded = connection.execute(
            "SELECT 1 FROM exclusion e JOIN player p ON p.username = e.username WHERE p.id = ?", (playerId,)
        ).fetchone()
        if excluded is not None:
            return None
        return self._rankOfValue(connection, slug, board, value)

    def _visibleOrder(self, board):
        direction = "DESC" if board.order == "desc" else "ASC"
        # Equal values: whoever reached it first is listed first.
        return "s.value %s, s.achieved_at ASC, s.id ASC" % direction

    def _entries(self, connection, slug, board, limit, offset):
        rows = connection.execute(
            "SELECT s.id, s.value, s.achieved_at, p.display_name FROM score_best s "
            "JOIN player p ON p.id = s.player_id "
            "WHERE s.slug = ? AND s.board = ? AND p.display_name IS NOT NULL AND s.player_id NOT IN %s "
            "ORDER BY %s LIMIT ? OFFSET ?" % (_EXCLUDED, self._visibleOrder(board)),
            (slug, board.id, limit, offset),
        ).fetchall()
        entries = []
        previous = None
        for index, row in enumerate(rows):
            if previous is not None and row["value"] == previous["value"]:
                rank = previous["rank"]
            elif previous is not None or offset == 0:
                # Every visible entry above this one is strictly better.
                rank = offset + index + 1
            else:
                # The first row of a window that starts mid-board: the entry
                # above it may hold the same value, so count.
                rank = self._rankOfValue(connection, slug, board, row["value"])
            entry = {
                "id": row["id"],
                "rank": rank,
                "displayName": row["display_name"],
                "value": row["value"],
                "achievedAt": row["achieved_at"],
            }
            entries.append(entry)
            previous = entry
        return entries

    def _rankOfValue(self, connection, slug, board, value):
        operator = ">" if board.order == "desc" else "<"
        return (
            connection.execute(
                "SELECT COUNT(*) FROM score_best s JOIN player p ON p.id = s.player_id "
                "WHERE s.slug = ? AND s.board = ? AND s.value %s ? AND p.display_name IS NOT NULL "
                "AND s.player_id NOT IN %s" % (operator, _EXCLUDED),
                (slug, board.id, value),
            ).fetchone()[0]
            + 1
        )

    def top(self, slug, board, limit):
        with self.reading() as connection:
            entries = self._entries(connection, slug, board, limit, 0)
            total = self._visibleCount(connection, slug, board)
            return entries, total

    def _visibleCount(self, connection, slug, board):
        return connection.execute(
            "SELECT COUNT(*) FROM score_best s JOIN player p ON p.id = s.player_id "
            "WHERE s.slug = ? AND s.board = ? AND p.display_name IS NOT NULL AND s.player_id NOT IN %s"
            % _EXCLUDED,
            (slug, board.id),
        ).fetchone()[0]

    def aroundPlayer(self, playerId, slug, board, window):
        """The entries around a player's own: (entries, me) where me is the
        player's entry id, or ([], None) if they have none or are hidden."""
        with self.reading() as connection:
            mine = connection.execute(
                "SELECT s.id, s.value, s.achieved_at FROM score_best s WHERE s.player_id = ? AND s.slug = ? "
                "AND s.board = ? AND s.player_id NOT IN %s" % _EXCLUDED,
                (playerId, slug, board.id),
            ).fetchone()
            if mine is None:
                return [], None
            operator = ">" if board.order == "desc" else "<"
            position = connection.execute(
                "SELECT COUNT(*) FROM score_best s JOIN player p ON p.id = s.player_id "
                "WHERE s.slug = ? AND s.board = ? AND p.display_name IS NOT NULL AND s.player_id NOT IN %s "
                "AND (s.value %s ? OR (s.value = ? AND (s.achieved_at < ? OR (s.achieved_at = ? AND s.id < ?))))"
                % (_EXCLUDED, operator),
                (slug, board.id, mine["value"], mine["value"], mine["achieved_at"], mine["achieved_at"], mine["id"]),
            ).fetchone()[0]
            offset = max(0, position - window)
            return self._entries(connection, slug, board, 2 * window + 1, offset), mine["id"]

    def playerBests(self, playerId, slug, declarations):
        """A player's best on each declared board of a game, with rank."""
        with self.reading() as connection:
            rows = connection.execute(
                "SELECT board, value, achieved_at FROM score_best WHERE player_id = ? AND slug = ?", (playerId, slug)
            ).fetchall()
            bests = []
            for row in rows:
                board = declarations.board(row["board"]) if declarations else None
                if board is None:
                    continue
                bests.append(
                    {
                        "board": row["board"],
                        "value": row["value"],
                        "achievedAt": row["achieved_at"],
                        "rank": self._rank(connection, playerId, slug, board, row["value"]),
                    }
                )
            return sorted(bests, key=lambda best: best["board"])

    def deleteGameData(self, playerId, slug):
        """A player's scores, submission log and unlocks for one game."""
        with self.writing() as connection:
            removed = 0
            for table in ("score_best", "score_log", "unlock"):
                removed += connection.execute(
                    "DELETE FROM %s WHERE player_id = ? AND slug = ?" % table, (playerId, slug)
                ).rowcount
            return removed

    def pruneLog(self, retentionDays):
        with self.writing() as connection:
            return connection.execute(
                "DELETE FROM score_log WHERE received_at < ?", (self.clock() - retentionDays * 86400000,)
            ).rowcount

    # --- achievements ---------------------------------------------------------------

    def unlock(self, playerId, slug, achievementId):
        """Idempotent. Returns (newlyUnlocked, unlockedAt)."""
        now = self.clock()
        with self.writing() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO unlock (player_id, slug, achievement, unlocked_at) VALUES (?, ?, ?, ?)",
                (playerId, slug, achievementId, now),
            )
            row = connection.execute(
                "SELECT unlocked_at FROM unlock WHERE player_id = ? AND slug = ? AND achievement = ?",
                (playerId, slug, achievementId),
            ).fetchone()
            return cursor.rowcount == 1, row["unlocked_at"]

    def playerUnlocks(self, playerId, slug):
        with self.reading() as connection:
            return [
                {"achievement": row["achievement"], "unlockedAt": row["unlocked_at"]}
                for row in connection.execute(
                    "SELECT achievement, unlocked_at FROM unlock WHERE player_id = ? AND slug = ? ORDER BY unlocked_at",
                    (playerId, slug),
                ).fetchall()
            ]

    def achievementShares(self, slug):
        """(players, {achievement: count}). Players are the accounts with any
        score or unlock in the game; excluded accounts count in neither."""
        with self.reading() as connection:
            players = connection.execute(
                "SELECT COUNT(*) FROM (SELECT player_id FROM score_best WHERE slug = ? "
                "UNION SELECT player_id FROM unlock WHERE slug = ?) WHERE player_id NOT IN %s" % _EXCLUDED,
                (slug, slug),
            ).fetchone()[0]
            counts = dict(
                (row[0], row[1])
                for row in connection.execute(
                    "SELECT achievement, COUNT(*) FROM unlock WHERE slug = ? AND player_id NOT IN %s "
                    "GROUP BY achievement" % _EXCLUDED,
                    (slug,),
                ).fetchall()
            )
            return players, counts

    # --- likes ------------------------------------------------------------------------

    def _likeCount(self, connection, slug):
        return connection.execute("SELECT COUNT(*) FROM game_like WHERE slug = ?", (slug,)).fetchone()[0]

    def like(self, playerId, slug):
        """Idempotent: a second like is a no-op (UNIQUE (player_id, slug))."""
        with self.writing() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO game_like (player_id, slug, created_at) VALUES (?, ?, ?)",
                (playerId, slug, self.clock()),
            )
            return self._likeCount(connection, slug)

    def unlike(self, playerId, slug):
        with self.writing() as connection:
            connection.execute("DELETE FROM game_like WHERE player_id = ? AND slug = ?", (playerId, slug))
            return self._likeCount(connection, slug)

    def likeCounts(self):
        with self.reading() as connection:
            return dict(
                (row[0], row[1])
                for row in connection.execute("SELECT slug, COUNT(*) FROM game_like GROUP BY slug").fetchall()
            )

    def likedBy(self, playerId):
        with self.reading() as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT slug FROM game_like WHERE player_id = ? ORDER BY created_at, id", (playerId,)
                ).fetchall()
            ]

    # --- operator tools -----------------------------------------------------------------

    def entry(self, entryId):
        with self.reading() as connection:
            row = connection.execute(
                "SELECT s.id, s.slug, s.board, s.value, s.achieved_at, p.username, p.display_name "
                "FROM score_best s JOIN player p ON p.id = s.player_id WHERE s.id = ?",
                (entryId,),
            ).fetchone()
            return dict(row) if row is not None else None

    def deleteEntry(self, entryId):
        """Remove one leaderboard entry. Its submissions stay in the log."""
        with self.writing() as connection:
            return connection.execute("DELETE FROM score_best WHERE id = ?", (entryId,)).rowcount > 0

    def exclude(self, username, reason):
        with self.writing() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO exclusion (username, reason, created_at) VALUES (?, ?, ?)",
                (username, reason, self.clock()),
            )

    def unexclude(self, username):
        with self.writing() as connection:
            return connection.execute("DELETE FROM exclusion WHERE username = ?", (username,)).rowcount > 0

    def exclusions(self):
        with self.reading() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM exclusion ORDER BY created_at").fetchall()]

    def deleteLikesOf(self, username):
        with self.writing() as connection:
            return connection.execute(
                "DELETE FROM game_like WHERE player_id = (SELECT id FROM player WHERE username = ?)", (username,)
            ).rowcount

    def submissionLog(self, slug, board, limit):
        with self.reading() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT l.id, p.username, p.display_name, l.value, l.run, l.received_at, l.accepted, l.reason "
                    "FROM score_log l JOIN player p ON p.id = l.player_id WHERE l.slug = ? AND l.board = ? "
                    "ORDER BY l.received_at DESC, l.id DESC LIMIT ?",
                    (slug, board, limit),
                ).fetchall()
            ]

    # --- backup --------------------------------------------------------------------------

    def backup(self, destination):
        """An online, consistent copy of the database (sqlite3 backup API),
        safe while the server is writing."""
        source = self._connect()
        try:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
