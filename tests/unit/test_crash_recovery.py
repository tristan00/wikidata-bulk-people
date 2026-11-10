import contextlib
import csv
import json
import sqlite3
import subprocess
import sys
import pytest
from tests.support import FixtureAPI
from wikidata_bulk_people import BulkOptions, extract_people, extract_people_to_csv, extract_people_to_db

@pytest.mark.parametrize("kind,phase", [
    ("jsonl", "write"), ("csv", "write"), ("database", "write"),
    ("jsonl", "file_flush"), ("csv", "file_flush"), ("database", "database_commit"),
    ("database", "database_init"), ("jsonl", "discovery_before_commit"), ("jsonl", "discovery_after_commit"),
])
def test_process_death_has_no_duplicate_or_lost_records(tmp_path, kind, phase):
    child = subprocess.run([sys.executable, "-m", "tests.crash_worker", str(tmp_path), kind, phase],
                           capture_output=True, text=True, timeout=30)
    assert child.returncode == 77, child.stdout + child.stderr
    count = 501 if phase.startswith("discovery") else 12
    options = BulkOptions(job_path=tmp_path / "job.db", batch_size=3)
    with FixtureAPI(count) as api:
        if phase.startswith("discovery"):
            api.images = 0
        if kind == "jsonl":
            extract_people(tmp_path / "out", options=options)
            rows = [json.loads(line) for line in (tmp_path / "out").read_text(encoding="utf-8").splitlines()]
            ids = [r["qid"] for r in rows]
        elif kind == "csv":
            extract_people_to_csv(tmp_path / "csv", options=options)
            with (tmp_path / "csv/people.csv").open(newline="", encoding="utf-8") as handle:
                ids = [r["qid"] for r in csv.DictReader(handle)]
            with (tmp_path / "csv/person_images.csv").open(newline="", encoding="utf-8") as handle:
                assert sum(1 for _ in csv.DictReader(handle)) == count * 20
        else:
            extract_people_to_db("sqlite:///" + str(tmp_path / "output.db"), options=options)
            with contextlib.closing(sqlite3.connect(tmp_path / "output.db")) as db:
                ids = [r[0] for r in db.execute("SELECT qid FROM people")]
                assert db.execute("SELECT COUNT(*) FROM person_images").fetchone()[0] == count * 20
        if phase == "discovery_after_commit":
            assert api.calls["discovery"] == 1
    assert len(ids) == len(set(ids)) == count
    with contextlib.closing(sqlite3.connect(tmp_path / "job.db")) as db:
        state = json.loads(db.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0])
        assert state["completed"] and state["exported"] == count
