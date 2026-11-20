# @author Daniel McCoy Stephenson
"""The cloud-saves rules in saves.py, called directly: canonical encoding, path and
content checks, save-file parsing, upload-body validation, the rule-3 check against
head, the Policy switches, and how a stored version becomes a save file again."""
import json

import pytest

from arcade_social import saves
from arcade_social.savesconfig import GameSaves, SavesConfig

ROOT = "/saves"
STORE = "fishe-saves"


def _game(mode="on"):
    return GameSaves("fishe", mode, STORE, "tak-saves", ROOT, 1000000, 100000000, True)


def _document(files=None, **overrides):
    document = {
        "format": "tak-saves",
        "version": 1,
        "game": STORE,
        "exported": "2026-11-19T00:00:00Z",
        "files": {ROOT + "/slot_1/meta.json": "{}"} if files is None else files,
    }
    document.update(overrides)
    return document


def _body(**overrides):
    body = {"parent": None, "device": "device-0001", "file": _document()}
    body.update(overrides)
    return body


def _refusal(function, *arguments):
    with pytest.raises(saves.SavesError) as caught:
        function(*arguments)
    return caught.value


class Holder(object):
    def __init__(self, value):
        self.value = value


# --- canonical ------------------------------------------------------------------------


def test_canonical_is_compact_sorted_utf8():
    assert saves.canonical({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode("utf-8")


def test_canonical_refuses_text_that_is_not_unicode():
    error = _refusal(saves.canonical, {"a": "\ud800"})
    assert (error.status, error.code) == (400, "invalid")


def test_encode_units_hashes_each_units_canonical_bytes():
    encoded = saves.encodeUnits({"slot_1": {"/saves/slot_1/a": "x"}})
    data = saves.canonical({"/saves/slot_1/a": "x"})
    assert encoded == {"slot_1": (saves.sha256(data), data)}


# --- checkPath ------------------------------------------------------------------------


def test_check_path_accepts_a_path_under_the_root():
    assert saves.checkPath(ROOT + "/slot_1/meta.json", ROOT) is None


@pytest.mark.parametrize(
    "path,problem",
    [
        ("", "an empty path"),
        (None, "an empty path"),
        (7, "an empty path"),
        (ROOT + "/" + "a" * saves.MAX_PATH_LENGTH, "a path that is too long"),
        ("/other/slot_1", "a path outside /saves/"),
        (ROOT, "a path outside /saves/"),
        (ROOT + "x/slot_1", "a path outside /saves/"),
        (ROOT + "/slot_1\\a", "a path with a control character or backslash"),
        (ROOT + "/slot_1/\x01", "a path with a control character or backslash"),
        (ROOT + "/", "a path with an empty, . or .. part"),
        (ROOT + "//a", "a path with an empty, . or .. part"),
        (ROOT + "/slot_1/", "a path with an empty, . or .. part"),
        (ROOT + "/./a", "a path with an empty, . or .. part"),
        (ROOT + "/slot_1/../a", "a path with an empty, . or .. part"),
    ],
)
def test_check_path_names_the_problem(path, problem):
    assert saves.checkPath(path, ROOT) == problem


def test_check_path_allows_the_longest_path():
    path = ROOT + "/" + "a" * (saves.MAX_PATH_LENGTH - len(ROOT) - 1)
    assert len(path) == saves.MAX_PATH_LENGTH
    assert saves.checkPath(path, ROOT) is None


# --- parseSaveFile --------------------------------------------------------------------


def test_parse_save_file_splits_files_into_units_by_first_segment():
    files = {
        ROOT + "/slot_1/meta.json": "{}",
        ROOT + "/slot_1/world/chunk": {"base64": "AAEC"},
        ROOT + "/slot_2/meta.json": "{}",
        ROOT + "/settings.json": "{}",
    }
    assert saves.parseSaveFile(_document(files), _game()) == {
        "slot_1": {ROOT + "/slot_1/meta.json": "{}", ROOT + "/slot_1/world/chunk": {"base64": "AAEC"}},
        "slot_2": {ROOT + "/slot_2/meta.json": "{}"},
        "settings.json": {ROOT + "/settings.json": "{}"},
    }


def test_parse_save_file_accepts_no_files_and_no_exported():
    document = _document({})
    del document["exported"]
    assert saves.parseSaveFile(document, _game()) == {}


@pytest.mark.parametrize(
    "document,message",
    [
        ([], "this is not a tak-saves file"),
        (_document(format="other"), "this is not a tak-saves file"),
        (_document(version=True), "the save file has no readable version"),
        (_document(version="1"), "the save file has no readable version"),
        (_document(version=None), "the save file has no readable version"),
        (_document(version=2), "the save file's version (2) is not one this service reads"),
        (_document(game="other"), "the save file is for another store than fishe-saves"),
        (_document(exported=5), "exported must be a string"),
        (_document(files=["x"]), "the save file has no list of files"),
        (
            _document(files={ROOT + "/../x": "{}"}),
            "the save file contains a path with an empty, . or .. part",
        ),
        (
            _document(files={ROOT + "/slot_1/a": 5}),
            "the save file is damaged: /saves/slot_1/a cannot be read",
        ),
    ],
)
def test_parse_save_file_refuses_an_invalid_document(document, message):
    error = _refusal(saves.parseSaveFile, document, _game())
    assert (error.status, error.code) == (400, "invalid")
    assert error.message.startswith(message)


def test_parse_save_file_refuses_too_many_files_before_reading_them():
    files = dict(("bad path %d" % index, None) for index in range(saves.MAX_FILES + 1))
    error = _refusal(saves.parseSaveFile, _document(files), _game())
    assert error.message == "the save file holds too many files"


@pytest.mark.parametrize("text", ["", "AAEC", "AA==", "AAE="])
def test_parse_save_file_accepts_base64_content(text):
    files = {ROOT + "/slot_1/a": {"base64": text}}
    assert saves.parseSaveFile(_document(files), _game()) == {"slot_1": files}


@pytest.mark.parametrize(
    "value",
    [
        None,
        5,
        ["AAEC"],
        {"base64": 5},
        {"base64": "AAEC", "extra": "x"},
        {"text": "AAEC"},
        {"base64": "AAE"},  # not a multiple of four
        {"base64": "A=AE"},  # padding in the middle
        {"base64": "AAE\n"},  # passes the pattern ($ before a final newline), fails decoding
    ],
)
def test_parse_save_file_refuses_unreadable_content(value):
    error = _refusal(saves.parseSaveFile, _document({ROOT + "/slot_1/a": value}), _game())
    assert error.message == "the save file is damaged: /saves/slot_1/a cannot be read"


# --- validateUpload -------------------------------------------------------------------


def test_validate_upload_returns_defaults():
    parent, meta, units, confirmRemoved, confirmShrink = saves.validateUpload(_body(), _game())
    assert parent is None
    assert meta == {"device": "device-0001", "deviceLabel": "A browser", "kind": "upload"}
    assert units == {"slot_1": {ROOT + "/slot_1/meta.json": "{}"}}
    assert (confirmRemoved, confirmShrink) == ([], False)


def test_validate_upload_returns_every_field_given():
    body = _body(
        parent=3,
        device="d" * 64,
        deviceLabel="  Laptop  ",
        kind="restore",
        confirmRemoved=["slot_2"],
        confirmShrink=True,
    )
    parent, meta, _, confirmRemoved, confirmShrink = saves.validateUpload(body, _game())
    assert parent == 3
    assert meta == {"device": "d" * 64, "deviceLabel": "Laptop", "kind": "restore"}
    assert (confirmRemoved, confirmShrink) == (["slot_2"], True)


@pytest.mark.parametrize("kind", saves.KINDS)
def test_validate_upload_accepts_every_kind(kind):
    assert saves.validateUpload(_body(kind=kind), _game())[1]["kind"] == kind


def test_validate_upload_accepts_a_sixty_character_label():
    assert saves.validateUpload(_body(deviceLabel="L" * 60), _game())[1]["deviceLabel"] == "L" * 60


@pytest.mark.parametrize(
    "body,message",
    [
        ([], "the body must be a JSON object"),
        (_body(zeta=1, alpha=2), "unknown field(s): alpha, zeta"),
        (_body(parent=True), "parent must be a version id or null"),
        (_body(parent=0), "parent must be a version id or null"),
        (_body(parent="3"), "parent must be a version id or null"),
        (_body(parent=1.0), "parent must be a version id or null"),
        (_body(device=None), "device must be 8-64 letters, digits and dashes"),
        (_body(device="short-7"), "device must be 8-64 letters, digits and dashes"),
        (_body(device="d" * 65), "device must be 8-64 letters, digits and dashes"),
        (_body(device="device_0001"), "device must be 8-64 letters, digits and dashes"),
        (_body(deviceLabel=5), "deviceLabel must be at most 60 printable characters"),
        (_body(deviceLabel="L" * 61), "deviceLabel must be at most 60 printable characters"),
        (_body(deviceLabel="a\nb"), "deviceLabel must be at most 60 printable characters"),
        (_body(kind="sync"), "kind must be one of upload, merge, restore, enroll"),
        (_body(confirmRemoved="slot_1"), "confirmRemoved must be a list of unit names"),
        (_body(confirmRemoved=[1]), "confirmRemoved must be a list of unit names"),
        (_body(confirmRemoved=["a", "a"]), "confirmRemoved names a unit twice"),
        (_body(confirmShrink=1), "confirmShrink must be true or false"),
        (_body(confirmShrink="yes"), "confirmShrink must be true or false"),
        (dict((key, value) for key, value in _body().items() if key != "file"), "file is required"),
        (_body(file=_document(game="other")), "the save file is for another store than fishe-saves"),
    ],
)
def test_validate_upload_refuses_an_invalid_body(body, message):
    error = _refusal(saves.validateUpload, body, _game())
    assert (error.status, error.code, error.message) == (400, "invalid", message)


# --- checkAgainstHead -----------------------------------------------------------------


def _encoded(**sizes):
    return dict((name, ("hash-" + name, b"x" * size)) for name, size in sizes.items())


def _head(totalSize, *names):
    return {"units": [{"name": name} for name in names], "totalSize": totalSize}


def test_check_against_head_allows_anything_without_a_head():
    saves.checkAgainstHead(None, _encoded(slot_1=1), [], False)
    saves.checkAgainstHead(None, {}, [], False)


def test_check_against_head_refuses_confirming_a_removal_without_a_head():
    error = _refusal(saves.checkAgainstHead, None, _encoded(slot_1=1), ["slot_2"], False)
    assert (error.status, error.code, error.extra) == (422, "confirm-mismatch", {"removed": []})


def test_check_against_head_refuses_an_unconfirmed_removal():
    head = _head(10, "slot_1", "slot_3", "slot_2")
    error = _refusal(saves.checkAgainstHead, head, _encoded(slot_1=10), [], False)
    assert (error.status, error.code) == (422, "removed")
    assert error.extra == {"removed": ["slot_2", "slot_3"]}


@pytest.mark.parametrize("confirmed", [["slot_3"], ["slot_2", "slot_3", "slot_4"], ["slot_2", "slot_2"]])
def test_check_against_head_refuses_a_confirmation_that_does_not_match(confirmed):
    head = _head(10, "slot_1", "slot_2", "slot_3")
    error = _refusal(saves.checkAgainstHead, head, _encoded(slot_1=10, slot_3=1), confirmed, False)
    assert (error.status, error.code, error.extra) == (422, "confirm-mismatch", {"removed": ["slot_2"]})


def test_check_against_head_allows_a_confirmed_removal_in_any_order_even_if_it_shrinks():
    head = _head(1000, "slot_1", "slot_2", "slot_3")
    saves.checkAgainstHead(head, _encoded(slot_1=1), ["slot_3", "slot_2"], False)


def test_check_against_head_refuses_an_unconfirmed_shrink_below_half():
    error = _refusal(saves.checkAgainstHead, _head(100, "slot_1"), _encoded(slot_1=30, slot_2=19), [], False)
    assert (error.status, error.code) == (422, "shrink")
    assert error.extra == {"before": 100, "after": 49}


def test_check_against_head_allows_exactly_half_a_confirmed_shrink_and_an_empty_head():
    saves.checkAgainstHead(_head(100, "slot_1"), _encoded(slot_1=50), [], False)
    saves.checkAgainstHead(_head(100, "slot_1"), _encoded(slot_1=1), [], True)
    saves.checkAgainstHead(_head(0), {}, [], False)


# --- Policy ---------------------------------------------------------------------------


def test_policy_refuses_an_unknown_mode():
    with pytest.raises(ValueError, match="must be off, readonly or on, got 'maybe'"):
        saves.Policy("maybe", None, 1, 1)


def test_policy_allows_everyone_when_accounts_is_none():
    policy = saves.Policy("on", None, 1, 1)
    assert policy.allowed("anyone") and policy.allowed(None)


def test_policy_allows_listed_accounts_case_insensitively():
    policy = saves.Policy("on", {"alice"}, 1, 1)
    assert policy.allowed("Alice")
    assert not policy.allowed("bob")
    assert not policy.allowed(None)
    assert not saves.Policy("on", set(), 1, 1).allowed("alice")


@pytest.mark.parametrize(
    "mode,game,expected",
    [
        ("off", _game(), (503, "off", "cloud saves are off")),
        ("readonly", _game(), (503, "paused", "cloud saves are paused; nothing new is being backed up")),
        ("on", None, (503, "paused", "cloud saves for this game are read-only")),
        ("on", _game("readonly"), (503, "paused", "cloud saves for this game are read-only")),
    ],
)
def test_policy_writable_names_why_writes_are_refused(mode, game, expected):
    refusal = saves.Policy(mode, None, 1, 1).writable(game)
    assert (refusal.status, refusal.code, refusal.message) == expected


def test_policy_writable_is_none_when_service_and_game_are_on():
    assert saves.Policy("on", None, 1, 1).writable(_game()) is None


# --- saveFile -------------------------------------------------------------------------


def test_save_file_rebuilds_the_envelope_from_stored_units():
    contents = {
        "slot_2": saves.canonical({ROOT + "/slot_2/a": "two"}),
        "slot_1": saves.canonical({ROOT + "/slot_1/a": {"base64": "AAEC"}}),
    }
    description = {"format": "tak-saves", "createdAtIso": "2026-11-19T01:02:03Z", "id": 9}
    assert saves.saveFile(_game(), description, contents) == {
        "format": "tak-saves",
        "version": 1,
        "game": STORE,
        "exported": "2026-11-19T01:02:03Z",
        "files": {ROOT + "/slot_1/a": {"base64": "AAEC"}, ROOT + "/slot_2/a": "two"},
        "cloudVersion": 9,
    }


def test_save_file_round_trips_through_parse_save_file():
    units = saves.parseSaveFile(_document({ROOT + "/slot_1/a": "x", ROOT + "/b.json": "y"}), _game())
    contents = dict((name, data) for name, (_, data) in saves.encodeUnits(units).items())
    description = {"format": "tak-saves", "createdAtIso": "t", "id": 1}
    rebuilt = json.loads(json.dumps(saves.saveFile(_game(), description, contents)))
    assert saves.parseSaveFile(rebuilt, _game()) == units


# --- Saves: game lookup, kill switch, database size -------------------------------------


def _saves(databaseSize=None, store=None, mode="on"):
    policy = saves.Policy(mode, None, 1, 1)
    return saves.Saves(store, policy, Holder(SavesConfig({"fishe": _game()})), databaseSize)


def test_game_returns_the_listed_game_for_its_store():
    assert _saves().game("fishe", STORE).store == STORE


def test_game_refuses_another_store_for_a_listed_game():
    error = _refusal(_saves().game, "fishe", "other-store")
    assert (error.status, error.code) == (404, "unknown-store")


def test_game_describes_an_unlisted_game_as_read_only_by_its_store():
    game = _saves().game("tidewater", "tidewater-saves")
    assert (game.slug, game.mode, game.store, game.root, game.pull) == (
        "tidewater",
        "readonly",
        "tidewater-saves",
        None,
        False,
    )
    assert _saves().policy.writable(game).code == "paused"


def test_game_refuses_an_unlisted_game_with_an_invalid_store_name():
    error = _refusal(_saves().game, "tidewater", "../etc")
    assert (error.status, error.code, error.message) == (404, "unknown-store", "no such store")


def test_require_on_refuses_only_when_off():
    error = _refusal(_saves(mode="off").requireOn)
    assert (error.status, error.code) == (503, "off")
    _saves(mode="readonly").requireOn()


def test_database_size_uses_the_given_function():
    assert _saves(databaseSize=lambda: 42).databaseSize() == 42


def test_database_size_adds_the_file_and_its_wal(tmp_path):
    path = tmp_path / "saves.sqlite3"

    class Store(object):
        pass

    store = Store()
    store.path = str(path)
    assert _saves(store=store).databaseSize() == 0
    path.write_bytes(b"x" * 100)
    assert _saves(store=store).databaseSize() == 100
    (tmp_path / "saves.sqlite3-wal").write_bytes(b"x" * 25)
    (tmp_path / "saves.sqlite3-shm").write_bytes(b"x" * 7)
    assert _saves(store=store).databaseSize() == 125
