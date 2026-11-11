import concurrent.futures
import contextlib
import csv
import json
import pathlib
import sqlite3
from unittest.mock import patch
import pytest

from tests.support import FixtureAPI
from wikidata_bulk_people import (
    BulkOptions, PeopleFilter, TransportError, ExtractionError, IncompleteRunError,
    extract_people, extract_people_to_csv, extract_people_to_db, extract_people_to_memory, iter_people,
)
from wikidata_bulk_people._bulk import _decode, _encode
from wikidata_bulk_people._storage import job_lock
from wikidata_bulk_people._sinks import JSONLSink
from wikidata_bulk_people._extract import PersonExtractor
from wikidata_bulk_people._sinks import CSVSink, DatabaseSink

def read_jsonl(out):
    return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("mode", ["iterator", "memory", "jsonl", "csv", "db"])
@pytest.mark.parametrize("interactive", [False, True])
def test_bulk_request_policy_reaches_all_public_paths(tmp_path, mode, interactive):
    options = BulkOptions(limit=2, interactive=interactive, job_path=tmp_path / "job")
    requests_seen = []
    with FixtureAPI(2) as api:
        api.failure = lambda url, params: requests_seen.append((url, dict(params)))
        if mode == "iterator":
            assert len(list(iter_people(options=options))) == 2
        elif mode == "memory":
            assert len(extract_people_to_memory(options=options)) == 2
        elif mode == "jsonl":
            extract_people(tmp_path / "people.jsonl", options=options)
        elif mode == "csv":
            extract_people_to_csv(tmp_path / "csv", options=options)
        else:
            extract_people_to_db("sqlite:///" + (tmp_path / "people.db").as_posix(), options=options)
    api_params = [p for url, p in requests_seen if url.endswith("/w/api.php")]
    assert api_params
    assert all(("maxlag" not in p) if interactive else p.get("maxlag") == "5" for p in api_params)

def status(out):
    return json.loads(pathlib.Path(str(out) + ".state.json").read_text())

def test_full_pipeline_has_all_fields_and_completed_resume_is_noop(tmp_path):
    out = tmp_path / "people.one.jsonl"
    with FixtureAPI(11) as api:
        extract_people(out, options=BulkOptions(batch_size=3))
        rows = read_jsonl(out)
        assert len(rows) == len({p["qid"] for p in rows}) == 11
        assert all(len(p["images"]) == 20 and len(p["spouses"]) == 2 for p in rows)
        before = out.read_bytes()
        count = sum(api.calls.values())
        extract_people(out, if_exists="replace", options=BulkOptions(batch_size=3))
        assert out.read_bytes() == before and sum(api.calls.values()) == count
    state = status(out)
    assert state["completed"] and state["discovered"] == state["enriched"] == state["exported"] == 11

@pytest.mark.parametrize("sink_type", ["jsonl", "csv", "database"])
def test_interruption_mid_batch_resumes_without_duplicates_or_loss(tmp_path, sink_type):
    options = BulkOptions(batch_size=3, job_path=tmp_path / "job.db")
    out = tmp_path / "out.jsonl"
    directory = tmp_path / "csv"
    database = tmp_path / "out.db"
    def run():
        if sink_type == "jsonl":
            extract_people(out, options=options)
        elif sink_type == "csv":
            extract_people_to_csv(directory, options=options)
        else:
            extract_people_to_db("sqlite:///" + str(database), options=options)
    cls = {"jsonl": JSONLSink, "csv": CSVSink, "database": DatabaseSink}[sink_type]
    original = cls.write
    calls = [0]
    def interrupted(self, person):
        original(self, person)
        calls[0] += 1
        if calls[0] == 5:
            raise KeyboardInterrupt()
    with FixtureAPI(12), patch.object(cls, "write", interrupted):
        with pytest.raises(KeyboardInterrupt):
            run()
    with FixtureAPI(12) as api:
        run()
        assert api.calls["discovery"] == 0
    if sink_type == "jsonl":
        ids = [r["qid"] for r in read_jsonl(out)]
    elif sink_type == "csv":
        with (directory / "people.csv").open(newline="", encoding="utf-8") as handle:
            ids = [r["qid"] for r in csv.DictReader(handle)]
        with (directory / "person_images.csv").open(newline="", encoding="utf-8") as handle:
            assert sum(1 for _ in csv.DictReader(handle)) == 240
    else:
        with contextlib.closing(sqlite3.connect(database)) as db:
            ids = [r[0] for r in db.execute("SELECT qid FROM people")]
            assert db.execute("SELECT COUNT(*) FROM person_images").fetchone()[0] == 240
    assert len(ids) == len(set(ids)) == 12

def test_transport_failure_keeps_discovered_work_pending(tmp_path):
    out = tmp_path / "out"
    def fail(url, params):
        if "commons.wikimedia.org" in url:
            raise TransportError(url, 503, "down")
    with FixtureAPI(6) as api:
        api.failure = fail
        with pytest.raises(TransportError):
            extract_people(out, options=BulkOptions(batch_size=3))
    assert not status(out)["completed"] and status(out)["discovered"] == 6
    with FixtureAPI(6) as api:
        extract_people(out, options=BulkOptions(batch_size=3))
        assert api.calls["discovery"] == api.calls["entities"] - 1 == 0
    assert len(read_jsonl(out)) == 6


def test_bounded_membership_survives_failure_without_rediscovery(tmp_path):
    out = tmp_path / "bounded.jsonl"
    options = BulkOptions(limit=1000)
    def fail(url, params):
        if "www.wikidata.org" in url:
            raise TransportError(url, 503, "source unavailable")
    with FixtureAPI(1005) as api:
        api.failure = fail
        with pytest.raises(TransportError):
            extract_people(out, options=options)
        assert api.calls["discovery"] == 1
    saved = status(out)
    assert saved["discovered"] == 1000 and saved["discovery_complete"]
    with FixtureAPI(1005) as api:
        extract_people(out, options=options)
        assert api.calls["discovery"] == 0
    rows = read_jsonl(out)
    assert len(rows) == len({r["qid"] for r in rows}) == 1000

def test_record_error_is_recorded_and_never_false_completion(tmp_path):
    out = tmp_path / "out"
    original = PersonExtractor.extract_many
    def broken(self, qids):
        result = original(self, qids)
        if "Q1000001" in qids:
            result["Q1000001"] = ExtractionError("Q1000001", "changed type")
        return result
    with FixtureAPI(6), patch.object(PersonExtractor, "extract_many", broken):
        with pytest.raises(IncompleteRunError):
            extract_people(out, options=BulkOptions(batch_size=3))
    assert not status(out)["completed"] and status(out)["failed"] == 1
    with FixtureAPI(6):
        extract_people(out, options=BulkOptions(batch_size=3))
    assert len(read_jsonl(out)) == 6 and status(out)["failed"] == 0

def test_close_failure_does_not_mark_completed(tmp_path):
    out = tmp_path / "out"
    original = JSONLSink.__exit__
    def fail(self, *args):
        original(self, *args)
        raise OSError("close failed")
    with FixtureAPI(4), patch.object(JSONLSink, "__exit__", fail), pytest.raises(OSError):
        extract_people(out)
    assert not status(out)["completed"]
    with FixtureAPI(4):
        extract_people(out)
    assert len(read_jsonl(out)) == 4

def test_changed_configuration_rejected_before_output_mutation(tmp_path):
    out = tmp_path / "out"
    with FixtureAPI(3):
        extract_people(out)
        before = out.read_bytes()
        with pytest.raises(ValueError, match="configuration"):
            extract_people(out, filter=PeopleFilter(born_after=1900), if_exists="replace")
        assert out.read_bytes() == before
    with pytest.raises(ValueError, match="changed or truncated"):
        out.write_text("bad")
        extract_people(out)

def test_distinct_dotted_paths_and_legacy_state(tmp_path):
    with FixtureAPI(1):
        for name in ("people.one.jsonl", "people.two.jsonl"):
            extract_people(tmp_path / name)
    assert status(tmp_path / "people.one.jsonl")["completed"]
    assert status(tmp_path / "people.two.jsonl")["completed"]
    out = tmp_path / "legacy"
    pathlib.Path(str(out) + ".state.json").write_text('{"last_qid":"Q123"}')
    with pytest.raises(ValueError, match="Legacy"):
        extract_people(out)

@pytest.mark.parametrize("kind", ["jsonl", "csv", "database"])
def test_limit_applies_to_all_outputs(tmp_path, kind):
    options = BulkOptions(limit=2, job_path=tmp_path / "job")
    with FixtureAPI(15):
        if kind == "jsonl":
            extract_people(tmp_path / "out", options=options)
            assert len(read_jsonl(tmp_path / "out")) == 2
        elif kind == "csv":
            extract_people_to_csv(tmp_path / "csv", options=options)
            with (tmp_path / "csv/people.csv").open(newline="") as handle:
                assert len(list(csv.DictReader(handle))) == 2
        else:
            extract_people_to_db("sqlite:///" + str(tmp_path / "db"), options=options)
            with contextlib.closing(sqlite3.connect(tmp_path / "db")) as db:
                assert db.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 2

def test_zero_limit_and_worker_thread_library_calls(tmp_path):
    with FixtureAPI(4) as api:
        extract_people(tmp_path / "zero", options=BulkOptions(limit=0))
        assert not api.calls and status(tmp_path / "zero")["completed"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            rows = pool.submit(extract_people_to_memory, options=BulkOptions(limit=2)).result()
        assert len(rows) == 2

def test_os_job_lock_excludes_a_second_writer(tmp_path):
    with job_lock(tmp_path / "lock"):
        with pytest.raises(ValueError, match="already in use"), job_lock(tmp_path / "lock"):
            pass

def test_default_database_batch_handles_ten_thousand_images_and_indexes(tmp_path):
    with FixtureAPI(500):
        people = list(iter_people(options=BulkOptions(limit=500)))
    with DatabaseSink("sqlite:///" + str(tmp_path / "db")) as sink:
        for p in people:
            sink.write(p)
    with contextlib.closing(sqlite3.connect(tmp_path / "db")) as db:
        assert db.execute("SELECT COUNT(*) FROM person_images").fetchone()[0] == 10000
        assert db.execute("PRAGMA index_list(person_images)").fetchall()
        assert "USING" in str(db.execute("EXPLAIN QUERY PLAN DELETE FROM person_images WHERE person_qid='Q1000000'").fetchall())


def test_rolled_back_enrichment_does_not_advance_counts(tmp_path):
    import wikidata_bulk_people._bulk as bulk
    out = tmp_path / "out"
    original = bulk._encode
    calls = [0]
    def failed(person):
        calls[0] += 1
        if calls[0] == 2:
            raise OSError("serialization failure")
        return original(person)
    with FixtureAPI(4), patch.object(bulk, "_encode", failed), pytest.raises(OSError):
        extract_people(out)
    assert status(out)["enriched"] == 0
    with FixtureAPI(4):
        extract_people(out)
    assert status(out)["enriched"] == status(out)["exported"] == 4


def test_rolled_back_discovery_does_not_advance_cursor_or_count(tmp_path):
    from wikidata_bulk_people._bulk import JobStore
    out = tmp_path / "out"
    original = JobStore.set
    failed = [False]
    def injected(self, key, value):
        original(self, key, value)
        if key == "state" and value["discovered"] == 4 and not failed[0]:
            failed[0] = True
            raise OSError("commit failed")
    with FixtureAPI(4), patch.object(JobStore, "set", injected), pytest.raises(OSError):
        extract_people(out)
    assert status(out)["discovered"] == 0 and status(out)["last_qid"] is None
    with FixtureAPI(4):
        extract_people(out)
    assert len(read_jsonl(out)) == status(out)["discovered"] == 4


def test_database_state_completion_and_atomic_batch_rollback(tmp_path):
    options = BulkOptions(job_path=tmp_path / "job", batch_size=3)
    url = "sqlite:///" + str(tmp_path / "db")
    original = DatabaseSink._flush
    def failed(self, state=None):
        if state is not None and state["exported"] == 6:

            self._buffers["person_aliases"][0]["person_qid"] = "Qmissing"
        return original(self, state)
    from sqlalchemy.exc import IntegrityError
    with FixtureAPI(6), patch.object(DatabaseSink, "_flush", failed), pytest.raises(IntegrityError):
        extract_people_to_db(url, options=options)
    with contextlib.closing(sqlite3.connect(tmp_path / "db")) as db:
        assert db.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 3
        assert json.loads(db.execute("SELECT value FROM pipeline_state").fetchone()[0])["exported"] == 3
    with FixtureAPI(6):
        extract_people_to_db(url, options=options)
    with contextlib.closing(sqlite3.connect(tmp_path / "db")) as db:
        state = json.loads(db.execute("SELECT value FROM pipeline_state").fetchone()[0])
        assert state["completed"] and state["exported"] == 6


def test_csv_schema_checked_before_append(tmp_path):
    directory = tmp_path / "csv"
    directory.mkdir()
    (directory / "people.csv").write_text("wrong,columns\n")
    with pytest.raises(ValueError, match="schema"), CSVSink(directory, if_exists="append"):
        pass
    assert (directory / "people.csv").read_text() == "wrong,columns\n"


def test_old_jsonl_state_is_not_silently_appended(tmp_path):
    (tmp_path / "people.jsonl").write_text("existing\n")
    (tmp_path / "people.state.json").write_text('{"last_qid":"Q1"}')
    with pytest.raises(ValueError, match="Legacy"):
        extract_people(tmp_path / "people.jsonl")


def test_job_progress_queries_have_partial_indexes(tmp_path):
    from wikidata_bulk_people._bulk import JobStore
    store = JobStore(tmp_path / "job", {}, False)
    try:
        for predicate in ("person IS NOT NULL", "person IS NULL", "error IS NOT NULL"):
            plan = store.db.execute("EXPLAIN QUERY PLAN SELECT COUNT(*) FROM work WHERE " + predicate).fetchall()
            assert "USING INDEX" in str(plan), plan
    finally:
        store.close()


@pytest.mark.parametrize("kw", [{"batch_size": 0}, {"workers": 5}, {"cache_ttl": -1}, {"limit": -1}])
def test_invalid_resource_options_fail_before_io(kw):
    with pytest.raises(ValueError):
        BulkOptions(**kw)


def test_legacy_state_left_after_explicit_fresh_run_does_not_block_new_journal(tmp_path):
    out = tmp_path / "people.jsonl"
    (tmp_path / "people.state.json").write_text('{"last_qid":"Q123"}')
    with FixtureAPI(2):
        extract_people(out, resume=False, if_exists="replace")
        extract_people(out)
    assert len(read_jsonl(out)) == 2


def test_colliding_state_paths_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="distinct"):
        extract_people(tmp_path / "out", options=BulkOptions(job_path=tmp_path / "out"))
    with pytest.raises(ValueError, match="distinct"):
        extract_people_to_db("sqlite:///" + str(tmp_path / "db"),
                             options=BulkOptions(job_path=tmp_path / "db"))


def test_database_transaction_limit_respected_inside_enrichment_batch(tmp_path):
    checkpoints = []
    original = DatabaseSink.write_pipeline_state
    def capture(self, state):
        checkpoints.append(state["exported"])
        original(self, state)
    with FixtureAPI(7), patch.object(DatabaseSink, "write_pipeline_state", capture):
        extract_people_to_db("sqlite:///" + str(tmp_path / "db"), batch_size=2,
            options=BulkOptions(batch_size=5, job_path=tmp_path / "job"))
    assert checkpoints == [2, 4, 5, 7]
