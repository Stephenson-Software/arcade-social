# @author Daniel McCoy Stephenson
import pytest

from arcade_social import displaynames


@pytest.mark.parametrize("name", ["Abc", "Dan S", "dan_s", "A.B-C 9", "x" * 20, "  Padded  ", "Jo3"])
def test_allowed(name):
    assert displaynames.check(name) == name.strip()


@pytest.mark.parametrize(
    "name",
    [
        "ab", "x" * 21, "", None, 5, " a ", "-abc", "abc-", "a  b", "a__b", "a._b", "Zoë", "Ｆullwidth",
        "a​bc", "a<b>c", "tab\tname", "new\nline", ".-.",
    ],
)
def test_refused_shapes(name):
    with pytest.raises(displaynames.InvalidName):
        displaynames.check(name)


def test_keys_fold_separators_case_and_confusables():
    key = displaynames.nameKey
    assert key("Dan_S") == key("dan s") == key("DANS") == key("D.a-n S")
    assert key("Dan1el") == key("Daniel") == key("DanIel") == key("Danlel")
    assert key("B0b") == key("Bob")
    assert key("Bob") != key("Rob")


@pytest.mark.parametrize("name", ["Admin", "adm1n", "The_Admin", "Moderator 7", "Official", "Daniel Stephenson", "mod", "Root", "Staff", "Arcade"])
def test_impersonation_is_refused_except_for_operators(name):
    with pytest.raises(displaynames.InvalidName):
        displaynames.check(name)
    assert displaynames.check(name, operator=True) == name


def test_ordinary_words_containing_short_reserved_words_pass():
    for name in ("Modest Mouse", "Staffordshire", "Rooted", "Systematic", "Owners Club"):
        assert displaynames.check(name) == name


def test_profanity_is_refused_for_everyone():
    for name in ("fuckface", "Sh1thead", "F.u.c.k"):
        with pytest.raises(displaynames.InvalidName):
            displaynames.check(name)
        with pytest.raises(displaynames.InvalidName):
            displaynames.check(name, operator=True)
