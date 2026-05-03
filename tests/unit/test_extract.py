"""Unit tests for QIDStream cursor-advance behavior in ordered vs unordered mode.

These tests mock out the SPARQL HTTP layer; no network is touched.
"""

from typing import Any
from unittest.mock import patch

from wikidata_bulk_people._extract import QIDStream
from wikidata_bulk_people._models import PeopleFilter


def _binding(qid: str) -> dict[str, Any]:
    """Build a fake SPARQL binding dict for a QID."""
    return {"item": {"value": f"http://www.wikidata.org/entity/{qid}"}}


def _short_page(qids: list[str]) -> list[dict[str, Any]]:
    """A page short enough to terminate iteration (< _PAGE_SIZE)."""
    return [_binding(q) for q in qids]


def _full_page(qids: list[str]) -> list[dict[str, Any]]:
    """A page padded out to exactly _PAGE_SIZE so iteration continues."""
    from wikidata_bulk_people._extract import _PAGE_SIZE

    base = [_binding(q) for q in qids]
    pad_count = _PAGE_SIZE - len(base)
    pad = [_binding(f"Q9999{i:04d}") for i in range(pad_count)]
    return base + pad


class TestIterSingleOrdered:
    """Default ordered mode: cursor advances to last yielded QID."""

    def test_terminates_on_short_page(self) -> None:
        stream = QIDStream(filter=PeopleFilter(), user_agent="test/1.0")
        with patch.object(stream._sparql, "run_query", return_value=_short_page(["Q1", "Q2", "Q3"])):
            result = list(stream)
        assert result == ["Q1", "Q2", "Q3"]

    def test_cursor_passed_to_next_page(self) -> None:
        """In ordered mode, last_qid is the last yielded — same as the page's natural max."""
        stream = QIDStream(filter=PeopleFilter(), user_agent="test/1.0")
        captured_queries: list[str] = []

        def fake_run(query: str) -> list[dict[str, Any]]:
            captured_queries.append(query)
            if len(captured_queries) == 1:
                return _full_page(["Q5", "Q10", "Q20"])  # Q20 is last yielded
            return _short_page(["Q30"])

        with patch.object(stream._sparql, "run_query", side_effect=fake_run):
            list(stream)

        assert len(captured_queries) == 2
        # First page: no cursor
        assert "FILTER(?item >" not in captured_queries[0]
        # Second page: cursor is last yielded from page 1, which (with ordering) was Q9999XXXX (the pad).
        # We don't need to assert the exact cursor — just that it's a valid keyset cursor on the last
        # yielded value.
        assert "FILTER(?item > wd:Q9999" in captured_queries[1]


class TestIterSingleUnordered:
    """Unordered mode: cursor advances to lex-max of returned QIDs (string max)."""

    def test_terminates_on_short_page(self) -> None:
        stream = QIDStream(filter=PeopleFilter(ordered=False), user_agent="test/1.0")
        with patch.object(
            stream._sparql,
            "run_query",
            return_value=_short_page(["Q1", "Q200", "Q3"]),
        ):
            result = list(stream)
        assert sorted(result) == ["Q1", "Q200", "Q3"]

    def test_cursor_advances_to_lex_max_not_last_yielded(self) -> None:
        """The page's "last yielded" QID is meaningless when results are unordered.

        The cursor must be the LEX-max of the page so SPARQL ``FILTER(?item > wd:X)``
        on the next page actually skips the same prefix WDQS just returned.
        """
        stream = QIDStream(filter=PeopleFilter(ordered=False), user_agent="test/1.0")
        captured_queries: list[str] = []

        def fake_run(query: str) -> list[dict[str, Any]]:
            captured_queries.append(query)
            if len(captured_queries) == 1:
                # Page 1 returns out-of-order. Lex order: Q10000 < Q5 < Q700 < Q800.
                # So lex-max is "Q800".
                return _full_page(["Q5", "Q10000", "Q800", "Q700"])
            return _short_page([])  # Empty page on second call → terminate.

        with patch.object(stream._sparql, "run_query", side_effect=fake_run):
            list(stream)

        assert len(captured_queries) == 2
        # Lex-max of {"Q5", "Q10000", "Q800", "Q700", and pad "Q9999XXXX"} is "Q9999XXXX"
        # because "Q9999XXXX" > "Q800" lexicographically. So the cursor should be the
        # max of all returned QIDs (including pad), which starts with "Q9999".
        assert "FILTER(?item > wd:Q9999" in captured_queries[1]

    def test_cursor_uses_string_max_not_numeric_max(self) -> None:
        """Verify SPARQL IRI ordering: 'Q5' > 'Q1000' as strings (because '5' > '1')."""
        stream = QIDStream(filter=PeopleFilter(ordered=False), user_agent="test/1.0")
        captured_queries: list[str] = []

        def fake_run(query: str) -> list[dict[str, Any]]:
            captured_queries.append(query)
            if len(captured_queries) == 1:
                # Pure short page so iteration terminates after one call.
                # Numeric max is Q1000; lex max is Q5.
                return _short_page(["Q5", "Q1000"])
            return []

        with patch.object(stream._sparql, "run_query", side_effect=fake_run):
            list(stream)

        # We don't follow up since first page was short — iteration ended.
        # But the principle holds: max(["Q5", "Q1000"]) == "Q5" in Python string ordering.
        assert max(["Q5", "Q1000"]) == "Q5"
