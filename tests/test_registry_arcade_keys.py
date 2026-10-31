from arcade_social import registry

ENTRY = (
    "games:\n"
    "  - slug: familiar\n"
    "    title: Familiar\n"
    "    repo: Preponderous-Software/familiar\n"
    '    token_sha256: "%s"\n'
    "    kind: static\n" % ("0" * 64)
)


def test_a_registry_with_arcades_newer_keys_loads():
    # The gateway's games.yaml carries arcade's tags and engine (and may carry
    # canonical and added); this service must load the same file.
    loaded = registry.loads(
        ENTRY
        + "    canonical: https://danielstephenson.dev/play/familiar\n"
        + "    added: 2026-10-24\n"
        + "    tags: [halloween, story]\n"
        + "    engine: strata\n"
    )
    assert loaded.get("familiar").title == "Familiar"
