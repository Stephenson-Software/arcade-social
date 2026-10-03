# arcade-scores.js

A one-file browser client for [arcade-social](../../README.md)'s scores and achievements (Stephenson-Software
RFC 0014 §2), for any game on arcade that is not a tak game: pygbag, Emscripten, CheerpJ or plain JavaScript.
(tak games use `tak.arcade`, which does the same through the tak runtime.) It has no build step and no
dependencies, and is under 3 KB.

**Vendor it.** Copy `arcade-scores.js` into the game's build and load it with a `<script>` tag; do not load it
from the service. A copy in the game's own origin keeps working under `Cross-Origin-Embedder-Policy: require-corp`
and pins the game to the version it was tested with. It defines `window.ArcadeScores` (and `module.exports`
under Node).

```html
<script src="arcade-scores.js"></script>
<script>
  ArcadeScores.submit("fastest-crossing", 41.7);          // or submitScore(board, value, run?)
  ArcadeScores.unlock("first-win");
  ArcadeScores.whoami().then(me => { /* null, or {signedIn, displayName} */ });
  ArcadeScores.top("fastest-crossing", 10).then(rows => { /* [{id, rank, displayName, value, achievedAt}] */ });
  someButton.onclick = () => ArcadeScores.signIn();       // top-level navigation to the sign-in page
</script>
```

| Call | Does | Resolves to |
|---|---|---|
| `submit(board, value, run?)` (alias `submitScore`) | `POST /v1/scores/<board>` `{value, run?}` | the service's `{slug, board, best, improved, rank}`, or `null` |
| `unlock(id)` | `POST /v1/achievements/<id>` `{}` (idempotent) | `{slug, achievement, unlocked, newlyUnlocked, unlockedAt}`, or `null` |
| `whoami()` | `GET /v1/session` with credentials, cached for a minute | `{signedIn, displayName}`, or `null` when it cannot be known |
| `top(board, limit = 10)` | `GET /v1/boards/<this game>/<board>?limit=` (public, `limit` 1–100) | the entries, or `[]` |
| `signIn()` | Navigates the **top** window to `https://api.play.danielstephenson.dev/signin?return=<this page>` | — |

**Fire-and-forget.** No call ever throws or rejects; the promises are there for a game that wants the answer,
and a game that ignores them loses nothing. Writes carry `credentials: "include"`,
`Content-Type: application/json` and `X-Play-Client: 1`, as the service requires.

**Nothing is sent:**

- unless the page is `https://<slug>.play.danielstephenson.dev`. The service decides the game from the
  request's `Origin`, so a game on an alias, on `localhost` or in a desktop build has nothing to report to;
- when `whoami()` says the player is signed out (a score earned signed out is never uploaded later, RFC 0014);
- for a malformed call: an id that is not `^[a-z][a-z0-9-]{1,30}$`, a value that is not a finite number, a
  `run` that is not 1–64 of `A-Za-z0-9._:-`.

A refusal (an undeclared board, a value outside the board's `min`..`max`, no display name yet, `maxPerHour`)
resolves to `null` and is not retried. A network failure or a 5xx is retried once, 30 s later; after that the
report is dropped. A lost score is acceptable; a stalled game is not.

The client does not filter: send every score and the service keeps each player's best. **Scores come from the
player's browser and can be forged;** every board is labelled "not verified". Declare a game's boards and
achievements in the gateway's `config/play/boards.yaml` first.

A pygbag game can call it through `platform.window.ArcadeScores`, and an Emscripten game through `EM_ASM`.

## Tests

```sh
node --test clients/js/arcade-scores.test.js
```
