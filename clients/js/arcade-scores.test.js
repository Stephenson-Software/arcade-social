// node --test clients/js/arcade-scores.test.js: arcade-scores.js with a stubbed fetch and location; no network.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const FILE = path.join(__dirname, "arcade-scores.js");
const A = require(FILE);
const API = "https://api.play.danielstephenson.dev";

let calls, session, answers;
function setUp(href, { signedIn = true, statuses = [] } = {}) {
  const url = new URL(href);
  globalThis.location = { protocol: url.protocol, hostname: url.hostname, href, assign: (u) => calls.push({ nav: u }) };
  calls = [];
  session = signedIn === "fail" ? "fail" : { signedIn, displayName: signedIn ? "Dan S" : null, needsDisplayName: false };
  answers = statuses.slice();
  A._reset(1);
}
globalThis.fetch = async (url, init = {}) => {
  calls.push({ url, method: init.method || "GET", credentials: init.credentials, headers: init.headers, body: init.body });
  if (url === API + "/v1/session") {
    if (session === "fail") throw new TypeError("Failed to fetch");
    return { ok: true, status: 200, json: async () => session };
  }
  if (url.startsWith(API + "/v1/boards/")) {
    return { ok: true, status: 200, json: async () => ({ entries: [{ rank: 1, displayName: "Dan S", value: 9 }], verified: false }) };
  }
  const next = answers.length ? answers.shift() : 200;
  if (next === "fail") throw new TypeError("Failed to fetch");
  if (next === "badjson") return { ok: true, status: 200, json: async () => { throw new SyntaxError("bad body"); } };
  return { ok: next < 400, status: next, json: async () => ({ ok: true }) };
};
const posts = () => calls.filter((c) => c.method === "POST");
const settle = () => new Promise((r) => setTimeout(r, 20));
const GAME = "https://frog.play.danielstephenson.dev/index.html";

test("is under 3 KB and exposes the documented calls", () => {
  assert.ok(fs.statSync(FILE).size < 3072, `${fs.statSync(FILE).size} bytes`);
  for (const name of ["submit", "submitScore", "unlock", "whoami", "top", "signIn"]) assert.equal(typeof A[name], "function");
  assert.equal(globalThis.ArcadeScores, A);
});

test("a signed-in player's score and unlock are credentialed JSON writes", async () => {
  setUp(GAME);
  assert.deepEqual(await A.submit("fastest-crossing", 41.7, "seed-1"), { ok: true });
  await A.submitScore("fastest-crossing", 40);
  await A.unlock("first-win");
  const [a, b, c] = posts();
  assert.equal(a.url, API + "/v1/scores/fastest-crossing");
  assert.deepEqual(JSON.parse(a.body), { value: 41.7, run: "seed-1" });
  assert.deepEqual(JSON.parse(b.body), { value: 40 });
  assert.equal(c.url, API + "/v1/achievements/first-win");
  assert.deepEqual(JSON.parse(c.body), {});
  for (const p of [a, b, c]) {
    assert.equal(p.credentials, "include");
    assert.deepEqual(p.headers, { "Content-Type": "application/json", "X-Play-Client": "1" });
  }
  assert.equal(calls.filter((x) => x.url === API + "/v1/session").length, 1, "the session is asked once a minute");
});

test("whoami reports the display name, and null off arcade", async () => {
  setUp(GAME);
  assert.deepEqual(await A.whoami(), { signedIn: true, displayName: "Dan S" });
  setUp("http://localhost:8000/");
  assert.equal(await A.whoami(), null);
  assert.deepEqual(calls, []);
});

test("nothing is sent for a signed-out player", async () => {
  setUp(GAME, { signedIn: false });
  assert.equal(await A.submit("fastest-crossing", 3), null);
  assert.equal(await A.unlock("first-win"), null);
  assert.deepEqual(posts(), []);
});

for (const href of ["http://localhost:8000/", "https://fishe.danielstephenson.dev/", "http://frog.play.danielstephenson.dev/",
                    "https://api.play.danielstephenson.dev/", "https://danielstephenson.dev/play",
                    "https://frog.play.danielstephenson.dev.evil.example/"]) {
  test(`nothing is sent from ${href}`, async () => {
    setUp(href);
    assert.equal(await A.submit("fastest-crossing", 3), null);
    assert.equal(await A.unlock("first-win"), null);
    assert.deepEqual(await A.top("fastest-crossing"), []);
    assert.deepEqual(calls, []);
  });
}

test("malformed calls send nothing and never throw", async () => {
  setUp(GAME);
  for (const args of [["Bad Board", 1], ["../admin", 1], ["b-1", "1"], ["b-1", NaN], ["b-1", Infinity], ["b-1", 1, "a b"], [undefined, 1]]) {
    assert.equal(await A.submit(...args), null, JSON.stringify(args));
  }
  assert.equal(await A.unlock("X"), null);
  assert.equal(await A.unlock(), null);
  assert.deepEqual(calls, []);
});

test("a server error or network failure is retried once; a refusal is final", async () => {
  setUp(GAME, { statuses: [503, 503, 503] });
  assert.equal(await A.unlock("first-win"), null);
  await settle();
  assert.equal(posts().length, 2);
  setUp(GAME, { statuses: ["fail", 200] });
  await A.unlock("first-win");
  await settle();
  assert.equal(posts().length, 2);
  setUp(GAME, { statuses: [422] });
  assert.equal(await A.submit("fastest-crossing", -1), null);
  await settle();
  assert.equal(posts().length, 1);
});

test("an unreadable answer, first or on the retry, resolves to null with no unhandled rejection", async () => {
  const unhandled = [];
  const onUnhandled = (e) => unhandled.push(e);
  process.on("unhandledRejection", onUnhandled);
  setUp(GAME, { statuses: ["badjson", 503, "badjson"] });
  assert.equal(await A.unlock("first-win"), null);
  assert.equal(await A.unlock("first-win"), null);
  await settle();
  process.off("unhandledRejection", onUnhandled);
  assert.equal(posts().length, 3);
  assert.deepEqual(unhandled, []);
});

test("an unknown session (service down) still tries the report", async () => {
  setUp(GAME, { signedIn: "fail" });
  await A.unlock("first-win");
  assert.equal(posts().length, 1);
});

test("top reads this game's board, public and capped", async () => {
  setUp(GAME);
  assert.deepEqual(await A.top("fastest-crossing", 500), [{ rank: 1, displayName: "Dan S", value: 9 }]);
  assert.equal(calls[0].url, API + "/v1/boards/frog/fastest-crossing?limit=100");
  assert.equal(calls[0].credentials, undefined);
});

test("signIn navigates the top window to the service with a return", () => {
  setUp(GAME);
  globalThis.top = { location: { assign: (u) => calls.push({ top: u }) } };
  A.signIn();
  assert.deepEqual(calls, [{ top: API + "/signin?return=" + encodeURIComponent(GAME) }]);
  delete globalThis.top;
});
