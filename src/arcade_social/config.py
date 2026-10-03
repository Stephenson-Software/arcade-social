# @author Daniel McCoy Stephenson
"""Configuration, read from ARCADE_SOCIAL_* environment variables, and the two
reviewed files the service reads from the gateway repository:

  games.yaml   arcade's registry (which games exist, their slugs and aliases);
               it decides which origins may call the service (RFC 0013 §2)
  boards.yaml  each game's leaderboards and achievements (RFC 0014 §1)

Both are reloaded when they change on disk. A file that no longer parses is
logged and the last good copy stays in force, as arcade does with games.yaml.
"""

import os
import sys
import threading
from urllib.parse import urlparse

from arcade_social import boards as boardsModule
from arcade_social import registry as registryModule


def log(message):
    print("[arcade-social] %s" % message, file=sys.stderr, flush=True)


def _flag(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _names(value):
    return tuple(name.strip().lower() for name in (value or "").split(",") if name.strip())


class ConfigError(ValueError):
    """A configuration value is unusable. The message names the variable."""


class Config(object):
    """Everything the server needs."""

    def __init__(
        self,
        publicUrl="https://api.play.danielstephenson.dev",
        portalOrigin="https://danielstephenson.dev",
        gameDomain="play.danielstephenson.dev",
        registryPath="/config/arcade/games.yaml",
        boardsPath="/config/play/boards.yaml",
        databasePath="/data/arcade-social.sqlite3",
        userauthUrl="http://userauth:9998",
        operators=(),
        trustForwardedFor=False,
        forwardClientIp=False,
        refreshDays=30,
        nameChangeDays=30,
        nameHoldDays=30,
        logRetentionDays=90,
        validateCacheSeconds=60,
        userauthTimeout=5.0,
        defaultReturn="https://danielstephenson.dev/play",
    ):
        parsed = urlparse(publicUrl)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.path not in ("", "/"):
            raise ConfigError("ARCADE_SOCIAL_PUBLIC_URL must be scheme://host, got %r" % publicUrl)
        self.publicUrl = publicUrl.rstrip("/")
        # The service's own origin: where its sign-in pages live and where its
        # host-only cookies are set (RFC 0013 §1).
        self.serviceOrigin = "%s://%s" % (parsed.scheme, parsed.netloc.lower())
        portal = urlparse(portalOrigin)
        if portal.scheme != "https" or not portal.hostname or portal.path not in ("", "/"):
            raise ConfigError("ARCADE_SOCIAL_PORTAL_ORIGIN must be https://host, got %r" % portalOrigin)
        self.portalOrigin = "https://%s" % portal.netloc.lower()
        self.gameDomain = gameDomain.lower().strip(".")
        self.registryPath = registryPath
        self.boardsPath = boardsPath
        self.databasePath = databasePath
        self.userauthUrl = userauthUrl.rstrip("/")
        self.operators = frozenset(name.lower() for name in operators)
        self.trustForwardedFor = bool(trustForwardedFor)
        self.forwardClientIp = bool(forwardClientIp)
        self.refreshDays = int(refreshDays)
        self.nameChangeDays = int(nameChangeDays)
        self.nameHoldDays = int(nameHoldDays)
        self.logRetentionDays = int(logRetentionDays)
        self.validateCacheSeconds = min(60, int(validateCacheSeconds))
        self.userauthTimeout = float(userauthTimeout)
        self.defaultReturn = defaultReturn

    @classmethod
    def fromEnvironment(cls, environ=None):
        environ = os.environ if environ is None else environ

        def get(name, default):
            return environ.get("ARCADE_SOCIAL_" + name, default)

        try:
            return cls(
                publicUrl=get("PUBLIC_URL", "https://api.play.danielstephenson.dev"),
                portalOrigin=get("PORTAL_ORIGIN", "https://danielstephenson.dev"),
                gameDomain=get("GAME_DOMAIN", "play.danielstephenson.dev"),
                registryPath=get("REGISTRY", "/config/arcade/games.yaml"),
                boardsPath=get("BOARDS", "/config/play/boards.yaml"),
                databasePath=get("DB", "/data/arcade-social.sqlite3"),
                userauthUrl=get("USERAUTH_URL", "http://userauth:9998"),
                operators=_names(get("OPERATORS", "")),
                trustForwardedFor=_flag(get("TRUST_FORWARDED_FOR", "false")),
                forwardClientIp=_flag(get("FORWARD_CLIENT_IP", "false")),
                refreshDays=int(get("REFRESH_DAYS", "30")),
                nameChangeDays=int(get("NAME_CHANGE_DAYS", "30")),
                nameHoldDays=int(get("NAME_HOLD_DAYS", "30")),
                logRetentionDays=int(get("LOG_RETENTION_DAYS", "90")),
                validateCacheSeconds=int(get("VALIDATE_CACHE_SECONDS", "60")),
                userauthTimeout=float(get("USERAUTH_TIMEOUT", "5")),
                defaultReturn=get("DEFAULT_RETURN", "https://danielstephenson.dev/play"),
            )
        except ValueError as e:
            if isinstance(e, ConfigError):
                raise
            raise ConfigError("a numeric ARCADE_SOCIAL_* variable is not a number: %s" % e)


class FileHolder(object):
    """A parsed file, reloaded when its mtime changes. A file that no longer
    parses is logged and the last good value stays in force."""

    def __init__(self, path, loader, empty, describe, errors):
        self.path = path
        self._loader = loader
        self._describe = describe
        self._errors = errors
        self._lock = threading.Lock()
        self._mtime = None
        self._value = empty
        self.refresh(initial=True)

    def refresh(self, initial=False):
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except OSError as e:
            if initial:
                raise
            log("%s unreadable, keeping the last good one: %s" % (self.path, e))
            return
        with self._lock:
            if mtime == self._mtime:
                return
            try:
                loaded = self._loader(self.path)
            except self._errors + (OSError,) as e:
                if initial:
                    raise
                log("%s invalid, keeping the last good one: %s" % (self.path, e))
                self._mtime = mtime
                return
            self._value = loaded
            self._mtime = mtime
            log("%s loaded: %s" % (self.path, self._describe(loaded)))

    @property
    def value(self):
        self.refresh()
        return self._value


def registryHolder(config):
    return FileHolder(
        config.registryPath,
        lambda path: registryModule.load(path, domain=config.gameDomain),
        registryModule.Registry([]),
        lambda loaded: "%d game(s)" % len(loaded),
        (registryModule.RegistryError,),
    )


def boardsHolder(config):
    return FileHolder(
        config.boardsPath,
        boardsModule.load,
        boardsModule.Declarations({}),
        lambda loaded: "%d game(s) with boards or achievements" % len(loaded),
        (boardsModule.BoardsError,),
    )
