# @author Daniel McCoy Stephenson
"""The service's own HTML pages: sign in, register, choose a display name,
account. They are the only pages a password is ever typed into (RFC 0013 §1:
"never in a game's page"), so they load nothing from anywhere: no script at
all, inline CSS only, and a Content-Security-Policy that says so."""

from html import escape

_STYLE = """
:root { --bg: #f7f7f5; --fg: #1b1b1b; --muted: #5d5d5d; --card: #ffffff; --line: #d6d6d0;
        --accent: #2f5f8a; --accent-fg: #ffffff; --bad: #a3271f; --good: #26683a; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #151618; --fg: #ececec; --muted: #a5a5a5; --card: #1f2023; --line: #3a3b3f;
          --accent: #7fb0dc; --accent-fg: #10161c; --bad: #ff8a80; --good: #8fd6a0; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
       font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 26rem; margin: 0 auto; padding: 2rem 16px 3rem; }
h1 { font-size: 1.4rem; margin: 0 0 1rem; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 1.25rem; }
label { display: block; font-weight: 600; margin: .9rem 0 .3rem; }
input[type=text], input[type=password] { width: 100%; font: inherit; padding: .7rem .75rem; min-height: 44px;
       border: 1px solid var(--line); border-radius: 8px; background: var(--bg); color: var(--fg); }
button { font: inherit; font-weight: 600; min-height: 44px; padding: .6rem 1.1rem; margin-top: 1.1rem;
         border: 0; border-radius: 8px; background: var(--accent); color: var(--accent-fg); cursor: pointer; }
button.quiet { background: transparent; color: var(--accent); border: 1px solid var(--line); }
button.danger { background: var(--bad); color: var(--card); }
.error { color: var(--bad); font-weight: 600; }
.ok { color: var(--good); font-weight: 600; }
.muted, .hint { color: var(--muted); font-size: .92rem; }
a { color: var(--accent); }
hr { border: 0; border-top: 1px solid var(--line); margin: 1.5rem 0; }
"""


def csp(formTargets):
    """No scripts, no external anything; forms may post only here, and the
    redirect after a form (to the game the player came from) is allowed."""
    return "; ".join(
        (
            "default-src 'none'",
            "style-src 'unsafe-inline'",
            "img-src 'self'",
            "form-action 'self' " + " ".join(formTargets),
            "frame-ancestors 'none'",
            "base-uri 'none'",
        )
    )


def _e(value):
    return escape("" if value is None else str(value), quote=True)


def page(title, body):
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"referrer\" content=\"same-origin\">"
        "<title>%s · Play</title><style>%s</style></head><body><main>%s</main></body></html>\n"
        % (_e(title), _STYLE, body)
    ).encode("utf-8")


def _hidden(csrf, returnUrl):
    return '<input type="hidden" name="csrf" value="%s"><input type="hidden" name="return" value="%s">' % (
        _e(csrf),
        _e(returnUrl),
    )


def _message(error=None, notice=None):
    parts = []
    if error:
        parts.append('<p class="error" role="alert">%s</p>' % _e(error))
    if notice:
        parts.append('<p class="ok" role="status">%s</p>' % _e(notice))
    return "".join(parts)


def signIn(csrf, returnUrl, registerUrl, error=None, notice=None, username=""):
    return page(
        "Sign in",
        """<h1>Sign in to play</h1>%s
<form class="card" method="post" action="/signin">%s
<label for="username">Username</label>
<input id="username" name="username" type="text" autocomplete="username" autocapitalize="none"
       spellcheck="false" maxlength="50" required value="%s">
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button>
</form>
<p class="hint">Signing in puts your scores on the leaderboards and lets you like games. Your username
is never shown to anyone; leaderboards show a display name you choose.</p>
<p>No account? <a href="%s">Create one</a>.</p>
<p class="hint"><a href="%s">Back without signing in</a></p>"""
        % (_message(error, notice), _hidden(csrf, returnUrl), _e(username), _e(registerUrl), _e(returnUrl)),
    )


def register(csrf, returnUrl, signInUrl, error=None, username=""):
    return page(
        "Create an account",
        """<h1>Create an account</h1>%s
<form class="card" method="post" action="/register">%s
<label for="username">Username</label>
<input id="username" name="username" type="text" autocomplete="username" autocapitalize="none"
       spellcheck="false" minlength="3" maxlength="50" required value="%s">
<p class="hint">3 to 50 characters. Private: only you see it.</p>
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="new-password" minlength="8"
       maxlength="72" required>
<p class="hint">8 to 72 characters, with a lowercase letter, an uppercase letter, a digit and a symbol.</p>
<label for="password2">Password again</label>
<input id="password2" name="password2" type="password" autocomplete="new-password" maxlength="72" required>
<button type="submit">Create account</button>
</form>
<p class="hint">This is a shared Preponderous/Stephenson Software account (UserAuth): the same account
signs in to other sites that use it. Password resets are done by the site owner by hand.</p>
<p>Already have an account? <a href="%s">Sign in</a>.</p>"""
        % (_message(error), _hidden(csrf, returnUrl), _e(username), _e(signInUrl)),
    )


def chooseName(csrf, returnUrl, error=None, value="", current=None, nextChange=None):
    heading = "Change your display name" if current else "Choose a display name"
    intro = (
        "<p>Leaderboards show this name, not your username.</p>"
        if not current
        else "<p>You are <strong>%s</strong> on the leaderboards.</p>" % _e(current)
    )
    note = "<p class=\"hint\">You can change it again once every 30 days.</p>"
    if nextChange:
        note = '<p class="hint">You can next change it on %s.</p>' % _e(nextChange)
    return page(
        heading,
        """<h1>%s</h1>%s%s
<form class="card" method="post" action="%s">%s
<label for="name">Display name</label>
<input id="name" name="name" type="text" autocomplete="nickname" minlength="3" maxlength="20" required
       value="%s">
<p class="hint">3 to 20 letters and digits; single spaces, _ - or . between them.</p>
<button type="submit">Save</button>
</form>%s"""
        % (
            heading,
            intro,
            _message(error),
            "/account/name" if current else "/welcome",
            _hidden(csrf, returnUrl),
            _e(value),
            note,
        ),
    )


def account(csrf, username, displayName, summary, returnUrl, error=None, notice=None, nextChange=None):
    name = _e(displayName) if displayName else "<em>not chosen yet</em>"
    change = (
        '<p class="hint">Next change possible on %s.</p>' % _e(nextChange)
        if nextChange
        else '<p><a href="/account/name">Change display name</a></p>'
    )
    return page(
        "Your account",
        """<h1>Your account</h1>%s
<div class="card">
<p>Signed in as <strong>%s</strong> (private).</p>
<p>Display name: <strong>%s</strong></p>%s
<p class="muted">Held here: %d best score(s), %d achievement(s), %d like(s).
<a href="/account/export">Download it as JSON</a>.</p>
<p><a href="/account/saves">Cloud saves</a></p>
<form method="post" action="/signout">%s<button class="quiet" type="submit">Sign out</button></form>
</div>
<hr>
<form class="card" method="post" action="/account/delete">%s
<h2 style="font-size:1.1rem;margin:0">Delete my data</h2>
<p class="muted">Deletes every score, achievement, like, cloud save and your display name from this service, now.
It does not delete your UserAuth account itself (UserAuth has no way to do that yet; ask the site owner).
Backups age out on their own schedule.</p>
<label for="confirm">Type <strong>delete</strong> to confirm</label>
<input id="confirm" name="confirm" type="text" autocomplete="off" required>
<button class="danger" type="submit">Delete my data</button>
</form>
<p class="hint"><a href="%s">Back to the games</a></p>"""
        % (
            _message(error, notice),
            _e(username),
            name,
            change,
            summary["scores"],
            summary["unlocks"],
            summary["likes"],
            _hidden(csrf, returnUrl),
            _hidden(csrf, returnUrl),
            _e(returnUrl),
        ),
    )


def _size(count):
    if count < 1024:
        return "%d bytes" % count
    if count < 1024 * 1024:
        return "%.1f KB" % (count / 1024.0)
    return "%.1f MB" % (count / (1024.0 * 1024.0))


def saves(csrf, games, available, error=None, notice=None):
    """The player's cloud saves, per game: every version, each one a download
    (a save file the game's "Load saves from a file" accepts), and a typed
    delete. games: [{slug, title, store, enrolled, storedBytes, versions: [...]}]."""
    if not available:
        body = '<p class="muted">Cloud saves are unavailable right now. Your saves in each browser are not affected.</p>'
    elif not games:
        body = (
            '<p class="muted">No game backs up saves to this account yet. Turn it on from a game\'s '
            "<strong>Saves</strong> button.</p>"
        )
    else:
        sections = []
        for game in games:
            rows = []
            for version in game["versions"]:
                href = "/account/saves/download?%s" % _e(
                    "slug=%s&store=%s&id=%d" % (game["slug"], game["store"], version["id"])
                )
                rows.append(
                    "<li>%s &middot; %s &middot; %d save(s), %s%s &middot; <a href=\"%s\">Download</a></li>"
                    % (
                        _e(version["createdAt"].replace("T", " ")[:16] + " UTC"),
                        _e(version["deviceLabel"]),
                        version["unitCount"],
                        _e(_size(version["totalSize"])),
                        " &middot; kept twice (merged)" if version["kind"] == "merge" else "",
                        href,
                    )
                )
            more = (
                '<p class="hint">Showing the newest %d of %d versions.</p>' % (len(game["versions"]), game["count"])
                if game["count"] > len(game["versions"])
                else ""
            )
            sections.append(
                """<div class="card" style="margin-bottom:1rem">
<h2 style="font-size:1.1rem;margin:0">%s</h2>
<p class="muted">%s &middot; %s stored. Each version below is a file the game's
<strong>Saves &rarr; Load saves from a file</strong> accepts.</p>
<ul>%s</ul>%s
<form method="post" action="/account/saves/delete">%s
<input type="hidden" name="slug" value="%s"><input type="hidden" name="store" value="%s">
<label for="confirm-%s">Type <strong>delete</strong> to delete every cloud version of this game</label>
<input id="confirm-%s" name="confirm" type="text" autocomplete="off" required>
<p class="hint">Download what you want to keep first. The saves in your browsers are not touched.</p>
<button class="danger" type="submit">Delete this game's cloud saves</button>
</form></div>"""
                % (
                    _e(game["title"]),
                    "Backing up" if game["enrolled"] else "Not backing up",
                    _e(_size(game["storedBytes"])),
                    "".join(rows) or "<li>No versions yet.</li>",
                    more,
                    '<input type="hidden" name="csrf" value="%s">' % _e(csrf),
                    _e(game["slug"]),
                    _e(game["store"]),
                    _e(game["slug"]),
                    _e(game["slug"]),
                )
            )
        body = "".join(sections)
    return page(
        "Cloud saves",
        """<h1>Cloud saves</h1>%s%s
<p class="hint"><a href="/account">Back to your account</a></p>"""
        % (_message(error, notice), body),
    )


def message(title, text, linkUrl=None, linkText=None):
    link = '<p><a href="%s">%s</a></p>' % (_e(linkUrl), _e(linkText)) if linkUrl else ""
    return page(title, "<h1>%s</h1><p>%s</p>%s" % (_e(title), _e(text), link))
