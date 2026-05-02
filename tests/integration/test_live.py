"""Integration tests — make real network calls to Wikidata/Wikipedia.

Run with:
    pytest -m integration -v
"""

import pytest


@pytest.mark.integration
def test_extract_einstein() -> None:
    """Fetch Albert Einstein (Q937) from Wikidata and verify key fields."""
    from wikidata_bulk_people import extract_person

    person = extract_person("Q937")

    assert person.qid == "Q937"
    assert person.name == "Albert Einstein"
    assert person.date_of_birth is not None
    assert person.date_of_birth.year == 1879
    assert person.date_of_birth.month == 3
    assert person.date_of_birth.day == 14
    assert person.date_of_death is not None
    assert person.date_of_death.year == 1955
    assert person.sex_or_gender == "male"
    assert len(person.occupations) > 0
