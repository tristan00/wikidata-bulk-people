import pytest

pytestmark = pytest.mark.integration

def test_extract_einstein():
    from wikidata_bulk_people import extract_person
    person = extract_person("Q937")
    assert person.qid == "Q937" and person.name == "Albert Einstein"
    assert person.date_of_birth.year == 1879
    assert person.date_of_death.year == 1955
    assert person.occupations and person.lead_paragraph

def test_live_sparql_string_cursor():
    from wikidata_bulk_people._discovery import _SparqlClient
    client = _SparqlClient("wikidata-bulk-people-tests (https://github.com/tristan00/wikidata-bulk-people)")
    try:
        rows = client.run_query('SELECT DISTINCT ?item WHERE { VALUES ?item { wd:Q1 wd:Q2 wd:Q3 } FILTER(STR(?item) > STR(wd:Q1)) } ORDER BY STR(?item)')
        assert [r["item"]["value"].rsplit("/", 1)[1] for r in rows] == ["Q2", "Q3"]
    finally:
        client.close()
