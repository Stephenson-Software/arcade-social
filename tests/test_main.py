# @author Daniel McCoy Stephenson
"""The command line: backup, migrate, check-config, operator tools; and the
vendored registry parser staying byte-for-byte arcade's."""

import hashlib
import json
import os

import pytest

from arcade_social import __main__ as cli
from arcade_social import boards
from arcade_social.store import Store

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
# sha256 of Stephenson-Software/arcade src/arcade/registry.py at
# c34f931bca31482eb09bcee959e949922680b9e6. Re-vendor (copy the file, update
# this) whenever arcade's registry format changes, or this service will refuse
# a games.yaml that arcade accepts.
ARCADE_REGISTRY_SHA256 = "175ab28ffec871691ea095f2ee10422fc298c0f6e3f854d92c3f3782c83e1c27"


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "db.sqlite3")
    monkeypatch.setenv("ARCADE_SOCIAL_DB", path)
    monkeypatch.setenv("ARCADE_SOCIAL_SAVES_DB", str(tmp_path / "saves.sqlite3"))
    return path


def test_registry_is_vendored_unchanged():
    with open(os.path.join(ROOT, "src", "arcade_social", "registry.py"), "rb") as vendored:
        assert hashlib.sha256(vendored.read()).hexdigest() == ARCADE_REGISTRY_SHA256


def test_backup_and_migrate(database, tmp_path, capsys):
    assert cli.main(["migrate"]) == 0
    assert "schema version 3 (was 0)" in capsys.readouterr().out
    store = Store(database)
    player = store.ensurePlayer("alice")
    store.like(player["id"], "fishe")
    destination = str(tmp_path / "copy.sqlite3")
    assert cli.main(["backup", destination]) == 0
    assert Store(destination).likeCounts() == {"fishe": 1}
    # Never overwrites.
    assert cli.main(["backup", destination]) == 1


def test_check_config(tmp_path, monkeypatch, capsys):
    examples = os.path.join(ROOT, "examples")
    assert cli.main(["check-config", "--registry", os.path.join(examples, "games.yaml"),
                     "--boards", os.path.join(examples, "boards.yaml")]) == 0
    out = capsys.readouterr().out
    assert "3 game(s) OK" in out and "2 game(s) OK" in out
    bad = tmp_path / "boards.yaml"
    bad.write_text("games:\n  fishe:\n    boards:\n      - id: x\n")
    assert cli.main(["check-config", "--registry", os.path.join(examples, "games.yaml"), "--boards", str(bad)]) == 1
    orphan = tmp_path / "orphan.yaml"
    orphan.write_text("games:\n  gone:\n    achievements:\n      - id: a1\n        title: A\n        description: B\n")
    assert cli.main(["check-config", "--registry", os.path.join(examples, "games.yaml"), "--boards", str(orphan)]) == 0
    assert "not in the registry" in capsys.readouterr().out


def test_admin_commands(database, capsys):
    store = Store(database)
    board = boards.Board("most-money", "Most", "desc", 0, 100)
    player = store.setDisplayName("cheater", "Cheater", "cheater", 30, 30)
    store.submitScore(player["id"], "fishe", board, 100)
    store.like(player["id"], "fishe")
    entryId = store.top("fishe", board, 1)[0][0]["id"]
    assert cli.main(["admin", "entry", str(entryId)]) == 0
    assert json.loads(capsys.readouterr().out)["username"] == "cheater"
    assert cli.main(["admin", "exclude", "Cheater", "--reason", "forged"]) == 0
    assert store.top("fishe", board, 10)[1] == 0
    assert cli.main(["admin", "unexclude", "cheater"]) == 0
    assert cli.main(["admin", "unexclude", "cheater"]) == 1
    assert cli.main(["admin", "log", "fishe", "most-money"]) == 0
    capsys.readouterr()
    assert cli.main(["admin", "delete-likes", "cheater"]) == 0
    assert "deleted 1 like(s)" in capsys.readouterr().out
    assert cli.main(["admin", "delete-entry", str(entryId)]) == 0
    assert cli.main(["admin", "delete-entry", str(entryId)]) == 1
    assert cli.main(["admin", "entry", str(entryId)]) == 1


def test_a_newer_database_stops_the_cli(database, capsys):
    import sqlite3

    Store(database)
    connection = sqlite3.connect(database)
    connection.execute("INSERT INTO schema_version VALUES (99, 'future', 0)")
    connection.commit()
    connection.close()
    assert cli.main(["migrate"]) == 1
    assert "schema version 99" in capsys.readouterr().err
