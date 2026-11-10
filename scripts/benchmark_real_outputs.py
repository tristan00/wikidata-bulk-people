import argparse
import csv
import dataclasses
import hashlib
import json
import pathlib
import sqlite3
import threading
import time
from contextlib import closing
from unittest.mock import patch

from benchmark_pipeline import rss
from wikidata_bulk_people import (BulkOptions, extract_people, extract_people_to_csv,
                                 extract_people_to_db, extract_people_to_memory, iter_people)


def canonical(person):
    person = dict(person)
    person.pop("fetched_at")
    return json.dumps(person, sort_keys=True, ensure_ascii=False, default=str).encode() + b"\n"


def records(path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            yield json.loads(line)


def summarize(people):
    digest = hashlib.sha256()
    counts = dict.fromkeys(["people", "person_aliases", "person_citizenships", "person_occupations",
                           "person_spouses", "person_images"], 0)
    last = ""
    for person in people:
        assert person["qid"] > last, "Duplicate or unordered person"
        last = person["qid"]
        digest.update(canonical(person))
        counts["people"] += 1
        for name in counts:
            if name != "people":
                counts[name] += len(person[name.removeprefix("person_")])
    return digest.hexdigest(), counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=pathlib.Path, required=True)
    parser.add_argument("--public-snapshot", type=pathlib.Path, required=True)
    parser.add_argument("--directory", type=pathlib.Path, required=True)
    parser.add_argument("--interrupt-after", type=int, help="Interrupt and resume JSONL after this many writes")
    args = parser.parse_args()
    if args.directory.exists() and any(args.directory.iterdir()):
        parser.error("Use a fresh output directory; completed-job no-ops are not throughput measurements")
    args.directory.mkdir(parents=True, exist_ok=True)
    expected_hash, expected_counts = summarize(records(args.reference))
    target = expected_counts["people"]
    result = {"kind": "real data, prepared sources; acquisition excluded", "target": target,
              "reference_sha256_without_fetched_at": expected_hash, "counts": expected_counts, "outputs": {}}
    if args.interrupt_after is not None and not 0 < args.interrupt_after < target:
        parser.error("--interrupt-after must be between zero and the record count")
    def unexpected(*a, **kw):
        raise AssertionError("Prepared source unexpectedly attempted HTTP")
    for name in ("jsonl", "csv", "database", "iterator", "memory"):
        peak = [rss()]
        stop = threading.Event()
        def sample():
            while not stop.wait(.2):
                peak[0] = max(peak[0], rss())
        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        options = BulkOptions(limit=target, public_snapshot=args.public_snapshot,
                              job_path=args.directory / (name + ".job.sqlite3"))
        started = time.perf_counter()
        try:
            with patch("requests.Session.send", unexpected):
                if name == "jsonl":
                    path = args.directory / "people.jsonl"
                    if args.interrupt_after:
                        from wikidata_bulk_people._sinks import JSONLSink
                        original = JSONLSink.write
                        writes = [0]
                        def interrupted(self, person):
                            original(self, person)
                            writes[0] += 1
                            if writes[0] == args.interrupt_after:
                                raise KeyboardInterrupt("real-data output recovery check")
                        with patch.object(JSONLSink, "write", interrupted):
                            try:
                                extract_people(path, options=options)
                            except KeyboardInterrupt:
                                result["jsonl_interrupted_after"] = writes[0]
                        assert result.get("jsonl_interrupted_after") == args.interrupt_after
                    extract_people(path, options=options)
                    pipeline = time.perf_counter() - started
                    assert summarize(records(path)) == (expected_hash, expected_counts)
                elif name == "csv":
                    path = args.directory / "csv"
                    extract_people_to_csv(path, options=options)
                    pipeline = time.perf_counter() - started
                    for table, count in expected_counts.items():
                        with (path / (table + ".csv")).open(newline="", encoding="utf-8") as handle:
                            rows = csv.DictReader(handle)
                            assert sum(1 for _ in rows) == count, table
                    with (path / "people.csv").open(newline="", encoding="utf-8") as handle:
                        for actual, expected in zip(csv.DictReader(handle), records(args.reference), strict=True):
                            for field in ("qid", "name", "lead_paragraph", "wikipedia_title", "lastrevid"):
                                assert actual[field] == ("" if expected[field] is None else str(expected[field]))
                elif name == "database":
                    path = args.directory / "people.db"
                    extract_people_to_db("sqlite:///" + str(path.resolve()), options=options)
                    pipeline = time.perf_counter() - started
                    with closing(sqlite3.connect(path)) as db:
                        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
                        for table, count in expected_counts.items():
                            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count, table
                        actual = db.execute("SELECT qid,name,lead_paragraph,wikipedia_title,lastrevid FROM people ORDER BY qid")
                        for row, expected in zip(actual, records(args.reference), strict=True):
                            assert row == tuple(expected[k] for k in ("qid", "name", "lead_paragraph", "wikipedia_title", "lastrevid"))
                else:
                    people = (extract_people_to_memory(options=options) if name == "memory"
                              else iter_people(options=options))
                    assert summarize(dataclasses.asdict(p) for p in people) == (expected_hash, expected_counts)
                    pipeline = time.perf_counter() - started
                    del people
        finally:
            stop.set()
            sampler.join()
        result["outputs"][name] = {"pipeline_seconds": pipeline, "total_seconds": time.perf_counter() - started,
                                   "sampled_peak_rss_mib": peak[0], "verified": True, "http_requests": 0}
        (args.directory / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps({"output": name, **result["outputs"][name]}), flush=True)


if __name__ == "__main__":
    main()
