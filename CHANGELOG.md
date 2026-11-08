# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and the project uses semantic versioning.

## [Unreleased]

### Added

- `/account/export` lists every cloud-save version of each game, not only the newest 200. The versions
  are read 200 at a time and the export is written to a temporary file (on disk past 4 MB) before it is
  sent, so a long history is never held in memory at once. File contents stay out of the export as before.
- `/account/export` includes `nameHolds`: the folded key (`display_key`) and `released_at` of each display
  name the player gave up, as stored. A hold is still kept until that name is taken again or the account
  is deleted; nothing is pruned.

### Fixed

- A reply from UserAuth that is not valid HTTP (a bad status line, or a body shorter than its
  `Content-Length`) now gives the 503 "Sign-in is unavailable" answer instead of a 500. A truncated body on
  a 400, 401, 409 or 429 keeps that status's own answer.

## [0.3.1] - 2026-11-05

### Changed

- No change to the service's behaviour. The README's rate-limit table, API responses, upload order and RFC list
  were made to match the code (#24, #25), and tests were added for the UserAuth client's refusal, expiry
  and cache paths (#27).

## [0.3.0] - 2026-11-01

### Added

- The walkable arcade hall as a caller (`ARCADE_SOCIAL_HALL_ORIGINS`, default
  `https://play.danielstephenson.dev,https://arcade.danielstephenson.dev`): it may read the session, the
  player's likes and own bests, and like or unlike any registry game, but never submit scores or
  achievements, use cloud saves or delete data. Hall URLs are valid sign-in `return` targets.
- Live presence in the walkable hall (RFC 0030 §4), opt-in: `GET/POST/DELETE /v1/presence`,
  `GET /v1/presence/me`, `POST/DELETE /v1/presence/optin` and a toggle on `/account`. Poses are kept in
  memory for 10 s only and never written or logged; listings carry display names and random per-visit ids,
  at most 50 people. The opt-in flag is a new column on the player row (schema version 4), exported and
  deleted with the account; deleted, opted-out and excluded accounts disappear at once.
- `GET /v1/boards/top` (public, `Cache-Control: public, max-age=60`): every declared board of every
  registry game with its #1 entry (`displayName`, `value`, `achievedAt`) or `null`, labelled
  `"verified": false`, in one request and one database query. Exclusions, hidden names and tie order are
  the per-board endpoint's. The hall's Hall of Fame reads it instead of one request per game and board.
- `ARCADE_SOCIAL_HALL_PRESENCE=off|on` (default `on`): a kill switch for live hall presence (RFC 0030),
  like `ARCADE_SOCIAL_SAVES`. Off, listings answer `{people: [], ttl, disabled: true}`, poses and opt-ins
  answer 503 `{error, disabled: true}`, and the in-memory table is emptied; leaving and opting out still
  work, and the stored opt-in flag is kept for when it is turned back on.

### Changed

- `registry.py` re-vendored from arcade, so a `games.yaml` that uses arcade's `canonical`, `added`,
  `tags` and `engine` keys loads instead of being refused with "unknown key" (#18).

### Fixed

- Starting the service (or `migrate`) while another process opens the same fresh database or saves
  database no longer fails with `database is locked`. SQLite rejects the switch to WAL mode at once,
  without waiting on the 30 s busy timeout, while another connection holds a write lock, so the switch is
  now retried for up to 30 s.

## [0.2.2] - 2026-10-23

### Changed

- No change to the service's behaviour. Added `CLAUDE.md` (#7), and the README now lists the account
  saves pages, the version caps and `check-config --saves` (#9).

## [0.2.1] - 2026-10-04

### Added

- `GET /version.json`: `{"version": "<arcade_social.__version__>"}` with `Content-Type: application/json`
  and `Cache-Control: no-store`, no sign-in and no cookies, so a deploy can be verified by the version
  it reports.

### Changed

- Tests only: `tests/savesclient.py`, the reference cloud-saves client, now follows the games' own
  `cloud.js` engine, and the randomized protocol test checks more (#3).

## [0.2.0] - 2026-10-03

### Added

- Cloud saves (RFC 0016), off by default (#2). A second SQLite file, `/data/saves.sqlite3`, keeps every
  version of a player's saves that a game page uploads, append-only, with the version it was based on.
  The service checks the save file's envelope, never its contents. Uploads are refused, writing nothing,
  with 409 on a stale parent, 422 on a dropped unit (unless `confirmRemoved` names exactly the dropped
  units) or a halving (unless `confirmShrink`), and 413 over a quota; old versions are never pruned.
- `/v1/saves/<store>` (`GET`, `PUT`, `DELETE`), `/v1/saves/<store>/enroll` (`POST`, `DELETE`),
  `GET /v1/saves/<store>/versions`, `GET /v1/saves/<store>/versions/<id>` and
  `POST /v1/saves/<store>/versions/<id>/pin`.
- `ARCADE_SOCIAL_SAVES=off|readonly|on` (default `off`), `ARCADE_SOCIAL_SAVES_ACCOUNTS` (unset is nobody,
  `*` is everyone) and a strictly parsed `saves.yaml` per game.
- The account page `/account/saves` (downloads and a typed delete), and cloud saves in `/account/export`.
- `python -m arcade_social backup-saves FILE` (online backup plus `integrity_check`) and the
  `admin saves-check`, `saves-usage` and `saves-versions` actions.

### Changed

- `DELETE /v1/me` and `/account/delete` remove the player's cloud saves first, so a failure between the
  two files leaves them deletable.

## [0.1.1] - 2026-10-03

### Added

- `clients/js/arcade-scores.js`: a vendorable, dependency-free browser client (under 3 KB) for static games:
  `submit`/`submitScore`, `unlock`, `whoami`, `top` and `signIn`. Sends only from a
  `https://<slug>.play.danielstephenson.dev` page for a signed-in player, never throws, retries a failed
  report once. Tested under `node --test` in CI (new `js-client` job).

### Fixed

- A registered game with no declarations gets empty board and achievement listings instead of a 404,
  which every game page logged as a console error. Unknown slugs still 404.

## [0.1.0] - 2026-10-03

### Added

- Sign-in for arcade games (Stephenson-Software RFC 0013 §1–§2): the service's own sign-in and
  registration pages proxy to UserAuth over the internal network and keep its tokens in host-only
  `__Host-` cookies (Secure, HttpOnly, SameSite=Lax). Access tokens are validated with UserAuth
  `/session/validate` (cached at most 60 s) and refreshed server-side, with concurrent refreshes
  coalesced because UserAuth refresh tokens are single-use.
- Origin-based access: arcade's `games.yaml` (read with arcade's own, vendored parser) decides which
  origins may call with credentials and which game each acts for; cookie writes need JSON +
  `X-Play-Client: 1` + an allowed Origin; forms carry a double-submit CSRF token.
- Display names chosen on first sign-in: unique by a folded key, 3–20 ASCII characters, a small
  impersonation and profanity guard, one change per 30 days, released names held for 30 days.
- Scores and achievements (RFC 0014, Tier 0): declarations in `boards.yaml`, bounds, integer and
  `maxPerHour` checks, best-per-board, competition-ranked public leaderboards and "around me",
  idempotent unlocks with public percentages, every response labelled `verified: false`.
- Likes (RFC 0015): one per account per game by `UNIQUE (player_id, slug)`, idempotent like/unlike,
  public counts for registry games, private "my likes", likes kept when a game leaves the registry.
- Privacy: no IPs stored (rate limits are in memory), JSON export, immediate deletion of all of a
  player's data or one game's, 90-day submission log retention.
- Operator tools over HTTP (allowlisted accounts, own origin only) and on the command line.
- SQLite in WAL mode with numbered forward-only migrations (`schema_version`), refusal of newer
  schemas, and `python -m arcade_social backup FILE` using the online backup API.
- Dockerfile (non-root, `/data` volume) and CI on Python 3.8 and 3.12 plus a Docker smoke test.
