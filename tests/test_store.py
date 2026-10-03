# @author Daniel McCoy Stephenson
"""The SQLite store: migrations, display-name rules, uniqueness by constraint,
concurrency across threads, deletion, retention and the online backup."""

import sqlite3
import threading

import pytest

from arcade_social import boards
from arcade_social.store import MIGRATIONS, NameHeld, NameTaken, NameTooSoon, SchemaTooNew, Store

DAY = 86400000
BOARD = boards.Board("most-money", "Most", "desc", 0, 1000, integer=True, maxPerHour=1000)


class Clock(object):
    def __init__(self, now=10 * DAY):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return Store(str(tmp_path / "db.sqlite3"), clock=clock)


def _tables(path):
    connection = sqlite3.connect(path)
    try:
        return set(row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))
    finally:
        connection.close()


# --- migrations -----------------------------------------------------------------------


def test_fresh_database_is_migrated_to_the_latest_version_in_wal_mode(tmp_path):
    path = str(tmp_path / "db.sqlite3")
    store = Store(path)
    assert store.migrated == (0, len(MIGRATIONS))
    assert store.schemaVersion() == 3
    assert {"player", "name_hold", "score_best", "score_log", "unlock", "exclusion", "game_like", "schema_version"} <= _tables(path)
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    descriptions = [row[0] for row in connection.execute("SELECT description FROM schema_version ORDER BY version")]
    assert descriptions == [description for _, description, _ in MIGRATIONS]
    connection.close()
    # Opening again applies nothing.
    assert Store(path).migrated == (3, 3)


def test_forward_migration_keeps_existing_data(tmp_path):
    """A database at version 2 (no likes table yet) upgrades to 3 in place."""
    path = str(tmp_path / "db.sqlite3")
    old = Store(path, migrations=MIGRATIONS[:2])
    assert old.migrated == (0, 2)
    assert "game_like" not in _tables(path)
    player = old.ensurePlayer("alice")
    old.setDisplayName("alice", "Alice", "allce", 30, 30)
    old.submitScore(player["id"], "fishe", BOARD, 77)
    new = Store(path)
    assert new.migrated == (2, 3)
    assert "game_like" in _tables(path)
    assert new.player("alice")["display_name"] == "Alice"
    entries, total = new.top("fishe", BOARD, 10)
    assert total == 1 and entries[0]["value"] == 77
    assert new.like(player["id"], "fishe") == 1


def test_a_newer_database_is_refused(tmp_path):
    path = str(tmp_path / "db.sqlite3")
    Store(path)
    with pytest.raises(SchemaTooNew):
        Store(path, migrations=MIGRATIONS[:2])


def test_a_failing_migration_rolls_back_entirely(tmp_path):
    path = str(tmp_path / "db.sqlite3")
    Store(path, migrations=MIGRATIONS[:1])
    broken = MIGRATIONS[:1] + ((2, "broken", ("CREATE TABLE half_done (x INTEGER)", "THIS IS NOT SQL")),)
    with pytest.raises(sqlite3.OperationalError):
        Store(path, migrations=broken)
    assert "half_done" not in _tables(path)
    assert Store(path, migrations=MIGRATIONS[:1]).migrated == (1, 1)


def test_migration_numbers_must_be_contiguous(tmp_path):
    with pytest.raises(ValueError):
        Store(str(tmp_path / "db.sqlite3"), migrations=(MIGRATIONS[0], MIGRATIONS[2]))


def test_concurrent_first_start_migrates_once(tmp_path):
    path = str(tmp_path / "db.sqlite3")
    errors = []

    def start():
        try:
            Store(path)
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=start) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    connection = sqlite3.connect(path)
    assert [row[0] for row in connection.execute("SELECT version FROM schema_version")] == [1, 2, 3]
    connection.close()


# --- display names ----------------------------------------------------------------------


def test_display_names_are_unique_by_key(store):
    store.setDisplayName("alice", "Dan S", "dans", 30, 30)
    with pytest.raises(NameTaken):
        store.setDisplayName("bob", "dan_s", "dans", 30, 30)
    # Re-casing your own name is not a change and is always allowed.
    assert store.setDisplayName("alice", "DAN S", "dans", 30, 30)["display_name"] == "DAN S"


def test_name_changes_are_limited_and_old_names_held(store, clock):
    store.setDisplayName("alice", "First", "flrst", 30, 30)
    # The first choice does not start the clock; the first change does.
    store.setDisplayName("alice", "Second", "second", 30, 30)
    with pytest.raises(NameTooSoon) as error:
        store.setDisplayName("alice", "Third", "thlrd", 30, 30)
    assert error.value.nextChangeAt == clock.now + 30 * DAY
    # "First" is held for alice: bob cannot take it for 30 days.
    with pytest.raises(NameHeld):
        store.setDisplayName("bob", "First", "flrst", 30, 30)
    clock.now += 31 * DAY
    assert store.setDisplayName("bob", "First", "flrst", 30, 30)["display_name"] == "First"
    assert store.setDisplayName("alice", "Third", "thlrd", 30, 30)["display_name"] == "Third"


def test_a_held_name_can_be_taken_back_by_its_holder(store):
    store.setDisplayName("alice", "First", "flrst", 0, 30)
    store.setDisplayName("alice", "Second", "second", 0, 30)
    assert store.setDisplayName("alice", "First", "flrst", 0, 30)["display_name"] == "First"


# --- scores, likes: constraints and concurrency -----------------------------------------------


def test_like_uniqueness_is_a_database_constraint(store):
    player = store.ensurePlayer("alice")
    connection = sqlite3.connect(store.path)
    connection.execute("INSERT INTO game_like (player_id, slug, created_at) VALUES (?, 'fishe', 1)", (player["id"],))
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO game_like (player_id, slug, created_at) VALUES (?, 'fishe', 2)", (player["id"],))
    connection.close()


def test_concurrent_likes_from_one_player_count_once(store):
    player = store.ensurePlayer("alice")
    others = [store.ensurePlayer("p%d" % index) for index in range(10)]
    errors = []

    def like(playerId):
        try:
            for _ in range(5):
                store.like(playerId, "fishe")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=like, args=(player["id"],)) for _ in range(10)]
    threads += [threading.Thread(target=like, args=(other["id"],)) for other in others]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert store.likeCounts() == {"fishe": 11}
    assert store.likedBy(player["id"]) == ["fishe"]


def test_concurrent_score_submissions_keep_the_true_best(store):
    players = [store.ensurePlayer("p%d" % index) for index in range(4)]
    errors = []

    def submit(playerId, values):
        try:
            for value in values:
                store.submitScore(playerId, "fishe", BOARD, value)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = []
    for index, player in enumerate(players):
        for chunk in range(4):
            values = [(index * 100 + chunk * 25 + offset) % 1000 for offset in range(25)]
            threads.append(threading.Thread(target=submit, args=(player["id"], values)))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    for index, player in enumerate(players):
        expected = max((index * 100 + n) % 1000 for n in range(100))
        bests = store.playerBests(player["id"], "fishe", _Declarations())
        assert bests[0]["value"] == expected
    connection = sqlite3.connect(store.path)
    assert connection.execute("SELECT COUNT(*) FROM score_log").fetchone()[0] == 400
    assert connection.execute("SELECT COUNT(*) FROM score_best").fetchone()[0] == 4
    connection.close()


def test_concurrent_unlocks_are_newly_unlocked_exactly_once(store):
    player = store.ensurePlayer("alice")
    results = []

    def unlock():
        results.append(store.unlock(player["id"], "fishe", "first-catch")[0])

    threads = [threading.Thread(target=unlock) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False] * 11 + [True]


class _Declarations(object):
    def board(self, boardId):
        return BOARD if boardId == "most-money" else None


# --- deletion, retention, backup ------------------------------------------------------------------


def test_delete_player_cascades_to_everything(store):
    player = store.ensurePlayer("alice")
    store.setDisplayName("alice", "First", "flrst", 0, 30)
    store.setDisplayName("alice", "Second", "second", 0, 30)
    store.submitScore(player["id"], "fishe", BOARD, 5)
    store.logRefused(player["id"], "fishe", "most-money", -1, None, "outside min..max")
    store.unlock(player["id"], "fishe", "first-catch")
    store.like(player["id"], "fishe")
    assert store.deletePlayer("alice") is True
    connection = sqlite3.connect(store.path)
    for table in ("player", "name_hold", "score_best", "score_log", "unlock", "game_like"):
        assert connection.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0] == 0, table
    connection.close()
    assert store.deletePlayer("alice") is False


def test_no_ip_or_agent_columns_anywhere(store):
    connection = sqlite3.connect(store.path)
    columns = []
    for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        columns += [row[1].lower() for row in connection.execute("PRAGMA table_info(%s)" % table)]
    connection.close()
    assert not [column for column in columns if "ip" == column or "addr" in column or "agent" in column or "device" in column]


def test_log_retention(store, clock):
    player = store.setDisplayName("alice", "Alice", "allce", 30, 30)
    store.submitScore(player["id"], "fishe", BOARD, 1)
    clock.now += 91 * DAY
    store.submitScore(player["id"], "fishe", BOARD, 2)
    assert store.pruneLog(90) == 1
    assert len(store.submissionLog("fishe", "most-money", 10)) == 1
    # The best itself is not part of the log and stays.
    assert store.top("fishe", BOARD, 10)[1] == 1


def test_online_backup_is_a_consistent_copy(store, tmp_path):
    player = store.ensurePlayer("alice")
    store.like(player["id"], "fishe")
    destination = str(tmp_path / "backup.sqlite3")
    store.backup(destination)
    copy = Store(destination)
    assert copy.likeCounts() == {"fishe": 1}
    assert copy.schemaVersion() == 3
