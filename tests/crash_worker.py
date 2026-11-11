import os
import pathlib
import sys
from unittest.mock import patch
from tests.support import FixtureAPI
from wikidata_bulk_people import BulkOptions, extract_people, extract_people_to_csv, extract_people_to_db
from wikidata_bulk_people._sinks import JSONLSink
from wikidata_bulk_people._sinks import CSVSink, DatabaseSink
from wikidata_bulk_people._bulk import JobStore
from wikidata_bulk_people._discovery import QIDStream

root, kind, phase = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
options = BulkOptions(job_path=root / "job.db", batch_size=3)
count = 501 if phase.startswith("discovery") else 12
api = FixtureAPI(count)
if phase.startswith("discovery"):
    api.images = 0
calls = [0]

def run():
    if kind == "jsonl":
        extract_people(root / "out", options=options)
    elif kind == "csv":
        extract_people_to_csv(root / "csv", options=options)
    else:
        extract_people_to_db("sqlite:///" + str(root / "output.db"), options=options)

with api:
    if phase == "write":
        cls = {"jsonl": JSONLSink, "csv": CSVSink, "database": DatabaseSink}[kind]
        original = cls.write
        def killed(self, person):
            original(self, person)
            calls[0] += 1
            if calls[0] == 5:
                os._exit(77)
        with patch.object(cls, "write", killed):
            run()
    elif phase == "database_init":
        original = DatabaseSink.read_pipeline_state
        def killed(self):
            result = original(self)
            os._exit(77)
        with patch.object(DatabaseSink, "read_pipeline_state", killed):
            run()
    elif phase == "database_commit":
        original = DatabaseSink.write_pipeline_state
        def killed(self, state):
            original(self, state)
            if state["exported"] == 6:
                os._exit(77)
        with patch.object(DatabaseSink, "write_pipeline_state", killed):
            run()
    elif phase == "file_flush":
        import wikidata_bulk_people._bulk as bulk
        original = bulk._flush_files
        def killed(*args):
            result = original(*args)
            calls[0] += 1
            if calls[0] == 2:
                os._exit(77)
            return result
        with patch.object(bulk, "_flush_files", killed):
            run()
    elif phase == "discovery_before_commit":
        original = JobStore.set
        def killed(self, key, value):
            original(self, key, value)
            if key == "state" and value["discovered"] == 500:
                os._exit(77)
        with patch.object(JobStore, "set", killed):
            run()
    else:
        original = QIDStream.iter_pages
        def killed(self, *args, **kw):
            for page in original(self, *args, **kw):
                yield page
                os._exit(77)
        with patch.object(QIDStream, "iter_pages", killed):
            run()
raise AssertionError("Fault injection did not trigger")
