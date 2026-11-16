# @author Daniel McCoy Stephenson
"""The SQLite store: migrations, display-name rules, uniqueness by constraint,
concurrency across threads, leaderboard ranks and windows, achievements,
likes, operator tools, deletion, retention and the online backup."""

import hashlib
import sqlite3
import threading

import pytest

from arcade_social import boards
from arcade_social.saves_store import SavesStore
from arcade_social.store import MIGRATIONS, NameHeld, NameTaken, NameTooSoon, SchemaTooNew, Store, enableWal

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


def test_the_hall_presence_opt_in_is_added_off_for_existing_players(tmp_path):
    """Version 3 to 4 adds the opt-in column, off for every existing player."""
    path = str(tmp_path / "db.sqlite3")
    old = Store(path, migrations=MIGRATIONS[:3])
    old.ensurePlayer("alice")
    old.setDisplayName("alice", "Alice", "allce", 30, 30)
    new = Store(path)
    assert new.migrated == (3, 4)
    player = new.player("alice")
    assert player["display_name"] == "Alice" and player["hall_presence"] == 0
    new.setHallPresence(player["id"], True)
    assert new.player("alice")["hall_presence"] == 1
    assert new.export("alice")["hallPresence"] is True
    assert new.presenceVisible([player["id"], 999]) == {player["id"]}
    new.exclude("alice", None)
    assert new.presenceVisible([player["id"]]) == set()
    new.unexclude("alice")
    assert new.deletePlayer("alice")
    assert new.presenceVisible([player["id"]]) == set()


def test_fresh_database_is_migrated_to_the_latest_version_in_wal_mode(tmp_path):
    path = str(tmp_path / "db.sqlite3")
    store = Store(path)
    assert store.migrated == (0, len(MIGRATIONS))
    assert store.schemaVersion() == 4
    assert {"player", "name_hold", "score_best", "score_log", "unlock", "exclusion", "game_like", "schema_version"} <= _tables(path)
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    descriptions = [row[0] for row in connection.execute("SELECT description FROM schema_version ORDER BY version")]
    assert descriptions == [description for _, description, _ in MIGRATIONS]
    connection.close()
    # Opening again applies nothing.
    assert Store(path).migrated == (4, 4)


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
    assert new.migrated == (2, 4)
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
    assert [row[0] for row in connection.execute("SELECT version FROM schema_version")] == [1, 2, 3, 4]
    connection.close()


def _holdWriteLock(path, seconds):
    """Hold a write lock on a fresh, rollback-journal file for `seconds`. While
    it is held, PRAGMA journal_mode=WAL fails at once with "database is locked"
    instead of waiting on busy_timeout: the race behind #13."""
    holder = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    holder.execute("CREATE TABLE placeholder (x INTEGER)")
    holder.execute("BEGIN IMMEDIATE")

    def release():
        holder.execute("COMMIT")
        holder.close()

    timer = threading.Timer(seconds, release)
    timer.start()
    return timer


@pytest.mark.parametrize("storeClass", [Store, SavesStore])
def test_first_start_waits_out_a_lock_on_the_wal_switch(tmp_path, storeClass):
    path = str(tmp_path / "db.sqlite3")
    timer = _holdWriteLock(path, 0.3)
    try:
        storeClass(path)
    finally:
        timer.join()
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert [row[0] for row in connection.execute("SELECT version FROM schema_version")][0] == 1
    connection.close()


def test_the_wal_switch_gives_up_after_its_timeout(tmp_path):
    path = str(tmp_path / "db.sqlite3")
    timer = _holdWriteLock(path, 1.0)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            enableWal(connection, path, timeout=0.1)
    finally:
        connection.close()
        timer.join()


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


def test_export_lists_the_names_held_for_the_player(store, clock):
    store.setDisplayName("alice", "First", "flrst", 0, 30)
    released = clock.now
    store.setDisplayName("alice", "Second", "second", 0, 30)
    clock.now += 1
    store.setDisplayName("alice", "Third", "thlrd", 0, 30)
    store.setDisplayName("bob", "Bobby", "bobby", 0, 30)
    store.setDisplayName("bob", "Robert", "robert", 0, 30)
    assert store.export("alice")["nameHolds"] == [
        {"display_key": "flrst", "released_at": released},
        {"display_key": "second", "released_at": released + 1},
    ]
    assert store.export("bob")["nameHolds"] == [{"display_key": "bobby", "released_at": released + 1}]
    # a hold past its 30 days is still held data until the name is taken again
    clock.now += 31 * DAY
    assert len(store.export("alice")["nameHolds"]) == 2


def test_all_versions_walks_every_page_newest_first(tmp_path):
    saves = SavesStore(str(tmp_path / "saves.sqlite3"))
    meta = {"device": "d1", "deviceLabel": "Laptop", "origin": "https://fishe.play.example", "kind": "auto"}
    meta["format"] = "tak-saves"
    made = []
    parent = None
    for index in range(14):
        data = b"save %d" % index
        units = {"/saves/a.json": (hashlib.sha256(data).hexdigest(), data)}
        parent = saves.commit(1, "fishe", "fishe-saves", parent, units, meta, lambda *args: None)[0]
        made.append(parent)
    saves.commit(2, "fishe", "fishe-saves", None, units, meta, lambda *args: None)
    newestFirst = list(reversed(made))
    # 14 is an exact multiple of 7, so the walk ends on an empty page
    for pageSize in (1, 5, 7, 14, 200):
        listed = list(saves.allVersions(1, "fishe", "fishe-saves", pageSize=pageSize))
        assert [v["id"] for v in listed] == newestFirst, pageSize
    assert list(saves.allVersions(1, "fishe", "other-store")) == []


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


# --- leaderboards: ranks, windows, champions, bests -------------------------------------------

FASTEST = boards.Board("fastest", "Fastest", "asc", 0, 1000, maxPerHour=1000)


def _named(store, username):
    """A player with a display name (only named players are listed)."""
    return store.setDisplayName(username, username.capitalize(), username, 0, 30)


def _scores(store, clock, board, values, slug="fishe"):
    """One named player per (username, value), each a millisecond after the last."""
    players = {}
    for username, value in values:
        players[username] = _named(store, username)
        store.submitScore(players[username]["id"], slug, board, value)
        clock.now += 1
    return players


def _ranked(entries):
    return [(entry["displayName"], entry["value"], entry["rank"]) for entry in entries]


def test_top_ranks_ties_together_in_the_order_they_were_reached(store, clock):
    _scores(store, clock, BOARD, [("ann", 50), ("bea", 70), ("cal", 70), ("dee", 30)])
    entries, total = store.top("fishe", BOARD, 10)
    assert _ranked(entries) == [("Bea", 70, 1), ("Cal", 70, 1), ("Ann", 50, 3), ("Dee", 30, 4)]
    assert total == 4
    assert _ranked(store.top("fishe", BOARD, 2)[0]) == [("Bea", 70, 1), ("Cal", 70, 1)]


def test_unnamed_and_excluded_players_are_neither_listed_nor_ranked_against(store, clock):
    _scores(store, clock, BOARD, [("ann", 50)])
    unnamed = store.ensurePlayer("ghost")
    store.submitScore(unnamed["id"], "fishe", BOARD, 999)
    cheat = _named(store, "cheat")
    assert store.submitScore(cheat["id"], "fishe", BOARD, 998)["rank"] == 1
    store.exclude("cheat", "forged")
    entries, total = store.top("fishe", BOARD, 10)
    assert _ranked(entries) == [("Ann", 50, 1)] and total == 1
    # An excluded player has no rank at all, even for a new best.
    assert store.submitScore(cheat["id"], "fishe", BOARD, 999)["rank"] is None


def test_an_ascending_board_ranks_the_lowest_value_first(store, clock):
    _scores(store, clock, FASTEST, [("ann", 30), ("bea", 10), ("cal", 20), ("dee", 10)])
    assert _ranked(store.top("fishe", FASTEST, 10)[0]) == [
        ("Bea", 10, 1),
        ("Dee", 10, 1),
        ("Cal", 20, 3),
        ("Ann", 30, 4),
    ]


def test_around_player_counts_a_tie_above_the_first_row_of_its_window(store, clock):
    players = _scores(
        store, clock, BOARD, [("ann", 100), ("bea", 90), ("cal", 80), ("dee", 80), ("eve", 70), ("fay", 60)]
    )
    # eve is fifth, so a window of 1 starts at dee, whose tie with cal (above
    # the window) keeps rank 3.
    entries, me = store.aroundPlayer(players["eve"]["id"], "fishe", BOARD, 1)
    assert _ranked(entries) == [("Dee", 80, 3), ("Eve", 70, 5), ("Fay", 60, 6)]
    assert me == entries[1]["id"]
    # Near the top the window is clamped to the first row.
    entries, me = store.aroundPlayer(players["bea"]["id"], "fishe", BOARD, 2)
    assert _ranked(entries) == [
        ("Ann", 100, 1),
        ("Bea", 90, 2),
        ("Cal", 80, 3),
        ("Dee", 80, 3),
        ("Eve", 70, 5),
    ]
    assert me == entries[1]["id"]


def test_around_player_is_empty_without_an_entry_or_when_excluded(store, clock):
    players = _scores(store, clock, BOARD, [("ann", 100), ("bea", 90)])
    nobody = store.ensurePlayer("nobody")
    assert store.aroundPlayer(nobody["id"], "fishe", BOARD, 3) == ([], None)
    assert store.aroundPlayer(players["bea"]["id"], "other-game", BOARD, 3) == ([], None)
    store.exclude("bea", None)
    assert store.aroundPlayer(players["bea"]["id"], "fishe", BOARD, 3) == ([], None)


def test_champions_holds_both_ends_of_every_board(store, clock):
    _scores(store, clock, BOARD, [("ann", 50), ("bea", 70), ("cal", 70), ("dee", 30), ("eve", 30)])
    _scores(store, clock, FASTEST, [("fay", 12)], slug="tak")
    hidden = store.ensurePlayer("ghost")
    store.submitScore(hidden["id"], "fishe", BOARD, 999)
    store.submitScore(_named(store, "cheat")["id"], "fishe", BOARD, 0)
    store.exclude("cheat", None)

    def end(name, value):
        return {"displayName": name, "value": value, "achievedAt": store.player(name.lower())["created_at"]}

    found = store.champions()
    assert set(found) == {("fishe", "most-money"), ("tak", "fastest")}
    # Ties go to whoever reached the value first, at either end.
    assert found[("fishe", "most-money")]["desc"] == end("Bea", 70)
    assert found[("fishe", "most-money")]["asc"] == end("Dee", 30)
    # A board with one entry has it at both ends, as separate dicts.
    tak = found[("tak", "fastest")]
    assert tak["desc"] == tak["asc"] == end("Fay", 12)
    assert tak["desc"] is not tak["asc"]


def test_player_bests_list_declared_boards_with_their_rank(store, clock):
    players = _scores(store, clock, BOARD, [("ann", 50), ("bea", 70)])
    store.submitScore(players["ann"]["id"], "fishe", FASTEST, 9)  # no longer declared
    store.submitScore(players["ann"]["id"], "tak", BOARD, 5)  # another game
    assert store.playerBests(players["ann"]["id"], "fishe", _Declarations()) == [
        {"board": "most-money", "value": 50, "achievedAt": players["ann"]["created_at"], "rank": 2}
    ]
    # A game with no boards.yaml entry has no declared boards.
    assert store.playerBests(players["ann"]["id"], "fishe", None) == []
    store.exclude("ann", None)
    assert store.playerBests(players["ann"]["id"], "fishe", _Declarations())[0]["rank"] is None


def test_max_per_hour_refuses_without_recording_until_the_hour_passes(store, clock):
    board = boards.Board("capped", "Capped", "desc", 0, 1000, maxPerHour=2)
    player = _named(store, "ann")
    assert store.submitScore(player["id"], "fishe", board, 1)["improved"] is True
    assert store.submitScore(player["id"], "fishe", board, 1)["improved"] is False
    assert store.submissionsInLastHour(player["id"], "fishe", "capped") == 2
    assert store.submitScore(player["id"], "fishe", board, 500) is None
    assert store.submissionsInLastHour(player["id"], "fishe", "capped") == 2
    assert store.top("fishe", board, 1)[0][0]["value"] == 1
    clock.now += 3600001
    assert store.submissionsInLastHour(player["id"], "fishe", "capped") == 0
    assert store.submitScore(player["id"], "fishe", board, 500) == {"best": 500, "improved": True, "rank": 1}


# --- achievements, likes, operator tools ---------------------------------------------------------


def test_achievement_shares_count_players_with_any_score_or_unlock(store, clock):
    players = _scores(store, clock, BOARD, [("ann", 1), ("cal", 3)])
    bea = _named(store, "bea")
    store.unlock(bea["id"], "fishe", "first-catch")
    store.unlock(players["cal"]["id"], "fishe", "first-catch")
    store.unlock(players["cal"]["id"], "fishe", "big-catch")
    store.unlock(players["cal"]["id"], "tak", "first-catch")  # another game
    cheat = _named(store, "cheat")
    store.unlock(cheat["id"], "fishe", "first-catch")
    store.exclude("cheat", None)
    # ann (score only), bea (unlock only) and cal (both) count once each.
    assert store.achievementShares("fishe") == (3, {"first-catch": 2, "big-catch": 1})
    assert store.achievementShares("nothing") == (0, {})


def test_player_unlocks_are_listed_in_the_order_they_happened(store, clock):
    player = _named(store, "ann")
    store.unlock(player["id"], "fishe", "zebra")
    first = clock.now
    clock.now += 5
    store.unlock(player["id"], "fishe", "alpha")
    # Unlocking again keeps the first time.
    assert store.unlock(player["id"], "fishe", "zebra") == (False, first)
    assert store.playerUnlocks(player["id"], "fishe") == [
        {"achievement": "zebra", "unlockedAt": first},
        {"achievement": "alpha", "unlockedAt": first + 5},
    ]
    assert store.playerUnlocks(player["id"], "tak") == []


def test_delete_game_data_removes_one_game_and_keeps_likes(store, clock):
    player = _named(store, "ann")
    other = _named(store, "bea")
    for slug in ("fishe", "tak"):
        store.submitScore(player["id"], slug, BOARD, 5)
        store.unlock(player["id"], slug, "first-catch")
    store.logRefused(player["id"], "fishe", "most-money", -1, None, "outside min..max")
    store.submitScore(other["id"], "fishe", BOARD, 7)
    store.like(player["id"], "fishe")
    # score_best 1 + score_log 2 (one accepted, one refused) + unlock 1
    assert store.deleteGameData(player["id"], "fishe") == 4
    assert store.deleteGameData(player["id"], "fishe") == 0
    assert store.playerBests(player["id"], "fishe", _Declarations()) == []
    assert store.playerUnlocks(player["id"], "fishe") == []
    assert len(store.playerBests(player["id"], "tak", _Declarations())) == 1
    assert store.top("fishe", BOARD, 10)[1] == 1
    assert store.likedBy(player["id"]) == ["fishe"]


def test_unlike_returns_the_remaining_count(store):
    ann = store.ensurePlayer("ann")
    bea = store.ensurePlayer("bea")
    store.like(ann["id"], "fishe")
    assert store.like(bea["id"], "fishe") == 2
    assert store.unlike(ann["id"], "fishe") == 1
    assert store.unlike(ann["id"], "fishe") == 1
    assert store.likedBy(ann["id"]) == []
    assert store.unlike(bea["id"], "fishe") == 0
    assert store.likeCounts() == {}


def test_exclusions_are_listed_oldest_first_and_replaced_on_repeat(store, clock):
    assert store.exclusions() == [] and store.isExcluded("ann") is False
    store.exclude("ann", "forged")
    clock.now += 1
    store.exclude("bea", None)
    assert store.isExcluded("ann") is True

    def listed():
        return [(row["username"], row["reason"]) for row in store.exclusions()]

    assert listed() == [("ann", "forged"), ("bea", None)]
    clock.now += 1
    store.exclude("ann", "forged again")
    assert listed() == [("bea", None), ("ann", "forged again")]


def test_summary_counts_what_is_held_and_unknown_accounts_hold_nothing(store):
    player = _named(store, "ann")
    store.submitScore(player["id"], "fishe", BOARD, 5)
    store.submitScore(player["id"], "tak", BOARD, 5)
    store.unlock(player["id"], "fishe", "first-catch")
    store.like(player["id"], "fishe")
    assert store.summary("ann") == {"scores": 2, "unlocks": 1, "likes": 1}
    assert store.summary("nobody") == {"scores": 0, "unlocks": 0, "likes": 0}
    assert store.player("nobody") is None
    assert store.export("nobody") is None


def test_presence_visible_needs_a_name_the_opt_in_and_no_exclusion(store):
    shown = _named(store, "ann")
    unnamed = store.ensurePlayer("bea")
    optedOut = _named(store, "cal")
    excluded = _named(store, "dee")
    for player in (shown, unnamed, excluded):
        store.setHallPresence(player["id"], True)
    store.exclude("dee", None)
    ids = [shown["id"], unnamed["id"], optedOut["id"], excluded["id"], 999]
    assert store.presenceVisible(ids) == {shown["id"]}
    assert store.presenceVisible([]) == set()


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
    assert copy.schemaVersion() == 4
