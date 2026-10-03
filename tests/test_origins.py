# @author Daniel McCoy Stephenson
from arcade_social import origins, registry
from helpers import GAMES_YAML

REGISTRY = registry.loads(GAMES_YAML, domain="play.example.test")


def classify(origin):
    return origins.classify(origin, REGISTRY, "play.example.test", "https://example.test", "https://api.play.example.test")


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
