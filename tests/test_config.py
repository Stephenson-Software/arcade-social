# @author Daniel McCoy Stephenson
import os
import time

import pytest

from arcade_social.config import Config, ConfigError, FileHolder
from arcade_social import boards


def test_environment_defaults_and_overrides():
    config = Config.fromEnvironment({})
    assert config.serviceOrigin == "https://api.play.danielstephenson.dev"
    assert config.portalOrigin == "https://danielstephenson.dev"
    assert config.registryPath == "/config/arcade/games.yaml" and config.boardsPath == "/config/play/boards.yaml"
    assert config.databasePath == "/data/arcade-social.sqlite3"
    assert config.userauthUrl == "http://userauth:9998"
    assert config.operators == frozenset() and config.trustForwardedFor is False
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


@pytest.mark.parametrize("environ", [
    {"ARCADE_SOCIAL_PUBLIC_URL": "api.example.test"},
    {"ARCADE_SOCIAL_PUBLIC_URL": "https://api.example.test/path"},
    {"ARCADE_SOCIAL_PORTAL_ORIGIN": "http://example.test"},
    {"ARCADE_SOCIAL_REFRESH_DAYS": "thirty"},
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
