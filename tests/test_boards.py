# @author Daniel McCoy Stephenson
"""boards.yaml: the strict parser and its validation (RFC 0014 §1)."""

import os

import pytest

from arcade_social import boards

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(HERE, "..", "examples", "boards.yaml")

BOARD = """games:
  fishe:
    boards:
      - id: most-money
        title: Most money
        order: desc
        min: 0
        max: 100
%s"""


def test_the_example_parses():
    declarations = boards.load(EXAMPLE)
    fishe = declarations.get("fishe")
    board = fishe.board("most-money")
    assert (board.order, board.integer, board.min, board.max, board.maxPerHour, board.unit) == (
        "desc", True, 0, 100000000, 30, "dollars")
    assert [a.id for a in fishe.listAchievements()] == ["first-catch", "reached-goal"]
    frog = declarations.get("frog-hopper").board("fastest-crossing")
    assert frog.min == 1.5 and isinstance(frog.min, float) and frog.max == 3600
    assert declarations.get("frog-hopper").achievement("no-splash").hidden is True
    assert list(declarations) == ["fishe", "frog-hopper"]


def test_defaults_and_better():
    board = boards.loads(BOARD % "").get("fishe").board("most-money")
    assert board.integer is False and board.maxPerHour == 30 and board.unit is None
    assert board.better(2, 1) and not board.better(1, 1)
    asc = boards.loads((BOARD % "").replace("order: desc", "order: asc")).get("fishe").board("most-money")
    assert asc.better(1, 2) and not asc.better(2, 2)


def test_empty_file():
    assert len(boards.loads("games: {}\n")) == 0
    with pytest.raises(boards.BoardsError):
        boards.loads("")
    with pytest.raises(boards.BoardsError):
        boards.loads("games: {}\n  fishe:\n")


@pytest.mark.parametrize(
    "extra, message",
    [
        ("        colour: red\n", "unknown board key"),
        ("        min: 3\n", "given twice"),
        ("        maxPerHour: 0\n", "maxPerHour"),
        ("        maxPerHour: lots\n", "maxPerHour"),
        ("        integer: maybe\n", "true or false"),
        ("        unit: %s\n" % ("x" * 21), "longer than"),
        ("      - id: most-money\n        title: Again\n        order: asc\n        min: 0\n        max: 1\n", "already declared"),
        ("      - id: Bad_Id\n        title: x\n        order: asc\n        min: 0\n        max: 1\n", "must match"),
        ("      - id: other\n        title: x\n        order: sideways\n        min: 0\n        max: 1\n", "asc or desc"),
        ("      - id: other\n        title: x\n        order: asc\n        min: 5\n        max: 1\n", "min greater than max"),
        ("      - id: other\n        title: x\n        order: asc\n        min: 0\n", "missing max"),
        ("      - id: other\n        title: x\n        order: asc\n        min: zero\n        max: 1\n", "must be a number"),
        ("     - id: other\n", "unexpected indentation"),
        ("\t\n", "tabs"),
        ("    achievements:\n      - id: a1\n        title: A\n", "missing description"),
        ("    achievements:\n      - id: a1\n        title: A\n        description: [x]\n", "unsupported value"),
        ("    boards:\n", "given twice"),
        ("    trophies:\n", "expected 'boards:'"),
        ("  fishe:\n    boards:\n", "declared twice"),
        ("  api:\n    boards:\n", "not a valid arcade slug"),
        ("  other:\n", "no boards and no achievements"),
    ],
)
def test_invalid_files_name_the_problem(extra, message):
    with pytest.raises(boards.BoardsError) as error:
        boards.loads(BOARD % extra)
    assert message in str(error.value)


def test_integer_boards_need_whole_limits():
    text = (BOARD % "").replace("max: 100", "max: 100.5").replace("order: desc", "order: desc\n        integer: true")
    with pytest.raises(boards.BoardsError):
        boards.loads(text)


def test_comments_and_quotes():
    text = (BOARD % "").replace("title: Most money", 'title: "Most money # not a comment"  # a comment')
    assert boards.loads(text).get("fishe").board("most-money").title == "Most money # not a comment"


def test_a_hash_inside_a_word_is_not_a_comment():
    text = (BOARD % "").replace("title: Most money", "title: Most#1 money")
    assert boards.loads(text).get("fishe").board("most-money").title == "Most#1 money"


def test_single_quotes_are_stripped():
    text = (BOARD % "").replace("title: Most money", "title: 'Most money'")
    assert boards.loads(text).get("fishe").board("most-money").title == "Most money"


def test_describe_is_what_the_api_returns():
    text = (BOARD % "        unit: dollars\n        integer: true\n        maxPerHour: 12\n").replace(
        "max: 100", "max: 250"
    )
    board = boards.loads(text).get("fishe").board("most-money")
    assert board.describe() == {
        "id": "most-money",
        "title": "Most money",
        "order": "desc",
        "unit": "dollars",
        "integer": True,
        "min": 0,
        "max": 250,
        "maxPerHour": 12,
    }


def test_boards_and_achievements_keep_the_declared_order():
    extra = (
        "      - id: alpha\n        title: A\n        order: asc\n        min: 0\n        max: 1\n"
        "    achievements:\n"
        "      - id: zulu\n        title: Z\n        description: z\n"
        "      - id: bravo\n        title: B\n        description: b\n"
    )
    game = boards.loads(BOARD % extra).get("fishe")
    assert [board.id for board in game.listBoards()] == ["most-money", "alpha"]
    assert [achievement.id for achievement in game.listAchievements()] == ["zulu", "bravo"]
    assert game.board("missing") is None and game.achievement("missing") is None
    assert boards.loads(BOARD % "").get("other") is None


def test_non_integer_limits_keep_their_type():
    text = (BOARD % "").replace("min: 0", "min: -2.5").replace("max: 100", "max: 1e3")
    board = boards.loads(text).get("fishe").board("most-money")
    assert board.min == -2.5 and isinstance(board.min, float)
    assert board.max == 1000.0 and isinstance(board.max, float)
    assert isinstance(boards.loads(BOARD % "").get("fishe").board("most-money").max, int)


@pytest.mark.parametrize(
    "text, message",
    [
        ("boards:\n", "must start with 'games:'"),
        ("games:\n    boards:\n", "a section outside any game"),
        ("games:\n  fishe:\n      - id: x\n", "an item outside 'boards:' or 'achievements:'"),
        ("games:\n  fishe:\n    boards:\n        id: x\n", "a key outside any '- ' item"),
        ("games:\n  fishe: {}\n", "expected '<slug>:' at two spaces"),
        ("games:\n  fi she:\n", "expected '<slug>:' at two spaces"),
        ("games:\n  fishe:\n    boards:\n      - just words\n", "expected 'key: value'"),
        ("# only a comment\n", "the file is empty"),
    ],
)
def test_misplaced_lines_name_the_problem(text, message):
    with pytest.raises(boards.BoardsError) as error:
        boards.loads(text)
    assert message in str(error.value)


@pytest.mark.parametrize(
    "old, new, message",
    [
        ("title: Most money", "title:", "line 5: a value is required"),
        ("title: Most money", 'title: "Most money', "line 5: unterminated quoted value"),
        ("title: Most money", "title: '", "line 5: unterminated quoted value"),
        ("title: Most money", 'title: "Most "money"', "line 5: escaped quotes are not supported"),
        ("max: 100", "max: 1e999", "line 8: max must be finite"),
        ("max: 100", "max: 1.e5", "line 8: max must be a number"),
        ("max: 100", "max: 007", "line 8: max must be a number"),
        ("title: Most money", "title: %s" % ("x" * 81), "line 5: title is longer than 80 characters"),
    ],
)
def test_invalid_values_name_their_line(old, new, message):
    with pytest.raises(boards.BoardsError) as error:
        boards.loads((BOARD % "").replace(old, new))
    assert message in str(error.value)


def test_integer_boards_refuse_exponents():
    text = (BOARD % "        integer: true\n").replace("max: 100", "max: 1e2")
    with pytest.raises(boards.BoardsError) as error:
        boards.loads(text)
    assert "line 8: max must be a whole number on an integer board" in str(error.value)
