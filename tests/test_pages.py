# @author Daniel McCoy Stephenson
"""The service's own HTML pages (pages.py), rendered directly: the CSP they are served
with, what each page shows, and that every caller-supplied value is escaped."""
import pytest

from arcade_social import pages

HOSTILE = '"><script>alert(1)</script>'
HOSTILE_ESCAPED = "&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"


def _text(raw):
    assert isinstance(raw, bytes)
    return raw.decode("utf-8")


def _summary(scores=0, unlocks=0, likes=0):
    return {"scores": scores, "unlocks": unlocks, "likes": likes}


def _version(**overrides):
    version = {
        "id": 7,
        "createdAt": "2026-11-12T09:30:45.123Z",
        "deviceLabel": "Laptop",
        "unitCount": 3,
        "totalSize": 2048,
        "kind": "upload",
    }
    version.update(overrides)
    return version


def _game(**overrides):
    game = {
        "slug": "tidewater",
        "title": "Tidewater",
        "store": "main",
        "enrolled": True,
        "storedBytes": 2048,
        "versions": [_version()],
        "count": 1,
    }
    game.update(overrides)
    return game


def test_csp_allows_no_script_and_posts_only_to_the_listed_targets():
    policy = pages.csp(["https://a.example.test", "https://b.example.test"])
    assert policy == (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
        "form-action 'self' https://a.example.test https://b.example.test; "
        "frame-ancestors 'none'; base-uri 'none'"
    )
    assert "script-src" not in policy


def test_csp_with_no_targets_allows_only_self():
    assert "form-action 'self' ;" in pages.csp([])


def test_page_is_utf8_bytes_with_an_escaped_title_and_no_script():
    html = _text(pages.page("Ünïcode " + HOSTILE, "<p>body</p>"))
    assert html.startswith("<!doctype html>\n")
    assert "<title>Ünïcode " + HOSTILE_ESCAPED + " · Play</title>" in html
    assert '<meta name="referrer" content="same-origin">' in html
    assert "<main><p>body</p></main>" in html
    assert "<script" not in html


def test_sign_in_escapes_every_caller_value():
    html = _text(pages.signIn(HOSTILE, HOSTILE, HOSTILE, error=HOSTILE, notice=HOSTILE, username=HOSTILE))
    assert "<script" not in html
    assert html.count(HOSTILE_ESCAPED) == 7  # csrf, return, username, register link, back link, error, notice


def test_sign_in_shows_error_and_notice_only_when_given():
    plain = _text(pages.signIn("tok", "/back", "/register?return=x"))
    assert 'role="alert"' not in plain and 'role="status"' not in plain
    assert 'action="/signin"' in plain
    assert (
        '<input type="hidden" name="csrf" value="tok"><input type="hidden" name="return" value="/back">'
    ) in plain
    assert '<a href="/register?return=x">Create one</a>' in plain
    assert '<a href="/back">Back without signing in</a>' in plain
    both = _text(pages.signIn("tok", "/back", "/r", error="Wrong password", notice="Account created"))
    assert (
        '<p class="error" role="alert">Wrong password</p><p class="ok" role="status">Account created</p>'
    ) in both


def test_register_posts_to_register_and_escapes_values():
    html = _text(pages.register("tok", HOSTILE, "/signin?x=1&y=2", error="Too short", username=HOSTILE))
    assert 'action="/register"' in html
    assert '<a href="/signin?x=1&amp;y=2">Sign in</a>' in html
    assert '<p class="error" role="alert">Too short</p>' in html
    assert "<script" not in html


def test_choose_name_first_time_posts_to_welcome():
    html = _text(pages.chooseName("tok", "/back", value="Dan S"))
    assert "<h1>Choose a display name</h1>" in html
    assert 'action="/welcome"' in html
    assert "Leaderboards show this name, not your username." in html
    assert "You can change it again once every 30 days." in html
    assert 'value="Dan S"' in html


def test_choose_name_change_posts_to_account_name_and_shows_next_change():
    html = _text(pages.chooseName("tok", "/back", current=HOSTILE, nextChange="2026-12-12", value=HOSTILE))
    assert "<h1>Change your display name</h1>" in html
    assert 'action="/account/name"' in html
    assert "You are <strong>%s</strong> on the leaderboards." % HOSTILE_ESCAPED in html
    assert "You can next change it on 2026-12-12." in html
    assert "once every 30 days" not in html
    assert "<script" not in html


def test_account_without_display_name_offers_change_link_and_counts():
    html = _text(pages.account("tok", "dan", None, _summary(4, 2, 1), "/back"))
    assert "Display name: <strong><em>not chosen yet</em></strong>" in html
    assert '<a href="/account/name">Change display name</a>' in html
    assert "Held here: 4 best score(s), 2 achievement(s), 1 like(s)." in html
    assert '<a href="/back">Back to the games</a>' in html


def test_account_hides_change_link_until_next_change():
    html = _text(pages.account("tok", "dan", "Dan S", _summary(), "/back", nextChange="2026-12-12"))
    assert "Display name: <strong>Dan S</strong>" in html
    assert "Next change possible on 2026-12-12." in html
    assert 'href="/account/name"' not in html


@pytest.mark.parametrize(
    "shown, state, nextValue, button",
    [
        (False, "Off: nobody sees you in the hall.", "on", "Show me in the hall"),
        (True, "On: you appear in the hall.", "off", "Stop showing me"),
    ],
)
def test_account_presence_form_toggles(shown, state, nextValue, button):
    html = _text(pages.account("tok", "dan", "Dan S", _summary(), "/back", hallPresence=shown))
    assert '<p class="muted">%s</p>' % state in html
    assert '<input type="hidden" name="show" value="%s">' % nextValue in html
    assert '<button type="submit">%s</button>' % button in html
    assert "switched off for now" not in html


def test_account_notes_presence_switched_off_by_operator():
    html = _text(pages.account("tok", "dan", "Dan S", _summary(), "/back", presenceDisabled=True))
    assert "Presence in the hall is switched off for now" in html


def test_account_escapes_every_caller_value():
    html = _text(
        pages.account(
            HOSTILE, HOSTILE, HOSTILE, _summary(), HOSTILE, error=HOSTILE, notice=HOSTILE, nextChange=HOSTILE
        )
    )
    assert "<script" not in html
    # username, display name, next change, error, notice, back link, and csrf + return in three forms
    assert html.count(HOSTILE_ESCAPED) == 12


@pytest.mark.parametrize(
    "count, shown",
    [
        (0, "0 bytes"),
        (1023, "1023 bytes"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1024 * 1024 - 1, "1024.0 KB"),
        (1024 * 1024, "1.0 MB"),
        (5 * 1024 * 1024 + 512 * 1024, "5.5 MB"),
    ],
)
def test_sizes_are_shown_in_bytes_kb_or_mb(count, shown):
    assert pages._size(count) == shown


def test_saves_unavailable():
    html = _text(pages.saves("tok", [_game()], False))
    assert "Cloud saves are unavailable right now." in html
    assert "Tidewater" not in html


def test_saves_with_no_games():
    html = _text(pages.saves("tok", [], True))
    assert "No game backs up saves to this account yet." in html


def test_saves_lists_each_version_with_an_escaped_download_link():
    html = _text(pages.saves("tok", [_game()], True, notice="Deleted"))
    assert "<h2 style=\"font-size:1.1rem;margin:0\">Tidewater</h2>" in html
    assert "Backing up &middot; 2.0 KB stored." in html
    assert (
        "<li>2026-11-12 09:30 UTC &middot; Laptop &middot; 3 save(s), 2.0 KB &middot; "
        '<a href="/account/saves/download?slug=tidewater&amp;store=main&amp;id=7">Download</a></li>'
    ) in html
    assert "kept twice (merged)" not in html
    assert "Showing the newest" not in html
    assert (
        '<input type="hidden" name="slug" value="tidewater"><input type="hidden" name="store" value="main">'
    ) in html
    assert 'id="confirm-tidewater"' in html
    assert '<p class="ok" role="status">Deleted</p>' in html


def test_saves_marks_merges_truncation_and_not_enrolled():
    game = _game(enrolled=False, versions=[_version(kind="merge"), _version(id=6)], count=5)
    html = _text(pages.saves("tok", [game], True))
    assert "Not backing up &middot;" in html
    assert html.count("kept twice (merged)") == 1
    assert "Showing the newest 2 of 5 versions." in html


def test_saves_game_without_versions():
    html = _text(pages.saves("tok", [_game(versions=[], count=0)], True))
    assert "<li>No versions yet.</li>" in html


def test_saves_escapes_game_and_version_values():
    game = _game(slug=HOSTILE, title=HOSTILE, store=HOSTILE, versions=[_version(deviceLabel=HOSTILE)])
    html = _text(pages.saves(HOSTILE, [game], True, error=HOSTILE))
    assert "<script" not in html
    assert "<h2 style=\"font-size:1.1rem;margin:0\">%s</h2>" % HOSTILE_ESCAPED in html


def test_message_with_and_without_link():
    plain = _text(pages.message("Signed out", "Bye"))
    assert "<h1>Signed out</h1><p>Bye</p></main>" in plain
    linked = _text(pages.message(HOSTILE, HOSTILE, linkUrl="/a?b=1&c=2", linkText=HOSTILE))
    assert '<p><a href="/a?b=1&amp;c=2">%s</a></p>' % HOSTILE_ESCAPED in linked
    assert "<script" not in linked
