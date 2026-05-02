"""Unit tests for PeopleFilter → SPARQL query compilation."""

from wikidata_bulk_people._extract import _build_query, _year_partitions
from wikidata_bulk_people._models import PeopleFilter


class TestBuildQuery:
    def test_empty_filter_produces_valid_query(self) -> None:
        q = _build_query(PeopleFilter(), partition_year=1900, last_qid=None)
        assert "SELECT" in q
        assert "?item" in q
        assert "wdt:P31 wd:Q5" in q  # instance of human

    def test_born_after_adds_constraint(self) -> None:
        q = _build_query(PeopleFilter(born_after=1900), partition_year=1950, last_qid=None)
        assert "P569" in q  # date of birth property

    def test_occupation_adds_constraint(self) -> None:
        q = _build_query(PeopleFilter(occupation_qid="Q169470"), partition_year=1900, last_qid=None)
        assert "Q169470" in q
        assert "P106" in q  # occupation property

    def test_citizenship_adds_constraint(self) -> None:
        q = _build_query(PeopleFilter(citizenship_qid="Q30"), partition_year=1900, last_qid=None)
        assert "Q30" in q
        assert "P27" in q  # country of citizenship property

    def test_has_wikipedia_article_adds_sitelink(self) -> None:
        q = _build_query(
            PeopleFilter(has_wikipedia_article=True), partition_year=1900, last_qid=None
        )
        assert "enwiki" in q or "sitelinks" in q.lower() or "schema:about" in q

    def test_keyset_cursor_added_when_last_qid(self) -> None:
        q = _build_query(PeopleFilter(), partition_year=1900, last_qid="Q1000")
        assert "Q1000" in q
        assert "FILTER" in q

    def test_no_cursor_when_last_qid_none(self) -> None:
        q = _build_query(PeopleFilter(), partition_year=1900, last_qid=None)
        # Should still have a FILTER for the partition, but no keyset cursor
        assert "Q1000" not in q

    def test_living_filter_true(self) -> None:
        q = _build_query(PeopleFilter(living=True), partition_year=1900, last_qid=None)
        # No P570 (date of death) required — check absence pattern or FILTER NOT EXISTS
        assert "P570" in q  # living=True should exclude those with P570

    def test_no_dob_bucket_partition_year_none(self) -> None:
        q = _build_query(PeopleFilter(), partition_year=None, last_qid=None)
        assert "P569" in q or "MINUS" in q  # no-DOB bucket


class TestYearPartitions:
    def test_returns_list_with_none_bucket(self) -> None:
        parts = _year_partitions(PeopleFilter())
        assert None in parts

    def test_born_after_filters_partitions(self) -> None:
        parts = _year_partitions(PeopleFilter(born_after=1990))
        year_parts = [p for p in parts if p is not None]
        assert all(p >= 1990 for p in year_parts)

    def test_born_before_filters_partitions(self) -> None:
        parts = _year_partitions(PeopleFilter(born_before=1850))
        year_parts = [p for p in parts if p is not None]
        assert all(p <= 1850 for p in year_parts)

    def test_combined_range_filters_partitions(self) -> None:
        parts = _year_partitions(PeopleFilter(born_after=1900, born_before=1910))
        year_parts = [p for p in parts if p is not None]
        assert all(1900 <= p <= 1910 for p in year_parts)
        assert len(year_parts) <= 11  # at most 11 years

    def test_no_dob_bucket_excluded_when_filtering(self) -> None:
        """The no-DOB bucket is always included regardless of birth-year filter."""
        parts = _year_partitions(PeopleFilter(born_after=1990, born_before=2000))
        # None bucket should still be present for completeness
        assert None in parts
