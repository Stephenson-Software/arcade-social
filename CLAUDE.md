# CLAUDE.md

Guidance for Claude Code sessions in this repository.

## Project overview

arcade-social provides sign-in, high scores, achievements, likes and opt-in cloud saves for the browser games on danielstephenson.dev/play, served from `https://api.play.danielstephenson.dev`. It is one small stdlib-Python HTTP process with SQLite (WAL) databases on a volume. Accounts belong to UserAuth, reached over an internal network, and this service never stores a password. `README.md` is the full reference: API, pages, configuration, rate limits, cloud saves, privacy, backups, and the Decisions table.

## Technology stack

- Python, `requires-python >=3.8`. CI tests on 3.8 and 3.12. The Docker image is `python:3.12-slim`.
- **Standard library only.** There is nothing to `pip install` at runtime. pytest is used for the tests only.
- SQLite via `sqlite3`: `/data/arcade-social.sqlite3`, plus `/data/saves.sqlite3` for cloud saves.
- `clients/js/arcade-scores.js`: a dependency-free browser client, tested with `node --test` (Node 20+, CI uses 22).
- black is configured in `pyproject.toml` (`line-length = 110`, excludes `registry.py`). CI does not run it.

## Build and run commands

```bash
# Validate the example configuration (also a CI step)
PYTHONPATH=src python3 -m arcade_social check-config \
  --registry examples/games.yaml --boards examples/boards.yaml --saves examples/saves.yaml

# Other subcommands: serve, migrate, backup FILE, backup-saves FILE, admin <action>
PYTHONPATH=src python3 -m arcade_social --help

# Container (what CI's docker job builds and smoke-tests)
docker build -t arcade-social:ci .
```

## Test commands

```bash
# Full Python suite (~4 minutes: it runs the real server against a fake UserAuth)
python -m pytest -q
python -m pytest tests/test_api.py -q           # one file
python -m pytest tests/test_api.py -k <name>    # one test

# JS client
node --test clients/js/arcade-scores.test.js
```

Use a Python that has pytest installed. CI does `pip install pytest` and runs `python -m pytest -v`. `pytest.ini` sets `testpaths = tests` and `pythonpath = src tests`. Tests live in `tests/test_*.py`. Shared fixtures are in `tests/conftest.py` (`env`) and `tests/helpers.py`. `tests/fakeuserauth.py` is a stdlib fake of the UserAuth behaviour the service depends on (single-use refresh tokens, revocation, 429 + `Retry-After`). `tests/savesclient.py` is the reference cloud-saves client.

CI (`.github/workflows/test.yml`, workflow **Test**) has three jobs: `test` (matrix 3.8 / 3.12: pytest plus the `check-config` step), `js-client`, and `docker` (build, non-root health check, origin checks, WAL migrate and online backup). `tests/test_store.py::test_concurrent_first_start_migrates_once` has failed once on 3.8 with `database is locked` (main, 2026-10-05) and passed on re-run. Treat a single failure there as a known flake. Investigate it if it repeats.

## Repository layout

```
src/arcade_social/
  __main__.py      CLI: serve, migrate, backup, backup-saves, check-config, admin
  server.py        HTTP routes
  userauth.py      UserAuth client (validate, refresh coalescing)
  origins.py       which origins may call with credentials (from arcade's games.yaml)
  registry.py      VENDORED byte-for-byte from arcade (src/arcade/registry.py)
  boards.py        boards.yaml: score boards and achievements
  store.py         main SQLite schema, migrations and queries (player data)
  saves.py / saves_store.py / savesconfig.py   cloud saves (RFC 0016), off by default
  displaynames.py, ratelimit.py, pages.py, config.py
  __init__.py      __version__
tests/             pytest suite + fake UserAuth
clients/js/        arcade-scores.js browser client + node tests + README
examples/          games.yaml, boards.yaml, saves.yaml (validated in CI)
Dockerfile         non-root `social` user, /data volume, /healthz healthcheck
CHANGELOG.md       Keep a Changelog
```

## Coding conventions

- Every source and test file starts with `# @author Daniel McCoy Stephenson`.
- Function and local variable names are camelCase (`checkPath`, `validateUpload`, `backupCommand`). Private helpers get a leading underscore. Classes are PascalCase.
- black style at line length 110. Format only the files you change.
- Every user-visible change gets an entry under `## [Unreleased]` in `CHANGELOG.md`.
- New endpoints and behaviours get tests through the real server and the fake UserAuth (`env` fixture), not by mocking the server.

## Do / Don't

**Do:**
- Keep it stdlib-only. Add no runtime dependency and no `requirements.txt`.
- Keep every leaderboard response labelled `"verified": false`. Scores come from the player's browser and can be forged (RFC 0014 §4).
- Update the README's JSON API, Configuration and related sections whenever an endpoint, variable or limit changes.
- Treat schema changes in `store.py` and `saves_store.py` as data migrations: add a migration and never rewrite or drop existing player data. The release automation holds a release that touches these two files until the owner approves it.

**Don't:**
- Don't hand-edit `src/arcade_social/registry.py`. When arcade's registry changes, re-vendor the whole file and update the pinned hash in `tests/test_main.py`.
- Don't bump `__version__` in `src/arcade_social/__init__.py`, tag, or cut releases by hand. Releases and the following `.dev0` bump are made by the release automation.
- Don't weaken the sign-in boundary: `__Host-` HttpOnly cookies, the allowed-origin check, `X-Play-Client: 1` and CSRF tokens on writes.
- Don't enable cloud saves by default. `ARCADE_SOCIAL_SAVES` stays `off` unless set.
- Don't commit SQLite files or `/data/`. `.gitignore` covers them.
- This repository is public. Don't name private repositories, or deployment details beyond what the README already states, in code, docs, issues or PRs.
