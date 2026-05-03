"""Unit tests for PeopleFilter → SPARQL query compilation."""

from wikidata_bulk_people._extract import _build_query, _build_query_no_dob
from wikidata_bulk_people._models import PeopleFilter


class TestBuildQuery:
    def test_empty_filter_produces_valid_query(self) -> None:
        q = _build_query(PeopleFilter(), last_qid=None)
        assert "SELECT" in q
        assert "?item" in q
        assert "wdt:P31 wd:Q5" in q  # instance of human

    def test_empty_filter_no_dob_constraint(self) -> None:
        q = _build_query(PeopleFilter(), last_qid=None)
        assert "P569" not in q  # no DOB constraint without born_after/born_before

    def test_born_after_adds_constraint(self) -> None:
        q = _build_query(PeopleFilter(born_after=1900), last_qid=None)
        assert "P569" in q
        assert "1900" in q
        assert ">=" in q

    def test_born_before_adds_constraint(self) -> None:
        q = _build_query(PeopleFilter(born_before=1850), last_qid=None)
        assert "P569" in q
        assert "1850" in q
        assert "<=" in q

    def test_born_after_and_before_combined(self) -> None:
        q = _build_query(PeopleFilter(born_after=1900, born_before=1910), last_qid=None)
        assert "P569" in q
        assert ">= 1900" in q
        assert "<= 1910" in q

    def test_occupation_adds_constraint(self) -> None:
        q = _build_query(PeopleFilter(occupation_qid="Q169470"), last_qid=None)
        assert "Q169470" in q
        assert "P106" in q  # occupation property

    def test_citizenship_adds_constraint(self) -> None:
        q = _build_query(PeopleFilter(citizenship_qid="Q30"), last_qid=None)
        assert "Q30" in q
        assert "P27" in q  # country of citizenship property

    def test_has_wikipedia_article_adds_sitelink(self) -> None:
        q = _build_query(PeopleFilter(has_wikipedia_article=True), last_qid=None)
        assert "enwiki" in q or "sitelinks" in q.lower() or "schema:about" in q

    def test_keyset_cursor_added_when_last_qid(self) -> None:
        q = _build_query(PeopleFilter(), last_qid="Q1000")
        assert "Q1000" in q
        assert "FILTER" in q

    def test_no_cursor_when_last_qid_none(self) -> None:
        q = _build_query(PeopleFilter(), last_qid=None)
        assert "Q1000" not in q

    def test_living_filter_true(self) -> None:
        q = _build_query(PeopleFilter(living=True), last_qid=None)
        assert "P570" in q  # living=True should exclude those with P570

    def test_ordered_default_includes_order_by(self) -> None:
        q = _build_query(PeopleFilter(), last_qid=None)
        assert "ORDER BY ?item" in q

    def test_unordered_omits_order_by(self) -> None:
        q = _build_query(PeopleFilter(ordered=False), last_qid=None)
        assert "ORDER BY" not in q
        # Cursor and LIMIT must still work
        assert "LIMIT" in q

    def test_unordered_keeps_keyset_cursor(self) -> None:
        q = _build_query(PeopleFilter(ordered=False), last_qid="Q1000")
        assert "ORDER BY" not in q
        assert "FILTER(?item > wd:Q1000)" in q

    def test_no_dob_query_default_includes_order_by(self) -> None:
        q = _build_query_no_dob(PeopleFilter(), last_qid=None)
        assert "ORDER BY ?item" in q
        assert "FILTER NOT EXISTS { ?item wdt:P569 [] }" in q

    def test_no_dob_query_unordered_omits_order_by(self) -> None:
        q = _build_query_no_dob(PeopleFilter(ordered=False), last_qid="Q500")
        assert "ORDER BY" not in q
        assert "FILTER(?item > wd:Q500)" in q
        assert "FILTER NOT EXISTS { ?item wdt:P569 [] }" in q
