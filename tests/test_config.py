# @author Daniel McCoy Stephenson
import os
import time

import pytest

from arcade_social.config import Config, ConfigError, FileHolder, OptionalFileHolder
from arcade_social import boards


def test_environment_defaults_and_overrides():
    config = Config.fromEnvironment({})
    assert config.serviceOrigin == "https://api.play.danielstephenson.dev"
    assert config.portalOrigin == "https://danielstephenson.dev"
    assert config.registryPath == "/config/arcade/games.yaml" and config.boardsPath == "/config/play/boards.yaml"
    assert config.databasePath == "/data/arcade-social.sqlite3"
    assert config.userauthUrl == "http://userauth:9998"
    assert config.operators == frozenset() and config.trustForwardedFor is False
    assert config.hallOrigins == ("https://play.danielstephenson.dev", "https://arcade.danielstephenson.dev")
    config = Config.fromEnvironment({
        "ARCADE_SOCIAL_OPERATORS": "Boss, other ,",
        "ARCADE_SOCIAL_TRUST_FORWARDED_FOR": "true",
        "ARCADE_SOCIAL_VALIDATE_CACHE_SECONDS": "600",
        "ARCADE_SOCIAL_PUBLIC_URL": "https://API.example.test/",
    })
    assert config.operators == frozenset(("boss", "other"))
    assert config.trustForwardedFor is True
    assert config.validateCacheSeconds == 60  # never longer than RFC 0013's minute
    assert config.serviceOrigin == "https://api.example.test"


def test_hall_presence_kill_switch_defaults_on():
    assert Config.fromEnvironment({}).hallPresence == "on"
    assert Config.fromEnvironment({"ARCADE_SOCIAL_HALL_PRESENCE": " OFF "}).hallPresence == "off"
    with pytest.raises(ConfigError):
        Config.fromEnvironment({"ARCADE_SOCIAL_HALL_PRESENCE": "maybe"})


def test_hall_origins_are_configurable_and_normalised():
    config = Config.fromEnvironment({"ARCADE_SOCIAL_HALL_ORIGINS": " https://Hall.example.test/ ,https://hall.example.test"})
    assert config.hallOrigins == ("https://hall.example.test",)
    assert Config.fromEnvironment({"ARCADE_SOCIAL_HALL_ORIGINS": ""}).hallOrigins == ()


@pytest.mark.parametrize("environ", [
    {"ARCADE_SOCIAL_PUBLIC_URL": "api.example.test"},
    {"ARCADE_SOCIAL_PUBLIC_URL": "https://api.example.test/path"},
    {"ARCADE_SOCIAL_PORTAL_ORIGIN": "http://example.test"},
    {"ARCADE_SOCIAL_REFRESH_DAYS": "thirty"},
    {"ARCADE_SOCIAL_HALL_ORIGINS": "http://play.danielstephenson.dev"},
    {"ARCADE_SOCIAL_HALL_ORIGINS": "https://play.danielstephenson.dev/hall/"},
    {"ARCADE_SOCIAL_HALL_ORIGINS": "https://play.danielstephenson.dev:8443"},
    {"ARCADE_SOCIAL_HALL_ORIGINS": "https://play.danielstephenson.dev:x"},
    {"ARCADE_SOCIAL_HALL_ORIGINS": "https://user@play.danielstephenson.dev"},
    {"ARCADE_SOCIAL_HALL_ORIGINS": "https://:secret@play.danielstephenson.dev"},
    {"ARCADE_SOCIAL_HALL_ORIGINS": "https://play.danielstephenson.dev?x=1"},
    {"ARCADE_SOCIAL_HALL_ORIGINS": "https://play.danielstephenson.dev#hall"},
    {"ARCADE_SOCIAL_USERAUTH_TIMEOUT": "soon"},
    {"ARCADE_SOCIAL_SAVES_PLAYER_MAX_BYTES": "1.5"},
    # a game's slug host, the portal or the service cannot also be the hall
    {"ARCADE_SOCIAL_HALL_ORIGINS": "https://fishe.play.danielstephenson.dev"},
    {"ARCADE_SOCIAL_HALL_ORIGINS": "https://danielstephenson.dev"},
    {"ARCADE_SOCIAL_HALL_ORIGINS": "https://api.play.danielstephenson.dev"},
])
def test_bad_values_are_refused(environ):
    with pytest.raises(ConfigError):
        Config.fromEnvironment(environ)


def test_a_broken_file_keeps_the_last_good_one(tmp_path):
    path = tmp_path / "boards.yaml"
    path.write_text("games: {}\n")
    holder = FileHolder(str(path), boards.load, boards.Declarations({}), len, (boards.BoardsError,))
    assert len(holder.value) == 0
    path.write_text("games:\n  fishe:\n    achievements:\n      - id: a1\n        title: A\n        description: B\n")
    later = time.time() + 5
    os.utime(str(path), (later, later))
    assert len(holder.value) == 1
    path.write_text("not yaml at all")
    os.utime(str(path), (later + 5, later + 5))
    assert len(holder.value) == 1
    with pytest.raises(boards.BoardsError):
        FileHolder(str(path), boards.load, boards.Declarations({}), len, (boards.BoardsError,))


def test_remaining_environment_variables_map_to_the_config():
    defaults = Config.fromEnvironment({})
    assert defaults.gameDomain == "play.danielstephenson.dev"
    assert defaults.forwardClientIp is False
    assert (defaults.refreshDays, defaults.nameChangeDays, defaults.nameHoldDays) == (30, 30, 30)
    assert defaults.logRetentionDays == 90 and defaults.validateCacheSeconds == 60
    assert defaults.userauthTimeout == 5.0
    assert defaults.defaultReturn == "https://danielstephenson.dev/play"
    assert defaults.savesPlayerMaxBytes == 300 * 1024 * 1024
    assert defaults.savesDatabaseMaxBytes == 10 * 1024 * 1024 * 1024
    config = Config.fromEnvironment({
        "ARCADE_SOCIAL_GAME_DOMAIN": ".Games.Example.Test.",
        "ARCADE_SOCIAL_USERAUTH_URL": "http://auth.internal:9000/",
        "ARCADE_SOCIAL_FORWARD_CLIENT_IP": " Yes ",
        "ARCADE_SOCIAL_REFRESH_DAYS": "7",
        "ARCADE_SOCIAL_NAME_CHANGE_DAYS": "14",
        "ARCADE_SOCIAL_NAME_HOLD_DAYS": "21",
        "ARCADE_SOCIAL_LOG_RETENTION_DAYS": "45",
        "ARCADE_SOCIAL_VALIDATE_CACHE_SECONDS": "15",
        "ARCADE_SOCIAL_USERAUTH_TIMEOUT": "2.5",
        "ARCADE_SOCIAL_DEFAULT_RETURN": "https://example.test/games",
        "ARCADE_SOCIAL_SAVES_PLAYER_MAX_BYTES": "1000",
        "ARCADE_SOCIAL_SAVES_DB_MAX_BYTES": "2000",
    })
    assert config.gameDomain == "games.example.test"
    assert config.userauthUrl == "http://auth.internal:9000"
    assert config.forwardClientIp is True
    assert (config.refreshDays, config.nameChangeDays, config.nameHoldDays) == (7, 14, 21)
    assert config.logRetentionDays == 45 and config.validateCacheSeconds == 15
    assert config.userauthTimeout == 2.5
    assert config.defaultReturn == "https://example.test/games"
    assert (config.savesPlayerMaxBytes, config.savesDatabaseMaxBytes) == (1000, 2000)


@pytest.mark.parametrize("value, expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("", False), ("enabled", False),
])
def test_boolean_variables_accept_only_the_listed_true_words(value, expected):
    assert Config.fromEnvironment({"ARCADE_SOCIAL_TRUST_FORWARDED_FOR": value}).trustForwardedFor is expected


def test_a_hall_origin_under_a_configured_game_domain_is_refused():
    environ = {"ARCADE_SOCIAL_GAME_DOMAIN": "games.example.test"}
    # The default game domain's hosts are no longer game hosts...
    assert Config.fromEnvironment(dict(environ, ARCADE_SOCIAL_HALL_ORIGINS="https://fishe.play.danielstephenson.dev"))
    # ...but the configured one's are.
    with pytest.raises(ConfigError):
        Config.fromEnvironment(dict(environ, ARCADE_SOCIAL_HALL_ORIGINS="https://fishe.games.example.test"))


def test_a_numeric_error_names_the_variable_family():
    with pytest.raises(ConfigError) as error:
        Config.fromEnvironment({"ARCADE_SOCIAL_NAME_HOLD_DAYS": "a month"})
    assert "numeric ARCADE_SOCIAL_* variable" in str(error.value)


class _CountingLoader(object):
    """Reads a text file; "bad" in it is a parse error. Counts the reads."""

    def __init__(self):
        self.calls = 0

    def __call__(self, path):
        self.calls += 1
        with open(path) as handle:
            text = handle.read()
        if "bad" in text:
            raise ValueError("bad file")
        return text


def _touch(path, offset):
    later = time.time() + offset
    os.utime(str(path), (later, later))


def _holder(cls, path, loader):
    return cls(str(path), loader, "EMPTY", lambda value: repr(value), (ValueError,))


def test_an_unchanged_file_is_not_parsed_again(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("one")
    loader = _CountingLoader()
    holder = _holder(FileHolder, path, loader)
    assert holder.value == "one" and holder.value == "one"
    assert loader.calls == 1
    path.write_text("bad")
    _touch(path, 5)
    assert holder.value == "one"
    assert holder.value == "one"
    assert loader.calls == 2  # a broken file is reported once, not on every read


def test_a_required_file_must_exist_at_start_and_is_kept_if_it_goes_away(tmp_path):
    path = tmp_path / "file.txt"
    with pytest.raises(OSError):
        _holder(FileHolder, path, _CountingLoader())
    path.write_text("one")
    holder = _holder(FileHolder, path, _CountingLoader())
    path.unlink()
    assert holder.value == "one"


def test_an_optional_file_may_be_absent_and_appear_later(tmp_path):
    path = tmp_path / "saves.yaml"
    loader = _CountingLoader()
    holder = _holder(OptionalFileHolder, path, loader)
    assert holder.value == "EMPTY" and loader.calls == 0
    path.write_text("one")
    assert holder.value == "one"
    path.unlink()
    assert holder.value == "one"  # once loaded, removal keeps the last good value


def test_an_optional_file_that_is_broken_at_start_is_still_refused(tmp_path):
    path = tmp_path / "saves.yaml"
    path.write_text("bad")
    with pytest.raises(ValueError):
        _holder(OptionalFileHolder, path, _CountingLoader())
