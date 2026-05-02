"""Unit tests for spouse is_former classification logic in _util.py."""

from wikidata_bulk_people._util import _classify_spouse_claim


def _spouse_claim(
    spouse_qid: str = "Q123",
    *,
    has_end_time: bool = False,
    has_end_cause: bool = False,
    has_start_time: bool = False,
) -> dict:
    """Build a synthetic P26 (spouse) claim dict."""
    qualifiers: dict = {}
    if has_end_time:
        qualifiers["P582"] = [
            {
                "snaktype": "value",
                "datatype": "time",
                "datavalue": {
                    "type": "time",
                    "value": {
                        "time": "+1955-04-18T00:00:00Z",
                        "precision": 11,
                        "calendarmodel": "http://www.wikidata.org/entity/Q1985727",
                    },
                },
            }
        ]
    if has_end_cause:
        qualifiers["P1534"] = [
            {
                "snaktype": "value",
                "datatype": "wikibase-item",
                "datavalue": {
                    "type": "wikibase-entityid",
                    "value": {"entity-type": "item", "id": "Q49228"},
                },
            }
        ]
    if has_start_time:
        qualifiers["P580"] = [
            {
                "snaktype": "value",
                "datatype": "time",
                "datavalue": {
                    "type": "time",
                    "value": {
                        "time": "+1903-01-01T00:00:00Z",
                        "precision": 9,
                        "calendarmodel": "http://www.wikidata.org/entity/Q1985727",
                    },
                },
            }
        ]

    return {
        "mainsnak": {
            "snaktype": "value",
            "datatype": "wikibase-item",
            "datavalue": {
                "type": "wikibase-entityid",
                "value": {"entity-type": "item", "id": spouse_qid},
            },
        },
        "qualifiers": qualifiers,
        "type": "statement",
        "rank": "normal",
    }


class TestClassifySpouseClaim:
    def test_no_qualifiers_is_not_former(self) -> None:
        result = _classify_spouse_claim(_spouse_claim())
        assert result["is_former"] is False

    def test_end_time_qualifier_is_former(self) -> None:
        result = _classify_spouse_claim(_spouse_claim(has_end_time=True))
        assert result["is_former"] is True

    def test_end_cause_without_end_time_is_former(self) -> None:
        result = _classify_spouse_claim(_spouse_claim(has_end_cause=True))
        assert result["is_former"] is True

    def test_end_time_and_end_cause_both_is_former(self) -> None:
        result = _classify_spouse_claim(_spouse_claim(has_end_time=True, has_end_cause=True))
        assert result["is_former"] is True

    def test_start_time_only_is_not_former(self) -> None:
        result = _classify_spouse_claim(_spouse_claim(has_start_time=True))
        assert result["is_former"] is False

    def test_qid_extracted(self) -> None:
        result = _classify_spouse_claim(_spouse_claim(spouse_qid="Q456"))
        assert result["qid"] == "Q456"

    def test_end_date_parsed_when_end_time_present(self) -> None:
        result = _classify_spouse_claim(_spouse_claim(has_end_time=True))
        assert result["end_date"] is not None
        assert result["end_date"].year == 1955

    def test_start_date_parsed_when_start_time_present(self) -> None:
        result = _classify_spouse_claim(_spouse_claim(has_start_time=True))
        assert result["start_date"] is not None
        assert result["start_date"].year == 1903

    def test_end_date_none_when_no_end_time(self) -> None:
        result = _classify_spouse_claim(_spouse_claim())
        assert result["end_date"] is None
