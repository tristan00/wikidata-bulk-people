"""Unit tests for Wikidata QID enums."""

import re

import pytest

from wikidata_bulk_people._models import PeopleFilter
from wikidata_bulk_people.enums import (
    Award,
    Country,
    Gender,
    Occupation,
    PoliticalIdeology,
    Religion,
    _QidEnum,
)

_ALL_ENUM_CLASSES = (Occupation, Country, Gender, Religion, Award, PoliticalIdeology)

_QID_RE = re.compile(r"^Q\d+$")


class TestQidEnumBase:
    """_QidEnum behaviour shared by every subclass."""

    @pytest.mark.parametrize("cls", _ALL_ENUM_CLASSES)
    def test_all_values_are_qids(self, cls: type[_QidEnum]) -> None:
        for member in cls:
            assert _QID_RE.match(member.value), (
                f"{cls.__name__}.{member.name} has non-QID value {member.value!r}"
            )

    @pytest.mark.parametrize("cls", _ALL_ENUM_CLASSES)
    def test_str_returns_qid(self, cls: type[_QidEnum]) -> None:
        """str() and f-strings must produce the QID, not 'ClassName.MEMBER'."""
        for member in cls:
            assert str(member) == member.value
            assert f"{member}" == member.value

    @pytest.mark.parametrize("cls", _ALL_ENUM_CLASSES)
    def test_equals_plain_string(self, cls: type[_QidEnum]) -> None:
        for member in cls:
            assert member == member.value

    @pytest.mark.parametrize("cls", _ALL_ENUM_CLASSES)
    def test_no_duplicate_qids(self, cls: type[_QidEnum]) -> None:
        seen: dict[str, str] = {}
        for member in cls:
            assert member.value not in seen, (
                f"{cls.__name__}: QID {member.value} used by both "
                f"{seen[member.value]} and {member.name}"
            )
            seen[member.value] = member.name

    @pytest.mark.parametrize("cls", _ALL_ENUM_CLASSES)
    def test_names_are_valid_identifiers(self, cls: type[_QidEnum]) -> None:
        for member in cls:
            assert member.name.isidentifier(), (
                f"{cls.__name__}.{member.name} is not a valid Python identifier"
            )
            assert member.name == member.name.upper(), (
                f"{cls.__name__}.{member.name} should be UPPER_CASE"
            )

    @pytest.mark.parametrize("cls", _ALL_ENUM_CLASSES)
    def test_non_empty(self, cls: type[_QidEnum]) -> None:
        assert len(cls) > 0, f"{cls.__name__} has no members"


class TestKnownValues:
    """Spot-check specific members that should always be present."""

    def test_occupation_physicist(self) -> None:
        assert Occupation.PHYSICIST == "Q169470"

    def test_occupation_politician(self) -> None:
        assert Occupation.POLITICIAN == "Q82955"

    def test_occupation_actor(self) -> None:
        assert Occupation.ACTOR == "Q33999"

    def test_country_united_states(self) -> None:
        assert Country.UNITED_STATES == "Q30"

    def test_country_germany(self) -> None:
        assert Country.GERMANY == "Q183"

    def test_country_japan(self) -> None:
        assert Country.JAPAN == "Q17"

    def test_gender_male(self) -> None:
        assert Gender.MALE == "Q6581097"

    def test_gender_female(self) -> None:
        assert Gender.FEMALE == "Q6581072"

    def test_religion_has_major_faiths(self) -> None:
        names = Religion._member_names_
        for faith in ("ISLAM", "BUDDHISM", "HINDUISM", "JUDAISM"):
            assert faith in names, f"Religion.{faith} missing"
        # Christianity is not an instance of Q9174/Q7066 in Wikidata so it doesn't appear
        # as a top-level member, but denominations should be present.
        assert any("CHRISTIANITY" in n for n in names), "No Christianity-related entry in Religion"

    def test_political_ideology_has_core_ideologies(self) -> None:
        names = PoliticalIdeology._member_names_
        for ideology in ("SOCIALISM", "COMMUNISM", "CAPITALISM", "FEMINISM", "NATIONALISM"):
            assert ideology in names, f"PoliticalIdeology.{ideology} missing"

    def test_award_has_nobel_prizes(self) -> None:
        names = Award._member_names_
        # Nobel prizes should appear under some name containing NOBEL
        nobel_members = [n for n in names if "NOBEL" in n]
        assert len(nobel_members) > 0, "No Nobel Prize entries found in Award"


class TestPeopleFilterAcceptsEnums:
    """PeopleFilter should accept enum members and pass QIDs to SPARQL correctly."""

    def test_occupation_enum_in_filter(self) -> None:
        f = PeopleFilter(occupation_qid=Occupation.PHYSICIST)
        assert f.occupation_qid == "Q169470"

    def test_citizenship_enum_in_filter(self) -> None:
        f = PeopleFilter(citizenship_qid=Country.GERMANY)
        assert f.citizenship_qid == "Q183"

    def test_gender_enum_in_filter(self) -> None:
        f = PeopleFilter(gender_qid=Gender.FEMALE)
        assert f.gender_qid == "Q6581072"

    def test_religion_enum_in_filter(self) -> None:
        f = PeopleFilter(religion_qid=Religion.ISLAM)
        assert f.religion_qid is not None
        assert _QID_RE.match(str(f.religion_qid))

    def test_award_enum_in_filter(self) -> None:
        first_award = next(iter(Award))
        f = PeopleFilter(award_qid=first_award)
        assert f.award_qid == first_award.value

    def test_political_ideology_enum_in_filter(self) -> None:
        f = PeopleFilter(political_ideology_qid=PoliticalIdeology.SOCIALISM)
        assert f.political_ideology_qid is not None
        assert _QID_RE.match(str(f.political_ideology_qid))

    def test_raw_qid_string_still_accepted(self) -> None:
        f = PeopleFilter(occupation_qid="Q169470")
        assert f.occupation_qid == "Q169470"

    def test_enum_produces_correct_sparql_constraint(self) -> None:
        from wikidata_bulk_people._extract import _build_query

        f = PeopleFilter(
            occupation_qid=Occupation.PHYSICIST,
            citizenship_qid=Country.UNITED_STATES,
            gender_qid=Gender.FEMALE,
        )
        q = _build_query(f, last_qid=None)
        assert "wd:Q169470" in q, "Physicist QID missing from SPARQL"
        assert "wd:Q30" in q, "US QID missing from SPARQL"
        assert "wd:Q6581072" in q, "Female QID missing from SPARQL"
        assert "wdt:P106" in q
        assert "wdt:P27" in q
        assert "wdt:P21" in q

    def test_new_filter_fields_produce_sparql_constraints(self) -> None:
        from wikidata_bulk_people._extract import _build_query

        f = PeopleFilter(
            religion_qid=Religion.ISLAM,
            political_ideology_qid=PoliticalIdeology.SOCIALISM,
        )
        q = _build_query(f, last_qid=None)
        assert "wdt:P140" in q, "P140 (religion) missing from SPARQL"
        assert "wdt:P1142" in q, "P1142 (political ideology) missing from SPARQL"
