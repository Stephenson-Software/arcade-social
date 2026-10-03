# @author Daniel McCoy Stephenson
"""Command line: serve, back up, check the config files, operator tools.

    python -m arcade_social serve
    python -m arcade_social backup FILE              (online copy of the live database)
    python -m arcade_social migrate                  (apply migrations and exit)
    python -m arcade_social check-config [--registry games.yaml] [--boards boards.yaml]
    python -m arcade_social admin entry ID
    python -m arcade_social admin delete-entry ID
    python -m arcade_social admin exclude USERNAME [--reason TEXT]
    python -m arcade_social admin unexclude USERNAME
    python -m arcade_social admin delete-likes USERNAME
    python -m arcade_social admin log SLUG BOARD [--limit N]

Every command reads the ARCADE_SOCIAL_* variables (the database path from
ARCADE_SOCIAL_DB), so on the gateway they run inside the container:
    docker exec arcade-social python -m arcade_social backup /data/backup.sqlite3
"""

import argparse
import json
import os
import sys

from arcade_social import __version__, boards, registry
from arcade_social.config import Config, ConfigError, log
from arcade_social.store import SchemaTooNew, Store


def serve(arguments):
    from arcade_social.server import Social, makeServer, startMaintenance

    config = Config.fromEnvironment()
    social = Social(config)
    host = os.environ.get("ARCADE_SOCIAL_HOST", "0.0.0.0")
    port = int(os.environ.get("ARCADE_SOCIAL_PORT", "8080"))
    server = makeServer(social, host, port)
    before, after = social.store.migrated
    log(
        "arcade-social %s on %s:%d for %s (database %s, schema %d%s; registry %s; boards %s; userauth %s; "
        "%d operator(s))"
        % (
            __version__,
            host,
            port,
            config.publicUrl,
            config.databasePath,
            after,
            "" if before == after else ", migrated from %d" % before,
            config.registryPath,
            config.boardsPath,
            config.userauthUrl,
            len(config.operators),
        )
    )
    startMaintenance(social)
    server.serve_forever()
    return 0


def _store():
    return Store(Config.fromEnvironment().databasePath)


def backup(arguments):
    if os.path.exists(arguments.file):
        print("backup: %s exists; refusing to overwrite it" % arguments.file, file=sys.stderr)
        return 1
    _store().backup(arguments.file)
    print("backed up to %s" % arguments.file)
    return 0


def migrate(arguments):
    store = _store()
    before, after = store.migrated
    print("schema version %d%s" % (after, "" if before == after else " (was %d)" % before))
    return 0


def checkConfig(arguments):
    config = Config.fromEnvironment()
    registryPath = arguments.registry or config.registryPath
    boardsPath = arguments.boards or config.boardsPath
    loadedRegistry = registry.load(registryPath, domain=config.gameDomain)
    loadedBoards = boards.load(boardsPath)
    print("%s: %d game(s) OK" % (registryPath, len(loadedRegistry)))
    print("%s: %d game(s) OK" % (boardsPath, len(loadedBoards)))
    missing = [slug for slug in loadedBoards if loadedRegistry.get(slug) is None]
    if missing:
        # Not an error: a game may have left the registry while its records stay.
        print("note: declared but not in the registry (no page can report to them): %s" % ", ".join(missing))
    return 0


def admin(arguments):
    store = _store()
    action = arguments.action
    if action == "entry":
        found = store.entry(arguments.id)
        print(json.dumps(found, indent=2, sort_keys=True) if found else "no such entry")
        return 0 if found else 1
    if action == "delete-entry":
        removed = store.deleteEntry(arguments.id)
        print("deleted" if removed else "no such entry")
        return 0 if removed else 1
    if action == "exclude":
        store.exclude(arguments.username.lower(), arguments.reason)
        print("excluded %s from every public leaderboard" % arguments.username.lower())
        return 0
    if action == "unexclude":
        removed = store.unexclude(arguments.username.lower())
        print("unexcluded" if removed else "that account is not excluded")
        return 0 if removed else 1
    if action == "delete-likes":
        print("deleted %d like(s)" % store.deleteLikesOf(arguments.username.lower()))
        return 0
    if action == "log":
        print(json.dumps(store.submissionLog(arguments.slug, arguments.board, arguments.limit), indent=2))
        return 0
    return 2


def main(argv=None):
    parser = argparse.ArgumentParser(prog="arcade_social", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version="arcade-social " + __version__)
    commands = parser.add_subparsers(dest="command")
    commands.required = True
    commands.add_parser("serve", help="run the server (configured by ARCADE_SOCIAL_* variables)")
    backupCommand = commands.add_parser("backup", help="write an online copy of the database to FILE")
    backupCommand.add_argument("file")
    commands.add_parser("migrate", help="apply schema migrations and exit")
    check = commands.add_parser("check-config", help="validate games.yaml and boards.yaml")
    check.add_argument("--registry")
    check.add_argument("--boards")

    adminCommand = commands.add_parser("admin", help="operator tools")
    actions = adminCommand.add_subparsers(dest="action")
    actions.required = True
    for name in ("entry", "delete-entry"):
        action = actions.add_parser(name)
        action.add_argument("id", type=int)
    exclude = actions.add_parser("exclude")
    exclude.add_argument("username")
    exclude.add_argument("--reason")
    for name in ("unexclude", "delete-likes"):
        action = actions.add_parser(name)
        action.add_argument("username")
    logCommand = actions.add_parser("log")
    logCommand.add_argument("slug")
    logCommand.add_argument("board")
    logCommand.add_argument("--limit", type=int, default=100)

    arguments = parser.parse_args(argv)
    handlers = {"serve": serve, "backup": backup, "migrate": migrate, "check-config": checkConfig, "admin": admin}
    try:
        return handlers[arguments.command](arguments)
    except (registry.RegistryError, boards.BoardsError, ConfigError, SchemaTooNew) as e:
        print("arcade-social: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
