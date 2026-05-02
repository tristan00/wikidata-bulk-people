"""Unit tests for low-level Wikidata claim parsing helpers in _util.py."""

from wikidata_bulk_people._util import (
    _claim_values,
    _datavalue,
    _entity_qid_from_snak,
    _label,
    _parse_time,
)

# ---------------------------------------------------------------------------
# Fixtures — inline dicts that mirror Wikidata API response shapes
# ---------------------------------------------------------------------------


def _time_snak(time_str: str, precision: int = 11, calendar: str = "gregorian") -> dict:
    calendar_map = {"gregorian": "Q1985727", "julian": "Q1985786"}
    return {
        "datatype": "time",
        "datavalue": {
            "type": "time",
            "value": {
                "time": time_str,
                "precision": precision,
                "calendarmodel": f"http://www.wikidata.org/entity/{calendar_map[calendar]}",
            },
        },
    }


def _entity_snak(qid: str) -> dict:
    return {
        "datatype": "wikibase-item",
        "datavalue": {
            "type": "wikibase-entityid",
            "value": {"entity-type": "item", "id": qid},
        },
    }


def _novalue_snak() -> dict:
    return {"snaktype": "novalue"}


def _somevalue_snak() -> dict:
    return {"snaktype": "somevalue"}


def _entity_with_claims(pid: str, snaks: list[dict]) -> dict:
    return {
        "claims": {
            pid: [{"mainsnak": snak, "type": "statement", "rank": "normal"} for snak in snaks]
        }
    }


# ---------------------------------------------------------------------------
# _parse_time
# ---------------------------------------------------------------------------


class TestParseTime:
    def test_day_precision(self) -> None:
        snak = _time_snak("+1879-03-14T00:00:00Z", precision=11)
        dv = _parse_time(snak["datavalue"]["value"])
        assert dv is not None
        assert dv.year == 1879
        assert dv.month == 3
        assert dv.day == 14
        assert dv.precision == 11
        assert dv.calendar == "gregorian"

    def test_month_precision(self) -> None:
        snak = _time_snak("+1879-03-00T00:00:00Z", precision=10)
        dv = _parse_time(snak["datavalue"]["value"])
        assert dv is not None
        assert dv.year == 1879
        assert dv.month == 3
        assert dv.day is None
        assert dv.precision == 10

    def test_year_precision(self) -> None:
        snak = _time_snak("+1879-00-00T00:00:00Z", precision=9)
        dv = _parse_time(snak["datavalue"]["value"])
        assert dv is not None
        assert dv.year == 1879
        assert dv.month is None
        assert dv.day is None
        assert dv.precision == 9

    def test_bce_year(self) -> None:
        snak = _time_snak("-0044-03-15T00:00:00Z", precision=11)
        dv = _parse_time(snak["datavalue"]["value"])
        assert dv is not None
        assert dv.year == -44

    def test_julian_calendar(self) -> None:
        snak = _time_snak("+1600-01-01T00:00:00Z", precision=11, calendar="julian")
        dv = _parse_time(snak["datavalue"]["value"])
        assert dv is not None
        assert dv.calendar == "julian"

    def test_novalue_returns_none(self) -> None:
        assert _parse_time(None) is None

    def test_missing_time_key_returns_none(self) -> None:
        assert _parse_time({}) is None


# ---------------------------------------------------------------------------
# _datavalue
# ---------------------------------------------------------------------------


class TestDatavalue:
    def test_extracts_time_value(self) -> None:
        claim = {
            "mainsnak": _time_snak("+1879-03-14T00:00:00Z"),
        }
        dv = _datavalue(claim)
        assert dv is not None
        assert dv["time"].startswith("+1879")

    def test_extracts_entity_value(self) -> None:
        claim = {"mainsnak": _entity_snak("Q5")}
        dv = _datavalue(claim)
        assert dv is not None
        assert dv["id"] == "Q5"

    def test_novalue_returns_none(self) -> None:
        claim = {"mainsnak": _novalue_snak()}
        assert _datavalue(claim) is None

    def test_somevalue_returns_none(self) -> None:
        claim = {"mainsnak": _somevalue_snak()}
        assert _datavalue(claim) is None

    def test_missing_mainsnak_returns_none(self) -> None:
        assert _datavalue({}) is None


# ---------------------------------------------------------------------------
# _label
# ---------------------------------------------------------------------------


class TestLabel:
    def test_english_label(self) -> None:
        entity = {"labels": {"en": {"language": "en", "value": "Albert Einstein"}}}
        assert _label(entity) == "Albert Einstein"

    def test_missing_language_returns_none(self) -> None:
        entity = {"labels": {"fr": {"language": "fr", "value": "Albert Einstein"}}}
        assert _label(entity, lang="en") is None

    def test_empty_labels_returns_none(self) -> None:
        assert _label({}) is None

    def test_custom_lang(self) -> None:
        entity = {"labels": {"de": {"language": "de", "value": "Albert Einstein"}}}
        assert _label(entity, lang="de") == "Albert Einstein"


# ---------------------------------------------------------------------------
# _claim_values
# ---------------------------------------------------------------------------


class TestClaimValues:
    def test_returns_list_of_datavalues(self) -> None:
        entity = _entity_with_claims("P569", [_time_snak("+1879-03-14T00:00:00Z")])
        vals = _claim_values(entity, "P569")
        assert len(vals) == 1
        assert vals[0]["time"].startswith("+1879")

    def test_skips_novalue(self) -> None:
        entity = _entity_with_claims("P569", [_novalue_snak(), _time_snak("+1879-03-14T00:00:00Z")])
        vals = _claim_values(entity, "P569")
        assert len(vals) == 1

    def test_missing_property_returns_empty(self) -> None:
        assert _claim_values({}, "P569") == []

    def test_multiple_values(self) -> None:
        entity = _entity_with_claims("P21", [_entity_snak("Q6581097"), _entity_snak("Q1234")])
        vals = _claim_values(entity, "P21")
        assert len(vals) == 2


# ---------------------------------------------------------------------------
# _entity_qid_from_snak
# ---------------------------------------------------------------------------


class TestEntityQidFromSnak:
    def test_extracts_qid(self) -> None:
        snak = _entity_snak("Q937")
        assert _entity_qid_from_snak(snak) == "Q937"

    def test_novalue_returns_none(self) -> None:
        assert _entity_qid_from_snak(_novalue_snak()) is None

    def test_time_snak_returns_none(self) -> None:
        assert _entity_qid_from_snak(_time_snak("+1879-03-14T00:00:00Z")) is None

    def test_missing_snak_returns_none(self) -> None:
        assert _entity_qid_from_snak({}) is None
