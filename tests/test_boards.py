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
