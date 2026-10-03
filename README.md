# arcade-social

Sign-in, high scores, achievements, likes and cloud saves for the browser games on
[danielstephenson.dev/play](https://danielstephenson.dev/play), served from
`https://api.play.danielstephenson.dev`.

It implements four Stephenson-Software RFCs:

| RFC | What this service does with it |
|---|---|
| 0013 Cloud saves | **Only §1–§2, the sign-in design.** Its §3–§6 are superseded by RFC 0016. |
| 0014 Scores and achievements | The service side (§1 declarations, §3 API, §4 Tier 0 checks). |
| 0015 Likes | The service side (§1 data model, §2 API). |
| 0016 Cloud saves | The service side: a second SQLite file, append-only versions, 409/422 guards, the kill switch (§3, §4.1, §6–§8). Off unless `ARCADE_SOCIAL_SAVES=on`. |

It is one small stdlib-Python process with one SQLite database (WAL mode) on a volume, in the
same shape as [arcade](https://github.com/Stephenson-Software/arcade): no dependencies, nothing to
`pip install`. Accounts belong to UserAuth (the shared account service), reached over the
gateway's internal network; this service never stores a password.

**Every score is reported by the player's own browser and can be forged.** The service checks what it
can (sign-in, declared boards, bounds, rate caps) and labels every leaderboard response
`"verified": false`. See RFC 0014 §4.

## Contents

- [How sign-in works](#how-sign-in-works)
- [Who may call what](#who-may-call-what)
- [JSON API](#json-api)
- [Pages](#pages)
- [boards.yaml](#boardsyaml)
- [Display names](#display-names)
- [Rate limits](#rate-limits)
- [Cloud saves](#cloud-saves)
- [Privacy and deletion](#privacy-and-deletion)
- [Configuration](#configuration)
- [Running on the gateway](#running-on-the-gateway)
- [Backups](#backups)
- [Operator tools](#operator-tools)
- [Decisions](#decisions)
- [Game clients](#game-clients)
- [Development](#development)

## How sign-in works

1. A game (or the portal) sends the player to `https://api.play.danielstephenson.dev/signin?return=<page URL>`
   with a normal, top-level navigation. The password is typed only on this service's own page, which
   loads no script at all (RFC 0013 §1: never in a game's page, because game pages load third-party
   code). It is not framed (`frame-ancestors 'none'`), so an embedded game must navigate the top window.
2. The form posts to `/signin`, which forwards the credentials to UserAuth `POST /login` on the internal
   network. On success the response sets two cookies:

   | Cookie | Holds | Attributes |
   |---|---|---|
   | `__Host-play_at` | UserAuth's access JWT | `Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=<until the JWT expires>` |
   | `__Host-play_rt` | UserAuth's refresh token | `Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=30 days` |

   No `Domain` attribute: the cookies are host-only, so no game, the portal, or any other subdomain is
   ever sent them. The `__Host-` prefix makes browsers enforce that (and `Secure`, `Path=/`). No page
   script can read them.
3. A player without a display name goes to `/welcome` to choose one, then back to the `return` URL. The
   return URL must be `https://` on the portal, a registry game (slug host or alias) or this service;
   anything else falls back to `ARCADE_SOCIAL_DEFAULT_RETURN`, so the page is not an open redirect.
4. Pages then call the API with `fetch(url, {credentials: "include"})`. Game origins and the service are
   the same *site*, so `SameSite=Lax` cookies are sent.

The service keeps **no session table**. It checks each access token with UserAuth
`GET /session/validate` (which checks revocation) and caches a positive answer for at most 60 seconds.
When the access token has expired it exchanges the refresh cookie at UserAuth `POST /token/refresh`
itself, on whichever request noticed, and sets the new cookies on that response. UserAuth refresh tokens
are single-use, so concurrent requests carrying the same old refresh token share one exchange (the result
is kept for 60 s) instead of signing each other out.

Registration (`/register`) proxies UserAuth's public `POST /register` (username 3–50 characters; password
8–72 with a lowercase letter, an uppercase letter, a digit and a symbol; UserAuth's own rules), then signs
in. No email is asked for. Accounts are shared: the same UserAuth account signs in to the other sites that
use UserAuth. UserAuth has no self-service password reset (the operator issues reset tokens by hand) and no
account deletion.

## Who may call what

**A request's `Origin` decides which game it acts for** (RFC 0013 §2). The API has no slug that a page
chooses for writes; a page at `https://kreatures.play.danielstephenson.dev` acts for `kreatures` and
nothing else, although it holds the same cookie as every other game.

| Origin | Treated as |
|---|---|
| `https://<slug>.play.danielstephenson.dev`, for a slug in arcade's registry | that game |
| `https://<alias>`, for an alias in arcade's registry | that alias's game |
| `https://danielstephenson.dev` (the portal) | the portal |
| `https://api.play.danielstephenson.dev` (this service) | the service (operator writes only) |
| anything else, or no `Origin` | nobody: no credentialed access |

The registry is arcade's own `games.yaml`, read with arcade's own parser (`src/arcade_social/registry.py`
is vendored byte-for-byte; a test pins its hash) and reloaded when the file changes.

Route access levels:

- **public** — anyone. `Access-Control-Allow-Origin: *`, no credentials (or, for an allowed origin, that
  origin with credentials).
- **private** — an allowed origin (game or portal); credentialed CORS; reads the signed-in player's own data.
- **write** — an allowed origin, **and** `Content-Type: application/json`, **and** `X-Play-Client: 1`.
  The last two cannot be sent cross-origin without a CORS preflight, which only allowed origins pass; the
  `Origin` is also checked server-side and a write without one is refused (RFC 0013 §1 CSRF rules).
- **operator** — a signed-in account listed in `ARCADE_SOCIAL_OPERATORS`; never answered with CORS headers;
  writes must come from the service's own origin with the same two headers.

The HTML forms carry their own CSRF token: a random `__Host-play_csrf` cookie (`SameSite=Strict`,
`HttpOnly`) that must equal the form's hidden field, and the form's `Origin` must be the service's own.

## JSON API

All bodies are JSON; unknown fields are refused (400). Times are ISO 8601 UTC. Errors are
`{"error": "<message>"}` (sometimes with extra fields, below). Every score and leaderboard response
carries `"verified": false` and
`"notice": "Scores are reported by players' browsers and are not verified."`

### Session

| Method | Path | Access | Result |
|---|---|---|---|
| `GET` | `/v1/session` | private | `{signedIn, displayName, needsDisplayName, signInUrl, accountUrl}` |
| `POST` | `/v1/signout` | write | Revokes the session at UserAuth, clears the cookies → `{signedIn: false}` |

The UserAuth username is never returned by the API (it is private); only the display name is.

### Scores (RFC 0014)

| Method | Path | Access | Body / result |
|---|---|---|---|
| `POST` | `/v1/scores/<board>` | write, **game origin only** | `{value, run?}` → `{slug, board, best, improved, rank}` |
| `GET` | `/v1/games/<slug>/boards` | public | The game's board declarations |
| `GET` | `/v1/boards/<slug>/<board>?limit=10` | public | `{board, entries: [{id, rank, displayName, value, achievedAt}], total}`; `limit` 1–100 |
| `GET` | `/v1/boards/<slug>/<board>/around-me?window=5` | private | The entries around the player's own: `{entries, me}`; `window` 1–25 |
| `GET` | `/v1/me/<slug>` | private | `{displayName, bests: [{board, value, rank, achievedAt}], achievements: [...]}` |
| `DELETE` | `/v1/me/<slug>` | write | Deletes the player's scores, submissions and unlocks for that game → `{slug, deleted}` |

- The slug of a score comes from `Origin`; the portal cannot submit scores (403).
- `value` must be a finite JSON number inside the board's `min`..`max`; on an `integer` board a whole
  number (`7.0` is accepted as `7`). Outside the limits → **422** `{error, min, max}`, and the refusal is
  logged for the operator.
- A player must have a display name before a score is accepted → **409** `{error, welcome}`.
- `maxPerHour` submissions per player per board → **429** with `Retry-After`.
- `run` is an optional opaque tag (1–64 of `A-Za-z0-9._:-`), stored in the submission log only.
- The server keeps each player's best per board in the board's order; a tie does not replace the stored
  best. `improved` says whether this submission became the best.
- Ranks are competition ranks: equal values share a rank (1, 1, 3); among equal values whoever reached it
  first is listed first. Excluded accounts are left out of every board, rank and percentage.
- A game page may ask `around-me`/`me` only about its own slug; the portal may ask about any.

### Achievements (RFC 0014)

| Method | Path | Access | Body / result |
|---|---|---|---|
| `POST` | `/v1/achievements/<id>` | write, **game origin only** | → `{slug, achievement, unlocked: true, newlyUnlocked, unlockedAt}`; idempotent |
| `GET` | `/v1/games/<slug>/achievements` | public | `{players, achievements: [{id, title, description, hidden, players, percent}]}` |

`players` is the number of (non-excluded) accounts with any score or unlock in the game; `percent` is
the share of them holding each achievement, to one decimal. A `hidden: true` achievement is listed as
"Hidden achievement" with no description. No display name is needed to unlock.

### Likes (RFC 0015)

| Method | Path | Access | Result |
|---|---|---|---|
| `POST` | `/v1/likes/<slug>` | write | `{slug, count, liked: true}`; idempotent |
| `DELETE` | `/v1/likes/<slug>` | write | `{slug, count, liked: false}`; idempotent |
| `GET` | `/v1/likes/counts` | public | `{slug: count, …}` for every registry game with at least one like |
| `GET` | `/v1/likes/me` | private | `[slug, …]`, the caller's likes, oldest first |

- One like per account per game, enforced by `UNIQUE (player_id, slug)`.
- The portal may like any game in the registry; a game's own page only that game (RFC 0015 §2).
- A like on a game that leaves the registry is kept: it still appears in `/v1/likes/me` with the raw
  slug and can still be removed, but it is not in `/v1/likes/counts` and the game cannot be liked again.

### Account

| Method | Path | Access | Body / result |
|---|---|---|---|
| `DELETE` | `/v1/me` | write | `{"confirm": "delete"}` → `{deleted}`: everything this service holds about the account |

### Operator (RFC 0014 §3, RFC 0015 operator tools)

| Method | Path | Result |
|---|---|---|
| `GET` | `/v1/admin/entries/<id>` | One leaderboard entry, with the account's username |
| `DELETE` | `/v1/admin/entries/<id>` | Removes the entry (its submissions stay in the log) |
| `GET` | `/v1/admin/log/<slug>/<board>?limit=100` | Recent submissions, accepted and refused |
| `GET` | `/v1/admin/exclusions` | Excluded accounts |
| `POST` | `/v1/admin/exclude/<username>` | `{reason?}`: hides the account from every public board and percentage |
| `DELETE` | `/v1/admin/exclude/<username>` | Lifts the exclusion |
| `DELETE` | `/v1/admin/likes/<username>` | Deletes every like of the account |

Leaderboard `entries[].id` is what `/v1/admin/entries/<id>` takes. The same tools exist on the command
line (below), which is the easier way to use them.

### Calling it from a page

```js
const API = "https://api.play.danielstephenson.dev";
const write = (method, path, body) => fetch(API + path, {
  method, credentials: "include",
  headers: {"Content-Type": "application/json", "X-Play-Client": "1"},
  body: JSON.stringify(body || {}),
});

const session = await (await fetch(API + "/v1/session", {credentials: "include"})).json();
if (!session.signedIn) location.href = session.signInUrl + "?return=" + encodeURIComponent(location.href);
await write("POST", "/v1/scores/most-money", {value: 12450});   // from the game's own origin
await write("POST", "/v1/likes/fishe");                          // from the portal or fishe's page
const counts = await (await fetch(API + "/v1/likes/counts")).json();   // public, no credentials
```

A page inside an iframe must send the player to sign in with `window.top.location`, because the sign-in
page refuses to be framed.

A page that writes must not run under `Referrer-Policy: no-referrer`: browsers then send `Origin: null` on
cross-origin POST/DELETE requests, and the service refuses a write whose Origin it cannot place. The
gateway's `secure-headers` (`strict-origin-when-cross-origin`) is fine.

## Pages

| Path | Purpose |
|---|---|
| `GET/POST /signin?return=` | Sign in |
| `GET/POST /register?return=` | Create a UserAuth account, then sign in |
| `GET/POST /welcome?return=` | Choose a display name (first sign-in) |
| `GET /account` | Username (private), display name, what is held, sign out, delete |
| `GET/POST /account/name` | Change the display name |
| `GET /account/export` | Download everything held about the account as JSON |
| `POST /account/delete` | Delete everything (type `delete` to confirm); also signs out |
| `POST /signout` | Sign out (revokes the session at UserAuth) |
| `GET /healthz` | `ok` |

Every page is sent with `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; img-src
'self'; form-action 'self' <portal> https://*.play.<domain> <aliases>; frame-ancestors 'none'; base-uri
'none'`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin` and `Cache-Control: no-store`.
(`same-origin`, not `no-referrer`: under `no-referrer` browsers send `Origin: null` with a page's own form
POSTs, and the form check would refuse them.)

## boards.yaml

Each game's leaderboards and achievements are declared in a reviewed file, `config/play/boards.yaml` in
the gateway repository (RFC 0014 §1), mounted read-only and reloaded when it changes. See
[`examples/boards.yaml`](examples/boards.yaml) and the format in
[`src/arcade_social/boards.py`](src/arcade_social/boards.py). It is parsed strictly (like arcade's
`games.yaml`): an unknown key, a bad indent or a duplicate id is an error naming its line, and a file that
stops parsing at runtime is logged while the last good one stays in force.

Board and achievement ids are permanent. Removing a declaration hides its board (the API answers 404);
its records stay in the database.

Check both files before a gateway PR:

```sh
PYTHONPATH=src python -m arcade_social check-config --registry games.yaml --boards boards.yaml
```

## Display names

Leaderboards show a display name, never the UserAuth username (owner's decision, 2026-10-03). It is chosen
on first sign-in (`/welcome`) and:

- is 3–20 characters: ASCII letters and digits with single spaces, `_`, `-` or `.` between them;
- is unique by a folded key — case, separators and the confusables `0/o` and `1/i/l` are folded, so
  `Dan_S` and `dan s` are one name and `Dan1el` collides with `Daniel`;
- may not claim to be staff or the owner (`admin`, `moderator`, `official`, the owner's names, and a few
  whole words such as `mod`, `root`, `staff`) unless the account is an operator; a short profanity list is
  refused for everyone. Both lists are deliberately small: they catch the obvious, and the operator can
  exclude an account for the rest;
- can be changed once every 30 days (`ARCADE_SOCIAL_NAME_CHANGE_DAYS`); the first choice is free;
- when changed, the old name stays reserved for its previous holder for 30 days
  (`ARCADE_SOCIAL_NAME_HOLD_DAYS`), so nobody can take it at once and pass as them. Deleting all data
  releases the name immediately.

## Rate limits

UserAuth limits `/login`, `/register` and `/token/refresh` to 20 requests per 60 s **per remote address**
and does not trust `X-Forwarded-For` by default (UserAuth `application.properties`,
`config/RateLimitFilter.java`). Every request from this service comes from one address, so UserAuth's
bucket is shared by every player of every game. The service therefore limits players itself, before
UserAuth sees anything:

| What | Limit | Key |
|---|---|---|
| Sign-in attempts | 10 / minute | client IP |
| Sign-in attempts | 10 / 15 minutes | username |
| Registrations | 5 / hour | client IP |
| API writes | 120 / minute | client IP |
| API writes | 60 / minute | account |
| Achievement unlocks | 100 / hour | account + game |
| Score submissions | `maxPerHour` per board (default 30) | account + board, counted in the database |
| Display-name changes | 20 form posts / hour, and one change per 30 days | account |

When UserAuth itself answers 429, the sign-in page says "busy, try again in a minute" (with UserAuth's
`Retry-After`) rather than "wrong password". Access tokens last 60 minutes and validation is cached, so a
signed-in player costs UserAuth's limited endpoints about one refresh an hour.

The client IP is the right-most `X-Forwarded-For` entry when `ARCADE_SOCIAL_TRUST_FORWARDED_FOR=true`
(Traefik appends the address it saw), otherwise the socket peer. Limits live only in memory, for one window,
and are lost on restart.

`ARCADE_SOCIAL_FORWARD_CLIENT_IP=true` makes the service pass the client IP to UserAuth as
`X-Forwarded-For`. That only helps if UserAuth runs with `RATE_LIMIT_TRUST_FORWARDED_FOR=true`, which would
also let every other internal caller choose its own key; it is off by default and not recommended without a
UserAuth change that trusts the header from this service alone.

## Cloud saves

RFC 0016. **Off by default** (`ARCADE_SOCIAL_SAVES=off`). A signed-in player turns it on per game from the
game's own Saves panel; the page keeps playing from its local store exactly as before, and the cloud is a
history of copies that never sits in the save path.

**What is stored.** In its own file, `/data/saves.sqlite3` (never `arcade-social.sqlite3`): every version a
page uploads, append-only, each with the version it was based on (`parent`). A version is the game's own
save file (`{"format": "tak-saves", "version": 1, "game": "<store>", "files": {...}}`, what "Download my
saves" writes), split into *units*: the first path segment under the game's root (`slot_3` for a
`SaveFileManager` game). A unit is stored once per player by the SHA-256 of its canonical encoding
(compact JSON, sorted keys, UTF-8). Nothing is ever pruned automatically, and nothing makes room by
deleting an old version.

**API** (only from the game's own page; the game is the request's `Origin`, never a parameter;
writes need `Content-Type: application/json` and `X-Play-Client: 1` like every write):

| Method | Path | |
|---|---|---|
| `GET` | `/v1/saves/<store>` | `{enrolled, allowed, writable, reason, pull, quota, head?}`. `head` is present only when enrolled; `head: null` (with 200) means "enrolled, nothing uploaded yet". **Any other answer is "unknown", never "empty".** |
| `POST` / `DELETE` | `/v1/saves/<store>/enroll` | Turn backing up on / off. Turning it off keeps every version. |
| `PUT` | `/v1/saves/<store>` | Upload `{parent, device, deviceLabel, kind, file, confirmRemoved?, confirmShrink?}` |
| `GET` | `/v1/saves/<store>/versions` | Newest first, with units, sizes, device labels |
| `GET` | `/v1/saves/<store>/versions/<id>` | That version as a save file (`?download=1` for an attachment) |
| `POST` | `/v1/saves/<store>/versions/<id>/pin` | Mark a version to keep |
| `DELETE` | `/v1/saves/<store>` | `{"confirm": "delete"}`: every version of this game for this player |

**Upload rules**, in order; a refusal writes nothing (each error carries a machine-readable `saves` code):

| Answer | `saves` | When |
|---|---|---|
| 400 | `invalid` | The save file fails the games' own import validation: format, version 1, `game` = store, every path under the root with no empty/`.`/`..` part, control character or backslash, ≤ 5000 files, contents text or `{"base64"}` |
| 409 | `stale` | `parent` is not the current head (also `null` when a head exists). Carries `head`. The page merges (keep both) and uploads again; there is no last-write-wins anywhere |
| 422 | `removed` / `confirm-mismatch` | The upload leaves out a unit head has, unless `confirmRemoved` names exactly the units left out (the page sends it only for slots the game itself deleted) |
| 422 | `shrink` | Nothing left out, but less than half head's size, unless `confirmShrink` (the player confirmed it) |
| 413 | `quota` / `too-large` | Per game per player (`maxStoredBytes`), per player (`ARCADE_SOCIAL_SAVES_PLAYER_MAX_BYTES`), or one upload (`maxUploadBytes`, refused before the body is read) |
| 429 | `rate` | More than 120 uploads per hour per player per game |
| 503 | `off` / `paused` / `full` | Kill switch, a read-only game, or the database at `ARCADE_SOCIAL_SAVES_DB_MAX_BYTES` |
| 200 | | The content equals head's: nothing stored, `{"id": head, "created": false}` |
| 201 | | A new version; head moved to it (compare-and-set in the same transaction) |

**Switches.** `ARCADE_SOCIAL_SAVES=off|readonly|on`: `readonly` refuses uploads, enrollment and pins
(503 `paused`) and keeps reads, downloads and deletion; `off` answers 503 `off` on every saves endpoint.
`ARCADE_SOCIAL_SAVES_ACCOUNTS` lists the usernames that may enroll and upload: unset or empty is
**nobody**, `*` is everyone. `config/play/saves.yaml` (below) turns each game on or read-only.

**saves.yaml** (`ARCADE_SOCIAL_SAVES_CONFIG`, default `/config/play/saves.yaml`; optional: a missing file
means no game is writable), parsed strictly and reloaded on change, like boards.yaml:

```yaml
games:
  night-ferry:
    mode: on                    # on | readonly
    store: night-ferry-saves    # the save file's `game`: the page's IndexedDB name
    format: tak-saves           # tak-saves | roam-saves
    root: /saves
    pull: true                  # Stage 2: pages may pull newer versions automatically (fast-forward)
    maxUploadBytes: 2097152
    maxStoredBytes: 26214400
```

A game that leaves the file, or is `mode: readonly`, keeps what it has: versions stay listed and
downloadable and can be deleted. **The account page** `/account/saves` lists every version with a download
(a file the game's "Load saves from a file" accepts) and a typed delete per game; it has no script.

**The client side** (merging per unit, keep-both, carry-forward of units a session lost track of, pulls as
imports with a local backup first) is in the games' runtime (tak `/tak/cloud.js`). `tests/savesclient.py`
is a reference copy of that algorithm, and `tests/test_saves_protocol.py` runs it as three devices
through random histories against these rules, checking after every step that no committed save is lost
(`SAVES_PROTOCOL_SEEDS`, default 400).

## Privacy and deletion

- **Held:** the UserAuth username (private), a display name, best scores, a 90-day submission log, unlocks,
  likes, timestamps; and the operator's exclusions (username + reason). Nothing else.
- **Not held:** passwords (forwarded to UserAuth, never stored or logged), IP addresses (only in rate-limit
  memory, for at most one window), user agents, device identifiers. There is no access log.
- **Export:** `/account/export` gives the player everything held about them.
- **Cloud saves** (RFC 0016), only for a player who turned them on: the save files a game's page uploaded,
  per version, with a random device id, a device label, the uploading origin and times. No IP address or
  user agent.
- **Deletion:** `/account/delete` or `DELETE /v1/me` removes the player's cloud saves (from
  `saves.sqlite3`, first) and then the player row and, by cascade, every score, submission, unlock, like and
  held name, immediately. `/account/saves` or `DELETE /v1/saves/<store>` removes one game's cloud saves;
  a browser's own saves are never touched by either. `DELETE /v1/me/<slug>` removes one game's scores and
  unlocks. An exclusion the operator made is moderation data and is kept. Backups age out on the backup
  set's own schedule.
- **UserAuth has no account deletion** (its controllers cover register, login, logout, password, session and
  token only). Deleting here does not delete the UserAuth account; the page says so.

## Configuration

Environment variables, all optional:

| Variable | Default | Meaning |
|---|---|---|
| `ARCADE_SOCIAL_PUBLIC_URL` | `https://api.play.danielstephenson.dev` | This service's own URL (its origin is where cookies live) |
| `ARCADE_SOCIAL_PORTAL_ORIGIN` | `https://danielstephenson.dev` | The portal origin |
| `ARCADE_SOCIAL_GAME_DOMAIN` | `play.danielstephenson.dev` | Games are `<slug>.<this>` |
| `ARCADE_SOCIAL_REGISTRY` | `/config/arcade/games.yaml` | arcade's registry |
| `ARCADE_SOCIAL_BOARDS` | `/config/play/boards.yaml` | Boards and achievements |
| `ARCADE_SOCIAL_DB` | `/data/arcade-social.sqlite3` | The SQLite database |
| `ARCADE_SOCIAL_USERAUTH_URL` | `http://userauth:9998` | UserAuth, internal |
| `ARCADE_SOCIAL_USERAUTH_TIMEOUT` | `5` | Seconds |
| `ARCADE_SOCIAL_OPERATORS` | (none) | Comma-separated UserAuth usernames allowed the operator tools |
| `ARCADE_SOCIAL_TRUST_FORWARDED_FOR` | `false` | Use the right-most `X-Forwarded-For` entry as the client IP (set `true` behind Traefik) |
| `ARCADE_SOCIAL_FORWARD_CLIENT_IP` | `false` | Pass the client IP on to UserAuth (see Rate limits) |
| `ARCADE_SOCIAL_REFRESH_DAYS` | `30` | Refresh cookie lifetime; match UserAuth's `REFRESH_TOKEN_EXPIRATION_DAYS` |
| `ARCADE_SOCIAL_VALIDATE_CACHE_SECONDS` | `60` | Positive validation cache; capped at 60 |
| `ARCADE_SOCIAL_NAME_CHANGE_DAYS` | `30` | Minimum days between display-name changes |
| `ARCADE_SOCIAL_NAME_HOLD_DAYS` | `30` | Days a released name stays reserved for its previous holder |
| `ARCADE_SOCIAL_LOG_RETENTION_DAYS` | `90` | Score submission log retention |
| `ARCADE_SOCIAL_DEFAULT_RETURN` | `https://danielstephenson.dev/play` | Where sign-in returns when `return` is missing or refused |
| `ARCADE_SOCIAL_HOST` / `ARCADE_SOCIAL_PORT` | `0.0.0.0` / `8080` | Listen address |
| `ARCADE_SOCIAL_SAVES` | `off` | Cloud saves: `off`, `readonly` or `on` (RFC 0016 §7) |
| `ARCADE_SOCIAL_SAVES_ACCOUNTS` | (nobody) | Usernames that may enroll and upload, comma-separated; `*` for everyone |
| `ARCADE_SOCIAL_SAVES_DB` | `/data/saves.sqlite3` | The cloud-saves database (a second file) |
| `ARCADE_SOCIAL_SAVES_CONFIG` | `/config/play/saves.yaml` | Which games keep cloud saves |
| `ARCADE_SOCIAL_SAVES_PLAYER_MAX_BYTES` | `314572800` | Stored per player across games, after dedupe |
| `ARCADE_SOCIAL_SAVES_DB_MAX_BYTES` | `10737418240` | At this size uploads stop for everyone (503 `full`); nothing is deleted |

No secret is needed: the service holds no JWT secret (it asks UserAuth) and no API key.

## Running on the gateway

Not deployed yet. What a gateway PR needs:

- **Service** built from this repository (submodule), `mem_limit` around 64–96m and `cpus: 0.25` like arcade.
- **Networks:** `traefik-network` (UserAuth is reachable there as `userauth:9998`, as barony-backend, trace
  and dpc-backend reach it).
- **Router:** `Host(\`api.play.danielstephenson.dev\`)` on `websecure` with the Let's Encrypt resolver and
  `secure-headers@file` (its `frameDeny` matches the pages' own). `api` is a reserved slug in arcade's
  registry, so no game can claim the host, and arcade's generated routers never route it. DNS: confirm the
  name resolves (a `*.play` record covers it if there is one; otherwise one A record).
- **Environment:** `ARCADE_SOCIAL_TRUST_FORWARDED_FOR=true`, `ARCADE_SOCIAL_OPERATORS=<owner's username>`.
- **Volumes:** a named volume at `/data` (the database; **back it up**), `./config/arcade:/config/arcade:ro`
  (the same directory arcade mounts; mount the directory, not the file, because git replaces files by
  rename) and `./config/play:/config/play:ro`.
- **A new file** `config/play/boards.yaml` (start from `games: {}` or from the example).
- **Cloud saves** (optional): `config/play/saves.yaml` and `ARCADE_SOCIAL_SAVES` / `ARCADE_SOCIAL_SAVES_ACCOUNTS`.
  Their database, `/data/saves.sqlite3`, is on the same volume and needs its own backup (`backup-saves`).
- **Healthcheck:** the image has one (`/healthz`), or copy arcade's compose healthcheck.
- **Backups:** add the volume to the gateway's backup set (see Backups).

A bind mount at `/data` must be writable by the image's non-root `social` user.

## Backups

The database is player data. Back it up while the service runs with SQLite's online backup API
(`sqlite3.Connection.backup`), which produces a consistent copy even during writes:

```sh
docker exec arcade-social python -m arcade_social backup /data/backup-$(date +%F).sqlite3
docker cp arcade-social:/data/backup-$(date +%F).sqlite3 .
```

The command refuses to overwrite an existing file. Do not copy the live `.sqlite3` file directly: in WAL
mode recent commits may still be in the `-wal` file.

Cloud saves live in a second file and have their own command, which also runs `PRAGMA integrity_check` on
the copy and prints its row counts (exit 1 if the check fails):

```sh
docker exec arcade-social python -m arcade_social backup-saves /data/saves-backup-$(date +%F).sqlite3
docker exec arcade-social python -m arcade_social admin saves-check
```

Schema changes are numbered, forward-only migrations recorded in `schema_version`; a number is never reused
or edited. A database written by a newer version is refused at start (`SchemaTooNew`), so rolling back to an
older image cannot write to a schema it does not understand: restore the matching backup instead.

## Operator tools

On the gateway, inside the container:

```sh
docker exec arcade-social python -m arcade_social admin log fishe most-money --limit 50
docker exec arcade-social python -m arcade_social admin entry 17
docker exec arcade-social python -m arcade_social admin delete-entry 17
docker exec arcade-social python -m arcade_social admin exclude someuser --reason "forged scores"
docker exec arcade-social python -m arcade_social admin unexclude someuser
docker exec arcade-social python -m arcade_social admin delete-likes someuser
docker exec arcade-social python -m arcade_social migrate
docker exec arcade-social python -m arcade_social check-config
docker exec arcade-social python -m arcade_social admin saves-usage someuser
docker exec arcade-social python -m arcade_social admin saves-versions someuser night-ferry night-ferry-saves
docker exec arcade-social python -m arcade_social admin saves-check
```

## Decisions

The owner's decisions of 2026-10-03, and, for every other open question, the option the RFC itself
recommended ("RFC recommendation taken"). Anything the RFCs did not settle is marked "implementation".

| Question | Decision | Source |
|---|---|---|
| Build scores/achievements (0014 OQ1) and likes (0015 OQ1)? | Both, now | Owner |
| Cloud saves (0013 OQ1)? | Not now: save export/import first | Owner |
| Cloud saves (0016 OQ1, 2026-10-03) | Build both Stage 1 (backup + manual load) and Stage 2 (fast-forward pulls) | Owner |
| Where cloud saves live (0016 OQ2) | Here, in a second SQLite file, behind `ARCADE_SOCIAL_SAVES` | RFC recommendation taken |
| Who may use them before an off-box backup exists (0016 OQ3) | Only the accounts in `ARCADE_SOCIAL_SAVES_ACCOUNTS` (the owner and the test account) | RFC recommendation taken |
| Quotas (0016 OQ4) | Set per game in saves.yaml after real save sizes are measured | RFC recommendation taken |
| Deletions (0016 OQ5) | Never propagated automatically | RFC recommendation taken |
| First game (0016 OQ7) | Night Ferry | Owner |
| Automatic pruning (0016 §6) | Not built: every version is kept until the player deletes it (a superset of the retention promise) | Implementation |
| New service or arcade (0013 OQ2, 0015 OQ5)? | One new small service for sign-in, scores, achievements, likes | Owner |
| Stack (0013 OQ3) | stdlib Python + SQLite (WAL) | Owner |
| Hostname (0013 OQ4) | `api.play.danielstephenson.dev` | Owner |
| Public names (0014 OQ3) | A separate display name chosen on first sign-in; the username stays private | Owner |
| Sign-in pattern (0013 OQ5) | Host-only `__Host-` HttpOnly cookies + credentialed CORS | RFC recommendation taken |
| Forgery tier (0014 OQ2) | Tier 0 only (no run tickets) | RFC recommendation taken |
| Where declarations live (0014 OQ4) | `config/play/boards.yaml` in the gateway | RFC recommendation taken |
| Signed-out scores uploaded after sign-in (0014 OQ6) | No | RFC recommendation taken |
| Which game first (0014 OQ5) | A small static game with an obvious score (RFC 0014 Rollout 4); not chosen here | RFC recommendation (deferred to adoption) |
| Replay verification (0014 OQ7) | Not in v1; the RFC makes no recommendation | Open |
| Likes or stars (0015 OQ2) | Likes | RFC recommendation taken |
| Public counts (0015 OQ3) | Public, as the DPC precedent | RFC recommendation taken |
| Display threshold (0015 OQ4) | None in the API (raw counts, as the precedent); the portal may hide small numbers | RFC precedent followed |
| In-game like button (0015 OQ6) | Portal only proposed for v1; the API also lets a game like only itself (RFC 0015 §2) | RFC recommendation taken |
| Portal shows sign-in state (0013 OQ8) | `GET /v1/session` lets it; whether it does is the portal's | Implementation |
| Split sign-in into its own RFC (0013 OQ9) | Not split; RFC 0013 records §1–§2 as accepted and implemented here | Implementation |
| Refresh | The service refreshes on whichever request finds the access token expired, coalescing concurrent refreshes; RFC 0013 §1 had the refresh cookie used only at `/auth/refresh`, which would make every game implement a 401-retry and race on single-use refresh tokens | Implementation (deviation, recorded in RFC 0013) |
| Identifiers | `INTEGER PRIMARY KEY` row ids instead of the RFCs' UUIDs (SQLite); one `player` mirror shared by scores and likes, keyed by the UserAuth username | Implementation |
| Exclusions | A table keyed by username, as RFC 0014 §3 | RFC |
| Registration | Proxied (UserAuth's `POST /register` is public); no email asked | Implementation |

## Game clients

Games do not need to hand-write the calls above:

- **tak games:** `tak.arcade.submitScore(board, value)` / `tak.arcade.unlock(id)` in
  [tak](https://github.com/Stephenson-Software/tak) (the Worker hands each report to the page, which sends it).
- **Every other kind** (pygbag, Emscripten, CheerpJ, plain JavaScript): vendor
  [`clients/js/arcade-scores.js`](clients/js/README.md), under 3 KB with no build step:
  `ArcadeScores.submit`, `unlock`, `whoami`, `top` and `signIn`.

Both send only from a `https://<slug>.play.danielstephenson.dev` page and only for a signed-in player, never
throw, never block the game, and retry a failed report once.

## Development

```sh
python -m pytest -q                       # Python 3.8+; stdlib only, pytest for the tests
node --test clients/js/arcade-scores.test.js   # the vendored JS client (Node 20+)
PYTHONPATH=src python -m arcade_social check-config --registry examples/games.yaml --boards examples/boards.yaml
```

The tests run the real server against a fake UserAuth (`tests/fakeuserauth.py`, stdlib `http.server`)
that reproduces the UserAuth behaviour the service depends on (single-use refresh tokens, revocation,
429 + `Retry-After`, validation messages).

`src/arcade_social/registry.py` is vendored byte-for-byte from arcade (`src/arcade/registry.py` at
`c34f931bca31482eb09bcee959e949922680b9e6`). When arcade's registry format changes, copy the file again and
update the hash in `tests/test_main.py`; until then this service would refuse a `games.yaml` arcade accepts
(it keeps the last good registry while running, but would not start).

## Licence

Stephenson Software Non-Commercial License (Stephenson-NC); see [LICENSE](LICENSE).
