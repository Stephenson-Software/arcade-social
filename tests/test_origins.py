# @author Daniel McCoy Stephenson
from arcade_social import origins, registry
from helpers import GAMES_YAML

REGISTRY = registry.loads(GAMES_YAML, domain="play.example.test")


def classify(origin, hall=()):
    return origins.classify(
        origin, REGISTRY, "play.example.test", "https://example.test", "https://api.play.example.test", hall
    )


HALL = ("https://play.example.test", "https://arcade.example.test")


def test_games_aliases_portal_and_service():
    assert (classify("https://fishe.play.example.test").kind, classify("https://fishe.play.example.test").slug) == ("game", "fishe")
    alias = classify("https://tidewater.example.test")
    assert (alias.kind, alias.slug) == ("game", "tidewater")
    assert classify("https://example.test").kind == "portal"
    assert classify("https://api.play.example.test").kind == "service"


def test_everything_else_is_refused():
    for origin in (
        None, "", "null", "https://evil.example", "http://fishe.play.example.test", "https://fishe.play.example.test/",
        "https://fishe.play.example.test:443", "https://FISHE.play.example.test", "https://x.fishe.play.example.test",
        "https://api.play.example.test.evil", "https://play.example.test", "https://nosuch.play.example.test",
        "https://example.test:8443", "http://example.test", "https://tidewater.example.test.evil.example",
        "https://fishe.play.example.test@evil.example",
    ):
        assert classify(origin) is None, origin


def test_return_origins_cover_portal_games_and_aliases():
    assert origins.returnOrigins(REGISTRY, "play.example.test", "https://example.test") == [
        "https://example.test", "https://*.play.example.test", "https://tidewater.example.test"]


def test_hall_origins_are_their_own_kind():
    for origin in HALL:
        caller = classify(origin, HALL)
        assert (caller.kind, caller.slug) == ("hall", None), origin
    # the other kinds are unchanged by configuring a hall
    assert classify("https://fishe.play.example.test", HALL).slug == "fishe"
    assert classify("https://example.test", HALL).kind == "portal"
    assert classify("https://api.play.example.test", HALL).kind == "service"


def test_the_bare_game_domain_is_never_a_slug_host():
    # An empty label is no slug, even when a registry game had an empty or
    # domain-named alias: only a configured hall origin makes it callable.
    assert classify("https://play.example.test") is None
    assert classify("https://.play.example.test", HALL) is None
    assert classify("https://..play.example.test", HALL) is None
    for origin in ("https://play.example.test/", "https://play.example.test:443", "http://play.example.test",
                   "https://PLAY.example.test", "https://play.example.test.evil.example", "https://arcade.example.test/hall"):
        assert classify(origin, HALL) is None, origin


def test_return_origins_include_the_hall():
    assert origins.returnOrigins(REGISTRY, "play.example.test", "https://example.test", HALL) == [
        "https://example.test", "https://*.play.example.test", "https://play.example.test",
        "https://arcade.example.test", "https://tidewater.example.test"]
