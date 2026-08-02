"""Tests for the dashboard-questions parser.

The rest of provision_metabase.py talks to a live Metabase instance and is
exercised manually via `make dashboard` — not something to fake convincingly in
a unit test. What *is* testable without one, and worth testing, is the parser:
it is the mechanism that keeps the dashboard from drifting out of sync with
sql/dashboard_questions.sql, so a parsing bug would silently corrupt every card.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.provision_metabase import QUESTIONS_PATH, parse_questions


def write_sql(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "dashboard_questions.sql"
    path.write_text(text)
    return path


def test_parses_the_real_committed_file() -> None:
    """The file this repo actually ships must parse into exactly 8 cards."""
    questions = parse_questions(QUESTIONS_PATH)

    assert len(questions) == 8
    assert set(questions) == set(range(1, 9))
    for number, (title, sql) in questions.items():
        assert title.strip(), f"card {number} has an empty title"
        assert sql.strip().upper().startswith("SELECT") or sql.strip().upper().startswith(
            "WITH"
        ), f"card {number} does not start with a query"


def test_card_titles_match_the_dashboard_notes() -> None:
    """metabase_notes.md documents each card by title; catch a rename that
    forgets to update the docs, or vice versa."""
    questions = parse_questions(QUESTIONS_PATH)
    notes = (QUESTIONS_PATH.parent.parent / "dashboards" / "metabase_notes.md").read_text()

    # A loose check: the first few words of each title should appear in the
    # notes table, not an exact string match against markdown formatting.
    for _, (title, _) in questions.items():
        headline = title.split("—")[0].strip()
        first_words = " ".join(headline.split()[:3])
        assert first_words.lower() in notes.lower(), f"{title!r} not referenced in dashboard notes"


def test_two_cards_parse_from_a_minimal_file(tmp_path: Path) -> None:
    text = """\
-- CARD 1 — First card
SELECT 1;

-- CARD 2 — Second card
SELECT 2;
"""
    questions = parse_questions(write_sql(tmp_path, text))

    assert questions[1] == ("First card", "SELECT 1;")
    assert questions[2] == ("Second card", "SELECT 2;")


def test_comment_lines_within_a_card_are_stripped_but_sql_is_kept(tmp_path: Path) -> None:
    text = """\
-- CARD 1 — Explained card
-- This comment explains the card and must not appear in the query sent to
-- Metabase, since it is not executable SQL.
SELECT user_id
FROM users
WHERE city_tier = 'tier_1';
"""
    _, sql = parse_questions(write_sql(tmp_path, text))[1]

    assert "must not appear" not in sql
    assert "SELECT user_id" in sql
    assert "WHERE city_tier = 'tier_1';" in sql


def test_a_multiline_query_is_captured_whole(tmp_path: Path) -> None:
    text = """\
-- CARD 1 — Multiline card
WITH totals AS (
    SELECT user_id, SUM(amount) AS total
    FROM transactions
    GROUP BY user_id
)
SELECT * FROM totals;
"""
    _, sql = parse_questions(write_sql(tmp_path, text))[1]

    assert "WITH totals AS" in sql
    assert "GROUP BY user_id" in sql
    assert sql.strip().endswith(";")


def test_no_card_headers_raises_with_a_useful_message(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no '-- CARD"):
        parse_questions(write_sql(tmp_path, "SELECT 1;\n"))


def test_card_numbers_need_not_be_contiguous_or_ordered(tmp_path: Path) -> None:
    """The parser must not assume the file lists cards 1..N in order — it should
    key strictly off the number in each header."""
    text = """\
-- CARD 5 — Out of order
SELECT 5;

-- CARD 1 — Comes first in the file
SELECT 1;
"""
    questions = parse_questions(write_sql(tmp_path, text))

    assert questions[5] == ("Out of order", "SELECT 5;")
    assert questions[1] == ("Comes first in the file", "SELECT 1;")


def test_a_card_with_only_comments_and_no_sql_is_dropped(tmp_path: Path) -> None:
    """Guards against an empty card silently becoming an empty Metabase query."""
    text = """\
-- CARD 1 — Only commentary, no query
-- someone started writing this card and never finished it

-- CARD 2 — A real card
SELECT 1;
"""
    questions = parse_questions(write_sql(tmp_path, text))

    assert 1 not in questions
    assert 2 in questions
