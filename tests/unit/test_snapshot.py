import copy
import gzip
import hashlib
import io
import json
import pathlib
import sqlite3
from contextlib import closing
from concurrent.futures import CancelledError
from threading import Event

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import requests

from tests.support import FixtureAPI, item, date
from wikidata_bulk_people import (BulkOptions, PeopleFilter, TransportError, extract_people, iter_people,
                                  extract_people_to_csv, extract_people_to_db, extract_people_to_memory)
from wikidata_bulk_people import _snapshot as source
from wikidata_bulk_people._cache import ObjectCache


def entity(qid="Q1000000", title="Person Q1000000"):
    with FixtureAPI() as api:
        result = api.get("https://www.wikidata.org/w/api.php", {"ids": qid}).json()["entities"][qid]
    result["sitelinks"] = {"enwiki": {"title": title}} if title else {}
    return result


class HTTP:
    def __init__(self, body=b"", data=None, status=200, headers=None):
        self.raw = io.BytesIO(body)
        self.body, self.data, self.status_code = body, data, status
        self.headers = headers or {"ETag": '"snapshot-v1"', "Last-Modified": "2026-11-06"}
        self.links = {}
        self.text = body.decode(errors="replace")
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()
    def close(self):
        pass
    def json(self):
        return self.data
    def iter_content(self, size):
        yield self.body


def dump_response(entities):
    body = b"[\n" + b",\n".join(json.dumps(e).encode() for e in entities) + b"\n]\n"
    return HTTP(gzip.compress(body))


@pytest.mark.parametrize("kwargs,expected", [
    ({}, True), ({"born_after": 1900, "born_before": 1900}, True),
    ({"born_after": 1901}, False), ({"born_before": 1899}, False),
    ({"occupation_qid": "Q100"}, True), ({"occupation_qid": "Q999"}, False),
    ({"citizenship_qid": "Q30"}, True), ({"gender_qid": "Q6581097"}, True),
    ({"religion_qid": "Q1"}, False), ({"award_qid": "Q1"}, False),
    ({"political_ideology_qid": "Q1"}, False), ({"living": False}, True), ({"living": True}, False),
])
def test_snapshot_filter_preserves_existing_search_constraints(kwargs, expected):
    assert source.matches(entity(), PeopleFilter(**kwargs)) is expected


def test_snapshot_filter_uses_truthy_ranks_unknown_death_and_optional_article():
    e = entity(title=None)
    assert not source.matches(e, PeopleFilter())
    assert source.matches(e, PeopleFilter(has_wikipedia_article=False))
    e["claims"]["P570"] = [{"mainsnak": {"snaktype": "somevalue"}}]
    assert source.matches(e, PeopleFilter(has_wikipedia_article=False, living=False))
    e["claims"]["P570"] = [{"mainsnak": {"snaktype": "novalue"}}]
    assert source.matches(e, PeopleFilter(has_wikipedia_article=False, living=True))
    preferred = item("Q123")
    preferred["rank"] = "preferred"
    e["claims"]["P31"].append(preferred)
    assert not source.matches(e, PeopleFilter(has_wikipedia_article=False))


def test_projection_preserves_qualifiers_ranks_aliases_and_revision():
    e = entity()
    e["claims"]["P26"][0].update(rank="preferred", qualifiers={"P580": [date()["mainsnak"]]}, references=[{}])
    projected = source._project(e)
    assert projected["claims"]["P26"][0]["qualifiers"] == e["claims"]["P26"][0]["qualifiers"]
    assert projected["claims"]["P26"][0]["rank"] == "preferred"
    assert projected["lastrevid"] == e["lastrevid"] and projected["aliases"] == e["aliases"]
    assert "references" not in projected["claims"]["P26"][0]
    assert "references" in e["claims"]["P26"][0]


def test_dump_selection_is_durable_bounded_and_reusable(tmp_path, monkeypatch):
    records = [entity("Q1000000"), entity("Q1000001"), entity("Q1000002")]
    monkeypatch.setattr(source, "_request", lambda *a, **kw: dump_response(records))
    result = source.acquire_entities(tmp_path, PeopleFilter(), 2, "test")
    assert result["selected"] == 2 and result["complete"]
    assert list(source.iter_qids(tmp_path)) == ["Q1000000", "Q1000001"]
    monkeypatch.setattr(source, "_request", lambda *a, **kw: pytest.fail("completed source downloaded again"))
    assert source.acquire_entities(tmp_path, PeopleFilter(), 2, "test") == result
    with closing(sqlite3.connect(tmp_path / "entities.sqlite3")) as db:
        db.execute("DELETE FROM entities WHERE qid='Q1000001'")
        db.commit()
    with pytest.raises(ValueError, match="incomplete"):
        source.acquire_entities(tmp_path, PeopleFilter(), 2, "test")


def test_dump_resume_refuses_changed_revision(tmp_path, monkeypatch):
    source._write(tmp_path / "wikidata.json", {"etag": '"old"'})
    monkeypatch.setattr(source, "_request", lambda *a, **kw: dump_response([entity()]))
    with pytest.raises(ValueError, match="changed"):
        source.acquire_entities(tmp_path, PeopleFilter(), 2, "test")
    assert list(source.iter_qids(tmp_path)) == []


def test_truncated_dump_is_retryable_and_never_marks_partial_selection_complete(tmp_path, monkeypatch):
    records = [entity("Q1000000"), entity("Q1000001")]
    truncated = dump_response(records[:1]).body[:-8]
    monkeypatch.setattr(source, "_request", lambda *a, **kw: HTTP(truncated))
    with pytest.raises(TransportError, match="Interrupted compressed source"):
        source.acquire_entities(tmp_path, PeopleFilter(), 2, "test")
    assert not json.loads((tmp_path / "wikidata.json").read_text())["complete"]
    monkeypatch.setattr(source, "_request", lambda *a, **kw: dump_response(records))
    assert source.acquire_entities(tmp_path, PeopleFilter(), 2, "test")["selected"] == 2


def test_download_resume_verifies_range_and_checksum(tmp_path, monkeypatch):
    content = b"real compressed parquet bytes"
    entry = {"path": "enwiki/data/data.parquet", "size": len(content), "lfs": {"oid": hashlib.sha256(content).hexdigest()}}
    (tmp_path / "data.partial").write_bytes(content[:5])
    def request(*args, **kwargs):
        assert kwargs["headers"] == {"Range": "bytes=5-"}
        return HTTP(content[5:], status=206, headers={"Content-Range": f"bytes 5-{len(content)-1}/{len(content)}"})
    monkeypatch.setattr(source, "_request", request)
    source._download(entry, "revision", tmp_path, "test")
    assert (tmp_path / "data.parquet").read_bytes() == content
    source._download(entry, "revision", tmp_path, "test")
    (tmp_path / "data.parquet").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="Corrupt"):
        source._download(entry, "revision", tmp_path, "test")


@pytest.mark.parametrize("kind", ["wrong-range", "wrong-sha", "oversized"])
def test_bad_source_download_cannot_be_promoted(tmp_path, monkeypatch, kind):
    entry = {"path": "file.parquet", "size": 4, "lfs": {"oid": hashlib.sha256(b"abcd").hexdigest()}}
    if kind == "wrong-range":
        (tmp_path / "file.partial").write_bytes(b"a")
    if kind == "oversized":
        (tmp_path / "file.partial").write_bytes(b"abcdef")
    monkeypatch.setattr(source, "_request", lambda *a, **kw: HTTP(b"wrong"))
    with pytest.raises((TransportError, ValueError)):
        source._download(entry, "revision", tmp_path, "test")
    assert not (tmp_path / "file.parquet").exists()


def test_acquisition_request_retries_503_and_respects_retry_after(monkeypatch):
    responses = iter([HTTP(status=503, headers={"Retry-After": "7"}), HTTP(b"ok")])
    pauses = []
    monkeypatch.setattr(requests, "get", lambda *a, **kw: next(responses))
    monkeypatch.setattr(source.time, "sleep", pauses.append)
    assert source._request("https://example.test/source", "test").body == b"ok"
    assert pauses == [7]
    monkeypatch.setattr(requests, "get", lambda *a, **kw: HTTP(status=403))
    with pytest.raises(TransportError):
        source._request("https://example.test/source", "test")


def article(qid="Q1000000"):
    image = {"content_url": f"https://upload.wikimedia.org/wikipedia/commons/a/ab/{qid}_0.jpg",
             "alternative_text": "Actual image alternative text"}
    return {"name": f"Person {qid}", "main_entity": {"identifier": qid}, "image": image,
            "abstract": "Fallback abstract", "sections": json.dumps([{"name": "Abstract", "has_parts": [
                {"type": "paragraph", "value": "First paragraph."}, {"type": "paragraph", "value": "Second."}]}]),
            "infoboxes": json.dumps([{"nested": image}]), "tables": json.dumps([{"nested": image}])}


def test_article_projection_deduplicates_nested_images_and_keeps_alt_and_paragraphs():
    page, alts = source.article_objects(article())
    assert page == {"extract": "First paragraph.\n\nSecond.", "images": ["File:Q1000000 0.jpg"]}
    assert alts == {"q1000000 0.jpg": "Actual image alternative text"}
    assert source.article_objects({"abstract": "fallback"}) == ({"extract": "fallback", "images": []}, {})


def setup_sources(directory, monkeypatch):
    directory.mkdir()
    records = [entity("Q1000000"), entity("Q1000001", "Person Q1000001")]
    monkeypatch.setattr(source, "_request", lambda *a, **kw: dump_response(records))
    source.acquire_entities(directory, PeopleFilter(), 2, "test")
    articles = directory / "articles"
    articles.mkdir()
    pq.write_table(pa.Table.from_pylist([article(), article("Q1000001"), article("Q9999999")]), articles / "data.parquet")
    return {"files": [{"path": "enwiki/data/data.parquet"}]}


def test_real_parquet_source_preparation_and_all_existing_pipeline_fields(tmp_path, monkeypatch):
    directory = tmp_path / "sources"
    manifest = setup_sources(directory, monkeypatch)
    def query(self, text):
        import re
        ids = re.findall(r"wd:(Q\d+)", text)
        return [{"item": {"value": "http://www.wikidata.org/entity/" + q}, "label": {"value": "Label " + q}} for q in ids]
    monkeypatch.setattr(source._SparqlClient, "run_query", query)
    with FixtureAPI() as api:
        prepared = source.populate(directory, manifest, "test")
        assert prepared["people"] == prepared["matched_articles"] == 2
        calls = api.calls.copy()
        assert source.populate(directory, manifest, "test") == prepared
        assert calls == api.calls
    monkeypatch.setattr(source, "prepare", lambda *a, **kw: {"prepared": prepared, "date": "snapshot"})
    out = tmp_path / "people.jsonl"
    options = BulkOptions(limit=2, public_snapshot=directory)
    with FixtureAPI() as api:
        extract_people(out, options=options)
        assert sum(api.calls.values()) == 0
        rows = [json.loads(line) for line in out.read_text().splitlines()]
        assert len(rows) == 2 and rows[0]["lead_paragraph"] == "First paragraph."
        assert rows[0]["images"][0]["alt"] == "Actual image alternative text"
        assert len(rows[0]["spouses"]) == 2 and rows[0]["date_of_birth"]["year"] == 1900
        assert len(list(iter_people(options=options))) == 2
        assert len(extract_people_to_memory(options=options)) == 2
        extract_people_to_csv(tmp_path / "csv", options=options)
        extract_people_to_db("sqlite:///" + str(tmp_path / "people.db"), options=options)
        import csv
        with (tmp_path / "csv" / "person_images.csv").open(newline="", encoding="utf-8") as handle:
            assert len(list(csv.DictReader(handle))) == 2
        with closing(sqlite3.connect(tmp_path / "people.db")) as db:
            assert db.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 2
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        extract_people(out, options=options)
        assert len(out.read_text().splitlines()) == 2
    with closing(sqlite3.connect(str(out) + ".state.json.job.sqlite3")) as db:
        assert json.loads(db.execute("SELECT value FROM meta WHERE key='source_provenance'").fetchone()[0])["date"] == "snapshot"


def test_renamed_article_is_matched_by_stable_entity_identity(tmp_path, monkeypatch):
    directory = tmp_path / "sources"
    manifest = setup_sources(directory, monkeypatch)
    row = article()
    row["name"] = "Previous article title"
    pq.write_table(pa.Table.from_pylist([row]), directory / "articles" / "data.parquet")
    monkeypatch.setattr(source._SparqlClient, "run_query", lambda self, q: [
        {"item": {"value": "http://www.wikidata.org/entity/" + qid}}
        for qid in __import__('re').findall(r"wd:(Q\d+)", q)])
    with FixtureAPI():
        assert source.populate(directory, manifest, "test")["matched_articles"] == 1
    cache = ObjectCache(directory / "source-cache.sqlite3")
    assert cache.get("enwiki:page:Person Q1000000")[0]
    assert not cache.get("enwiki:page:Previous article title")[0]
    cache.close()


def test_metadata_failure_resumes_only_unfinished_article_shards(tmp_path, monkeypatch):
    directory = tmp_path / "sources"
    manifest = setup_sources(directory, monkeypatch)
    pq.write_table(pa.Table.from_pylist([article()]), directory / "articles" / "data.parquet")
    pq.write_table(pa.Table.from_pylist([article("Q1000001")]), directory / "articles" / "second.parquet")
    manifest["files"].append({"path": "enwiki/data/second.parquet"})
    monkeypatch.setattr(source._SparqlClient, "run_query", lambda self, q: [
        {"item": {"value": "http://www.wikidata.org/entity/" + qid}}
        for qid in __import__('re').findall(r"wd:(Q\d+)", q)])
    original = source.CommonsClient.get_image_infos
    calls = []
    def fail_second(self, files):
        calls.append(files)
        if len(calls) == 2:
            raise TransportError("test", reason="metadata unavailable")
        return original(self, files)
    monkeypatch.setattr(source.CommonsClient, "get_image_infos", fail_second)
    with FixtureAPI() as api:
        with pytest.raises(TransportError, match="metadata unavailable"):
            source.populate(directory, manifest, "test")
        progress = json.loads((directory / "prepared.json").read_text())
        assert progress["indexed_shards"] == 1 and not progress.get("complete")
        monkeypatch.setattr(source.CommonsClient, "get_image_infos", original)
        opened = []
        parquet_file = pq.ParquetFile
        def tracked(path):
            opened.append(path.name)
            return parquet_file(path)
        monkeypatch.setattr(pq, "ParquetFile", tracked)
        completed = source.populate(directory, manifest, "test")
        assert completed["complete"] and completed["matched_articles"] == 2
        assert opened == ["second.parquet"]
        assert api.calls["commons"] == 2


def test_duplicate_article_versions_choose_latest_exact_title_and_count_once(tmp_path, monkeypatch):
    directory = tmp_path / "sources"
    manifest = setup_sources(directory, monkeypatch)
    rows = []
    for qid, revision, name, text in [
        ("Q1000000", 100, "Person Q1000000", "Correct current-title revision"),
        ("Q1000000", 300, "Previous title", "Different article title"),
        ("Q1000000", 50, "Person Q1000000", "Older revision encountered later"),
        ("Q1000001", 20, "Person Q1000001", "Newest second person"),
        ("Q1000001", 10, "Person Q1000001", "Old second person")]:
        row = article(qid)
        row.update(version={"identifier": revision}, identifier=int(qid[1:]), name=name, sections=None,
                   abstract=text)
        rows.append(row)
    pq.write_table(pa.Table.from_pylist(rows), directory / "articles" / "data.parquet")
    monkeypatch.setattr(source._SparqlClient, "run_query", lambda self, q: [
        {"item": {"value": "http://www.wikidata.org/entity/" + qid}}
        for qid in __import__('re').findall(r"wd:(Q\d+)", q)])
    with FixtureAPI():
        assert source.populate(directory, manifest, "test")["matched_articles"] == 2
    cache = ObjectCache(directory / "source-cache.sqlite3")
    assert cache.get("enwiki:page:Person Q1000000")[1]["extract"] == "Correct current-title revision"
    assert cache.get("enwiki:selection:Q1000000")[1]["revision"] == 100
    assert cache.get("enwiki:page:Person Q1000001")[1]["extract"] == "Newest second person"
    cache.close()


def test_interrupted_job_refuses_replaced_source_revisions(tmp_path, monkeypatch):
    directory = tmp_path / "sources"
    manifest = setup_sources(directory, monkeypatch)
    monkeypatch.setattr(source._SparqlClient, "run_query", lambda self, q: [
        {"item": {"value": "http://www.wikidata.org/entity/" + qid}}
        for qid in __import__('re').findall(r"wd:(Q\d+)", q)])
    with FixtureAPI():
        source.populate(directory, manifest, "test")
    monkeypatch.setattr(source, "prepare", lambda *a, **kw: {"revision": "original"})
    from wikidata_bulk_people._extract import PersonExtractor
    original = PersonExtractor.extract_many
    calls = []
    def interrupted(self, qids):
        calls.append(qids)
        if len(calls) == 2:
            raise TransportError("test", reason="interrupted")
        return original(self, qids)
    monkeypatch.setattr(PersonExtractor, "extract_many", interrupted)
    out = tmp_path / "people.jsonl"
    options = BulkOptions(limit=2, batch_size=1, public_snapshot=directory)
    with pytest.raises(TransportError, match="interrupted"):
        extract_people(out, options=options)
    before = out.read_bytes()
    assert len(before.splitlines()) == 1
    monkeypatch.setattr(source, "prepare", lambda *a, **kw: {"revision": "replacement"})
    monkeypatch.setattr(PersonExtractor, "extract_many", original)
    with pytest.raises(ValueError, match="source revisions changed"):
        extract_people(out, options=options)
    assert out.read_bytes() == before


def test_snapshot_objects_never_pollute_or_expire_with_the_live_cache(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    frozen = ObjectCache(source_dir / "source-cache.sqlite3")
    frozen.put_many({"entity:en:Q1": {"id": "Q1"}})
    frozen.close()
    cache = source.SnapshotCache(source_dir, tmp_path / "live.sqlite3", ttl=0)
    cache.put_many({"online": "expires"})
    assert cache.get("entity:en:Q1") == (True, {"id": "Q1"})
    assert cache.get("online") == (False, None)
    assert (cache.hits, cache.misses) == (1, 1)
    cache.close()
    live = ObjectCache(tmp_path / "live.sqlite3")
    assert live.get("entity:en:Q1") == (False, None)
    live.close()


@pytest.mark.parametrize("interactive", [False, True])
def test_sparse_article_fallbacks_are_batched_and_reusable(tmp_path, interactive):
    cache = ObjectCache(tmp_path / "cache.sqlite3")
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE entities(qid TEXT PRIMARY KEY,title TEXT)")
        db.executemany("INSERT INTO entities VALUES(?,?)",
                       [(f"Q{i}", f"Person Q{i}") for i in range(1000000, 1000024)])
        with FixtureAPI() as api:
            requests_seen = []
            api.failure = lambda url, params: requests_seen.append((url, dict(params)))
            commons = source.CommonsClient("test", cache=cache, interactive=interactive)
            try:
                assert source._prefetch_fallbacks(db, cache, commons, "test", interactive=interactive) == 24
                assert api.calls["articles"] == 2
                assert api.calls["html"] == 24
                before = api.calls.copy()
                assert source._prefetch_fallbacks(db, cache, commons, "test", interactive=interactive) == 0
                assert api.calls == before
                params = [p for url, p in requests_seen if url.endswith("/w/api.php")]
                assert params
                assert all(("maxlag" not in p) if interactive else p.get("maxlag") == "5" for p in params)
            finally:
                commons.close()
    cache.close()


def test_missing_checkpointed_source_cache_is_rejected(tmp_path):
    (tmp_path / "prepared.json").write_text(json.dumps({"indexed_shards": 1, "matched_articles": 20}))
    with pytest.raises(ValueError, match="Checkpointed source cache is missing"):
        source.populate(tmp_path, {"files": []}, "test")
    cache = ObjectCache(tmp_path / "source-cache.sqlite3")
    cache.close()
    with pytest.raises(ValueError, match="does not match"):
        source.populate(tmp_path, {"files": []}, "test")


def test_source_options_and_path_collisions_fail_before_acquisition(tmp_path):
    with pytest.raises(ValueError, match="positive limit"):
        BulkOptions(public_snapshot=tmp_path)
    with pytest.raises(ValueError, match="separate"):
        extract_people(tmp_path / "people", options=BulkOptions(limit=2, public_snapshot=tmp_path))


@pytest.mark.parametrize("output", ["jsonl", "csv", "database", "iterator", "memory"])
def test_snapshot_rejects_live_only_partition_option_for_every_public_output(tmp_path, output):
    options = BulkOptions(limit=2, public_snapshot=tmp_path / "sources")
    kwargs = {"filter": PeopleFilter(year_partition=True), "options": options}
    with pytest.raises(ValueError, match="only applies to live API"):
        if output == "jsonl":
            extract_people(tmp_path / "out.jsonl", **kwargs)
        elif output == "csv":
            extract_people_to_csv(tmp_path / "csv", **kwargs)
        elif output == "database":
            extract_people_to_db("sqlite:///" + str(tmp_path / "out.db"), **kwargs)
        elif output == "iterator":
            list(iter_people(**kwargs))
        else:
            extract_people_to_memory(**kwargs)
    assert not (tmp_path / "sources").exists()


@pytest.mark.parametrize("same_path", [True, False])
def test_iterator_rejects_live_cache_in_snapshot_directory(tmp_path, same_path):
    directory = tmp_path / "sources"
    cache = directory if same_path else directory / "live.db"
    with pytest.raises(ValueError, match="outside the public snapshot"):
        list(iter_people(options=BulkOptions(limit=1, public_snapshot=directory, cache_path=cache)))
    assert not directory.exists()


def test_prepare_manifest_reuses_pinned_sources_and_rejects_changed_selection(tmp_path, monkeypatch):
    directory = tmp_path / "source"
    directory.mkdir()
    (directory / "entities.sqlite3").touch()
    (directory / "source-cache.sqlite3").touch()
    monkeypatch.setattr(source, "acquire_entities", lambda *a: {"etag": "version"})
    monkeypatch.setattr(source, "acquire_articles", lambda *a: {"revision": "fixed", "files": []})
    monkeypatch.setattr(source, "populate", lambda *a, **kw: {"complete": True, "people": 2})
    provenance = source.prepare(directory, PeopleFilter(), 2, "test")
    assert provenance["articles"]["revision"] == "fixed"
    monkeypatch.setattr(source, "populate", lambda *a, **kw: pytest.fail("prepared source rebuilt"))
    assert source.prepare(directory, PeopleFilter(), 2, "test") == provenance
    with pytest.raises(ValueError, match="different selection"):
        source.prepare(directory, PeopleFilter(living=True), 2, "test")
    (directory / "source-cache.sqlite3").unlink()
    with pytest.raises(ValueError, match="missing"):
        source.prepare(directory, PeopleFilter(), 2, "test")


def test_article_inventory_is_pinned_downloaded_verified_and_reused(tmp_path, monkeypatch):
    content = b"test parquet source"
    entries = [{"type": "file", "path": f"enwiki/data/{i}.parquet", "size": len(content),
                "lfs": {"oid": hashlib.sha256(content).hexdigest()}} for i in range(5)]
    calls = []
    def get(url, *args, **kwargs):
        calls.append(url)
        if url.endswith("/structured-wikipedia"):
            return HTTP(data={"sha": "pinned-revision"})
        if "/tree/" in url:
            assert "/pinned-revision/" in url
            return HTTP(data=entries)
        if url.endswith("README.md"):
            return HTTP(b"Dataset extracted: 13 May 2026\n")
        assert "/pinned-revision/" in url
        return HTTP(content)
    monkeypatch.setattr(source, "_request", get)
    manifest = source.acquire_articles(tmp_path, "test")
    assert manifest["complete"] and manifest["dataset_date"] == "13 May 2026"
    assert len(list(tmp_path.glob("*.parquet"))) == 5
    before = len(calls)
    source.acquire_articles(tmp_path, "test")
    assert len(calls) == before


@pytest.mark.parametrize("problem", ["pagination", "empty", "invalid"])
def test_bad_article_inventory_is_never_marked_complete(tmp_path, monkeypatch, problem):
    def get(url, *args, **kwargs):
        if url.endswith("/structured-wikipedia"):
            return HTTP(data={"sha": "revision"})
        response = HTTP(data=[] if problem == "empty" else [{"type": "directory", "path": "bad"}])
        if problem == "pagination":
            response.links = {"next": {"url": "not-requested"}}
        return response
    monkeypatch.setattr(source, "_request", get)
    with pytest.raises(ValueError, match="paginated|inventory"):
        source.acquire_articles(tmp_path, "test")
    assert not (tmp_path / "manifest.json").exists()


def test_download_request_failure_retries_and_exhausts(monkeypatch):
    pauses = []
    monkeypatch.setattr(source.time, "sleep", pauses.append)
    def fail(*args, **kwargs):
        raise requests.ConnectionError("disconnected")
    monkeypatch.setattr(requests, "get", fail)
    with pytest.raises(TransportError, match="Source download failed"):
        source._request("https://example.test/file", "test")
    assert pauses == [1, 2, 4, 8]


@pytest.mark.parametrize("failure", ["entities", "articles", "populate"])
def test_prepare_cancels_workers_before_releasing_lock(tmp_path, monkeypatch, failure):
    started = Event()
    finished = Event()

    def interrupted_worker(stop):
        started.set()
        assert stop.wait(5), "Failed preparation did not cancel the other worker"
        with pytest.raises(ValueError, match="already in use"), source.job_lock(tmp_path / "source.lock"):
            pass
        finished.set()
        source._check_cancelled(stop)

    def entities(directory, filter, limit, user_agent, stop):
        if failure == "articles":
            interrupted_worker(stop)
        if failure == "entities":
            assert started.wait(5)
            raise OSError("entity failure")
        return {"selected": 1}

    def articles(directory, user_agent, stop):
        if failure == "articles":
            assert started.wait(5)
            raise OSError("article failure")
        source._write(directory / "manifest.json", {"files": []})
        interrupted_worker(stop)

    def populate(*args, **kwargs):
        assert started.wait(5)
        raise OSError("populate failure")

    monkeypatch.setattr(source, "acquire_entities", entities)
    monkeypatch.setattr(source, "acquire_articles", articles)
    monkeypatch.setattr(source, "populate", populate)
    with pytest.raises(OSError, match="failure"):
        source.prepare(tmp_path, PeopleFilter(), 1, "test")
    assert finished.is_set()
    assert not (tmp_path / "provenance.json").exists()
    with source.job_lock(tmp_path / "source.lock"):
        pass


def test_article_failure_cancels_active_downloads_and_skips_queue(tmp_path, monkeypatch):
    source._write(tmp_path / "manifest.json", {
        "revision": "fixed", "files": [{"path": str(i)} for i in range(8)], "started_epoch": 0})
    started = Event()
    finished = Event()
    calls = []

    def download(entry, revision, directory, user_agent, stop):
        calls.append(entry["path"])
        if entry["path"] == "0":
            assert started.wait(5)
            raise OSError("download failed")
        started.set()
        assert stop.wait(5)
        finished.set()
        source._check_cancelled(stop)

    monkeypatch.setattr(source, "_download", download)
    with pytest.raises(OSError, match="download failed"):
        source.acquire_articles(tmp_path, "test")
    assert finished.is_set()
    assert set(calls) <= {"0", "1", "2"}
    assert not source._read(tmp_path / "manifest.json").get("complete")


def test_cancelled_download_keeps_partial_without_promoting_it(tmp_path, monkeypatch):
    stop = Event()
    closed = []

    class Interrupted(HTTP):
        def iter_content(self, size):
            yield b"ab"
            stop.set()
            yield b"cd"

        def close(self):
            closed.append(True)

    entry = {"path": "data.parquet", "size": 4, "lfs": {"oid": hashlib.sha256(b"abcd").hexdigest()}}
    monkeypatch.setattr(source, "_request", lambda *a, **kw: Interrupted())
    with pytest.raises(CancelledError):
        source._download(entry, "fixed", tmp_path, "test", stop)
    assert closed
    assert (tmp_path / "data.partial").read_bytes() == b"ab"
    assert not (tmp_path / "data.parquet").exists()


def test_cancellation_interrupts_retry_backoff(monkeypatch):
    stop = Event()
    calls = []

    def get(*args, **kwargs):
        calls.append(True)
        stop.set()
        raise requests.ConnectionError("interrupted")

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(CancelledError):
        source._request("https://example.test/file", "test", stop=stop)
    assert calls == [True]
