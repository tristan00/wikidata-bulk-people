import json
from unittest.mock import patch
import pytest
from rdflib import Graph, Namespace, URIRef, Literal
XSD_DATE = URIRef("http://www.w3.org/2001/XMLSchema#date")
from wikidata_bulk_people._discovery import QIDStream, _build_query, _SparqlClient
from wikidata_bulk_people import PeopleFilter, TransportError

WD = Namespace("http://www.wikidata.org/entity/")
WDT = Namespace("http://www.wikidata.org/prop/direct/")

def binding(qid):
    return {"item": {"type": "uri", "value": str(WD[qid])}}

def test_query_executes_with_string_cursor_and_distinct(monkeypatch):
    import wikidata_bulk_people._discovery as module
    monkeypatch.setattr(module, "_PAGE_SIZE", 2)
    graph = Graph()
    for qid in ("Q1", "Q2", "Q3", "Q10", "Q100"):
        graph.add((WD[qid], WDT.P31, WD.Q5))

        graph.add((WD[qid], WDT.P569, Literal("1900-01-01", datatype=XSD_DATE)))
        graph.add((WD[qid], WDT.P569, Literal("1901-01-01", datatype=XSD_DATE)))
    stream = QIDStream(PeopleFilter(has_wikipedia_article=False, born_after=1800), "test")
    queries = []
    def query(q):
        queries.append(q)
        return json.loads(graph.query(q, initNs={"wd": WD, "wdt": WDT}).serialize(format="json"))["results"]["bindings"]
    with patch.object(stream._sparql, "run_query", side_effect=query):
        assert list(stream) == ["Q1", "Q10", "Q100", "Q2", "Q3"]
    assert len(queries) == 3
    assert "SELECT DISTINCT" in queries[0] and "STR(?item)" in queries[1]

def test_unordered_is_explicitly_rejected():
    with pytest.raises(ValueError, match="ordered=False"):
        QIDStream(PeopleFilter(ordered=False), "test")


def test_bounded_discovery_executes_filter_once_and_orders_selected_membership():
    graph = Graph()
    for qid, year in (("Q20", 1900), ("Q3", 1901), ("Q11", 1899), ("Q10", 1900)):
        graph.add((WD[qid], WDT.P31, WD.Q5))
        graph.add((WD[qid], WDT.P569, Literal(f"{year}-01-01", datatype=XSD_DATE)))
    stream = QIDStream(PeopleFilter(has_wikipedia_article=False, born_after=1900), "test")
    queries = []
    def query(q):
        queries.append(q)
        return json.loads(graph.query(q, initNs={"wd": WD, "wdt": WDT}).serialize(format="json"))["results"]["bindings"]
    with patch.object(stream._sparql, "run_query", side_effect=query):
        pages = list(stream.iter_pages(snapshot_limit=1000))
    assert len(queries) == len(pages) == 1
    assert pages[0].qids == ["Q10", "Q20", "Q3"]
    assert "ORDER BY" not in queries[0]


@pytest.mark.parametrize("bindings", [[{}], [binding("Q1"), binding("Q1")],
                                     [binding("Q1"), binding("Q2"), binding("Q3")]])
def test_bounded_discovery_rejects_invalid_membership_before_yielding(bindings):
    stream = QIDStream(PeopleFilter(), "test")
    with patch.object(stream._sparql, "run_query", return_value=bindings), pytest.raises(TransportError):
        next(stream.iter_pages(snapshot_limit=2))


@pytest.mark.parametrize("kwargs", [{"snapshot_limit": 0}, {"snapshot_limit": 100001},
                                    {"snapshot_limit": 20, "start_after": "Q1"},
                                    {"snapshot_limit": 20, "start_year": 1900}])
def test_bounded_discovery_rejects_invalid_bounds_or_keyset_resume(kwargs):
    with pytest.raises(ValueError):
        list(QIDStream(PeopleFilter(), "test").iter_pages(**kwargs))

@pytest.mark.parametrize("page", [[binding("Q2"), binding("Q1")], [{}], [binding("Qx")]])
def test_invalid_pages_fail_instead_of_reporting_completion(page):
    stream = QIDStream(PeopleFilter(), "test")
    with patch.object(stream._sparql, "run_query", return_value=page), pytest.raises(TransportError):
        list(stream)

def test_cursor_must_advance(monkeypatch):
    monkeypatch.setattr("wikidata_bulk_people._discovery._PAGE_SIZE", 2)
    stream = QIDStream(PeopleFilter(), "test")
    with patch.object(stream._sparql, "run_query", return_value=[binding("Q1"), binding("Q2")]):
        with pytest.raises(TransportError, match="advance|ordered"):
            list(stream)

def test_year_bounds_and_duplicate_full_page_make_progress(monkeypatch):
    monkeypatch.setattr("wikidata_bulk_people._discovery._PAGE_SIZE", 2)
    stream = QIDStream(PeopleFilter(year_partition=True, born_after=1900, born_before=1901), "test")
    calls = []
    def query(q):
        calls.append(q)
        return [] if "STR(?item) >" in q else [binding("Q1"), binding("Q2")]
    with patch.object(stream._sparql, "run_query", side_effect=query):
        assert list(stream) == ["Q1", "Q2"]
    assert len(calls) == 4
    assert all("YEAR(?dob) >= 1900" in q and "YEAR(?dob) <= 1901" in q for q in calls)
    assert all("NO_DOB" not in q for q in calls)

def test_unbounded_year_partitions_include_tails_and_unknown_birth():
    stream = QIDStream(PeopleFilter(year_partition=True), "test")
    buckets = stream._buckets()
    assert buckets[0] == "BEFORE" and buckets[-2:] == ["AFTER", "NO_DOB"]
    with pytest.raises(ValueError, match="partition"):
        list(stream.iter_pages(start_year="bogus"))

@pytest.mark.parametrize("data", [{}, {"results": {}}, {"results": {"bindings": None}}])
def test_malformed_sparql_is_not_empty_success(data):
    client = _SparqlClient("test")
    with patch.object(client, "_get", return_value=data), pytest.raises(TransportError):
        client.run_query("SELECT ?item WHERE {}")

def test_qid_injection_rejected():
    with pytest.raises(ValueError, match="Invalid"):
        _build_query(PeopleFilter(occupation_qid="Q1 . } UNION {"), None)


def test_unbounded_partitions_include_unknown_and_unparseable_birth_values(monkeypatch):
    from rdflib import BNode
    monkeypatch.setattr("wikidata_bulk_people._discovery._YP_YEAR_MIN", 1900)
    monkeypatch.setattr("wikidata_bulk_people._discovery._YP_YEAR_MAX", 1901)
    graph = Graph()
    for number in range(1, 7):
        graph.add((WD[f"Q{number}"], WDT.P31, WD.Q5))
    graph.add((WD.Q1, WDT.P569, BNode()))
    graph.add((WD.Q3, WDT.P569, Literal("1900-01-01", datatype=XSD_DATE)))
    graph.add((WD.Q4, WDT.P569, Literal("1899-01-01", datatype=XSD_DATE)))
    graph.add((WD.Q5, WDT.P569, Literal("1902-01-01", datatype=XSD_DATE)))
    graph.add((WD.Q6, WDT.P569, Literal("unknown")))
    stream = QIDStream(PeopleFilter(has_wikipedia_article=False, year_partition=True), "test")
    def query(q):
        return json.loads(graph.query(q, initNs={"wd": WD, "wdt": WDT}).serialize(format="json"))["results"]["bindings"]
    with patch.object(stream._sparql, "run_query", side_effect=query):
        assert sorted(stream) == [f"Q{n}" for n in range(1, 7)]
