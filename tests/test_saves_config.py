# @author Daniel McCoy Stephenson
"""saves.yaml: the strict parser, and the switches read from the environment."""

import os

import pytest

from arcade_social import __main__ as cli
from arcade_social import savesconfig
from arcade_social.config import Config, ConfigError
from arcade_social.saves_store import SavesStore

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = os.path.join(HERE, "..", "examples")

GOOD = """games:
  night-ferry:
    mode: on
    store: night-ferry-saves
    format: tak-saves
    root: /saves
    pull: true
    maxUploadBytes: 2097152
    maxStoredBytes: 26214400
"""


def test_a_good_file():
    loaded = savesconfig.loads(GOOD)
    game = loaded.get("night-ferry")
    assert (game.mode, game.store, game.format, game.root, game.pull) == ("on", "night-ferry-saves", "tak-saves", "/saves", True)
    assert (game.maxUploadBytes, game.maxStoredBytes) == (2097152, 26214400)
    assert list(loaded) == ["night-ferry"]
    assert len(savesconfig.loads("games: {}\n")) == 0


def test_the_example_is_valid():
    assert savesconfig.load(os.path.join(EXAMPLES, "saves.yaml")).get("fishe").pull is True


@pytest.mark.parametrize(
    "text, message",
    [
        ("", "empty"),
        ("game:\n", "must start"),
        (GOOD.replace("mode: on", "mode: yes"), "mode must be"),
        (GOOD.replace("format: tak-saves", "format: zip"), "format must be"),
        (GOOD.replace("root: /saves", "root: saves"), "root must be"),
        (GOOD.replace("root: /saves", "root: /saves/../etc"), "root must be"),
        (GOOD.replace("store: night-ferry-saves", "store: ../x"), "store"),
        (GOOD.replace("    pull: true\n", "    pull: maybe\n"), "pull must be"),
        (GOOD.replace("maxUploadBytes: 2097152", "maxUploadBytes: 2MB"), "maxUploadBytes"),
        (GOOD.replace("maxUploadBytes: 2097152", "maxUploadBytes: 999999999"), "maxUploadBytes"),
        (GOOD.replace("maxStoredBytes: 26214400", "maxStoredBytes: 1000"), "smaller than"),
        (GOOD.replace("    mode: on\n", ""), "missing mode"),
        (GOOD + "    colour: blue\n", "unknown key"),
        (GOOD + "    mode: on\n", "given twice"),
        (GOOD + "  night-ferry:\n", "declared twice"),
        (GOOD + GOOD.replace("games:\n", "").replace("night-ferry:", "other:"), "already used"),
        (GOOD.replace("  night-ferry:", "  Night_Ferry:"), "not a valid arcade slug"),
        (GOOD.replace("    mode: on", "\tmode: on"), "tabs"),
        (GOOD.replace("    mode: on", "      mode: on"), "indentation"),
    ],
)
def test_bad_files_name_the_problem(text, message):
    with pytest.raises(savesconfig.SavesConfigError) as error:
        savesconfig.loads(text)
    assert message in str(error.value)


def test_the_environment_switches():
    default = Config.fromEnvironment({})
    assert default.savesMode == "off"
    assert default.savesAccounts == frozenset()
    assert default.savesDatabasePath == "/data/saves.sqlite3"
    assert default.savesConfigPath == "/config/play/saves.yaml"
    on = Config.fromEnvironment(
        {"ARCADE_SOCIAL_SAVES": "ON", "ARCADE_SOCIAL_SAVES_ACCOUNTS": "Alice, bob ,"}
    )
    assert on.savesMode == "on" and on.savesAccounts == frozenset(["alice", "bob"])
    assert Config.fromEnvironment({"ARCADE_SOCIAL_SAVES_ACCOUNTS": "*"}).savesAccounts is None
    with pytest.raises(ConfigError):
        Config.fromEnvironment({"ARCADE_SOCIAL_SAVES": "maybe"})


def test_a_missing_saves_yaml_means_no_games(tmp_path):
    from helpers import Env

    env = Env(tmp_path, savesYaml=None, savesMode="on")
    try:
        client = env.player("alice")
        response = client.api("GET", "/v1/saves/fishe-saves")
        assert response.status == 200 and response.json()["writable"] is False
    finally:
        env.stop()


def test_check_config_and_backup_saves(tmp_path, monkeypatch, capsys):
    games = os.path.join(EXAMPLES, "games.yaml")
    boardsFile = os.path.join(EXAMPLES, "boards.yaml")
    assert cli.main(["check-config", "--registry", games, "--boards", boardsFile, "--saves", os.path.join(EXAMPLES, "saves.yaml")]) == 0
    assert "1 game(s) OK" in capsys.readouterr().out
    bad = tmp_path / "saves.yaml"
    bad.write_text(GOOD.replace("mode: on", "mode: sometimes"))
    assert cli.main(["check-config", "--registry", games, "--boards", boardsFile, "--saves", str(bad)]) == 1
    monkeypatch.setenv("ARCADE_SOCIAL_DB", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("ARCADE_SOCIAL_SAVES_DB", str(tmp_path / "saves.sqlite3"))
    store = SavesStore(str(tmp_path / "saves.sqlite3"))
    store.enroll(1, "fishe", "fishe-saves")
    destination = str(tmp_path / "copy.sqlite3")
    assert cli.main(["backup-saves", destination]) == 0
    assert "integrity_check ok" in capsys.readouterr().out
    assert SavesStore(destination).enrolled(1, "fishe", "fishe-saves")
    assert cli.main(["backup-saves", destination]) == 1  # never overwrites
    assert cli.main(["admin", "saves-check"]) == 0
    assert cli.main(["migrate"]) == 0
    assert "saves schema version 1" in capsys.readouterr().out
