# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and the project uses semantic versioning.

## [Unreleased]

### Added

- `clients/js/arcade-scores.js`: a vendorable, dependency-free browser client (under 3 KB) for static games:
  `submit`/`submitScore`, `unlock`, `whoami`, `top` and `signIn`. Sends only from a
  `https://<slug>.play.danielstephenson.dev` page for a signed-in player, never throws, retries a failed
  report once. Tested under `node --test` in CI (new `js-client` job).

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
