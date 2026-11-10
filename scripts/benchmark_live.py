import argparse
import dataclasses
import hashlib
import collections
import json
import logging
import pathlib
import sqlite3
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import requests

from wikidata_bulk_people import BulkOptions, PeopleFilter, TransportError, extract_people


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=pathlib.Path, required=True)
    parser.add_argument("--people", type=int, default=100000)
    parser.add_argument("--retry-seconds", type=float, default=60)
    parser.add_argument("--retry-budget", type=float, default=3600)
    parser.add_argument("--public-snapshot", type=pathlib.Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    metadata = args.directory / "run.json"
    if metadata.exists():
        report = json.loads(metadata.read_text())
        if report["target"] != args.people:
            raise ValueError("Cannot change an existing run's target")
    else:
        if any((args.directory / name).exists() for name in ("job.sqlite3", "cache.sqlite3", "people.jsonl")):
            parser.error("Existing data has no benchmark clock; use a fresh run directory")
        report = {"kind": "published public snapshots with API enrichment" if args.public_snapshot else "live Wikimedia public APIs", "target": args.people,
                  "started_at": datetime.now(timezone.utc).isoformat(),
                  "started_epoch": time.time(), "initial_cache": "empty"}
        if args.public_snapshot:
            report["source_directory_initially_exists"] = args.public_snapshot.exists()
        metadata.write_text(json.dumps(report, indent=2))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(args.directory / "run.log", encoding="utf-8")])
    lock = threading.Lock()
    metrics = collections.defaultdict(lambda: {"requests": 0, "seconds": 0., "buffered_body_bytes": 0,
                                                "streamed_requests": 0,
                                                "statuses": {}})
    original = requests.Session.send
    def measured(session, request, **kwargs):
        started = time.perf_counter()
        parsed = urlparse(request.url)
        query = parse_qs(parsed.query)
        key = parsed.netloc + ":" + query.get("action", [parsed.path.split("/")[1]])[0]
        response = None
        try:
            response = original(session, request, **kwargs)
            return response
        finally:
            elapsed = time.perf_counter() - started
            with lock:
                entry = metrics[key]
                entry["requests"] += 1
                entry["seconds"] += elapsed
                status = str(response.status_code) if response is not None else "exception"
                entry["statuses"][status] = entry["statuses"].get(status, 0) + 1
                streamed = bool(kwargs.get("stream", False))
                entry["streamed_requests"] += int(streamed)


                if response is not None and not streamed:
                    entry["buffered_body_bytes"] += len(response.content)
            if elapsed > 5:
                logging.info("HTTP %s %.2fs status=%s", key, elapsed, status)
    requests.Session.send = measured
    stop = threading.Event()
    job_path = args.directory / "job.sqlite3"
    def snapshot():
        with lock:
            report["http_this_invocation"] = json.loads(json.dumps(metrics))
        report["elapsed_since_cold_start_seconds"] = time.time() - report["started_epoch"]
        if job_path.exists():
            try:
                with sqlite3.connect(f"file:{job_path.as_posix()}?mode=ro", uri=True) as db:
                    row = db.execute("SELECT value FROM meta WHERE key='state'").fetchone()
                    if row:
                        report["state"] = json.loads(row[0])
                    row = db.execute("SELECT value FROM meta WHERE key='source_provenance'").fetchone()
                    if row:
                        report["sources"] = json.loads(row[0])
            except sqlite3.Error:
                pass
        if args.public_snapshot:
            for filename in ("wikidata.json", "prepared.json"):
                path = args.public_snapshot / filename
                if path.exists():
                    report[filename] = json.loads(path.read_text())
        temporary = metadata.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
        temporary.replace(metadata)
    def monitor():
        while not stop.wait(30):
            snapshot()
            logging.info("LIVE %s", json.dumps({"elapsed": report["elapsed_since_cold_start_seconds"],
                                                "state": report.get("state")}))
    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    try:
        retry_started = time.monotonic()
        while True:
            try:
                option_args = {"limit": args.people, "job_path": job_path,
                               "cache_path": args.directory / "cache.sqlite3"}
                if args.public_snapshot:
                    option_args["public_snapshot"] = args.public_snapshot
                extract_people(args.directory / "people.jsonl", filter=PeopleFilter(),
                               options=BulkOptions(**option_args))
                break
            except TransportError as exc:
                report["last_error"] = str(exc)
                report["transport_failures"] = report.get("transport_failures", 0) + 1
                if time.monotonic() - retry_started >= args.retry_budget:
                    raise
                logging.warning("Resuming durable job in %.1fs after transport failure: %s",
                                args.retry_seconds, exc)
                time.sleep(args.retry_seconds)
        from wikidata_bulk_people import Person, ImageRef, SpouseRecord
        count = images = spouses = 0
        populated = collections.Counter()
        digest = hashlib.sha256()
        seen = set()
        with (args.directory / "people.jsonl").open(encoding="utf-8") as handle:
            for line in handle:
                person = json.loads(line)
                if set(person) != {f.name for f in dataclasses.fields(Person)}:
                    raise AssertionError("Person schema changed or lost fields")
                for key, cls in (("images", ImageRef), ("spouses", SpouseRecord)):
                    expected = {f.name for f in dataclasses.fields(cls)}
                    if any(set(value) != expected for value in person[key]):
                        raise AssertionError(f"Incomplete {key} schema")
                if person["qid"] in seen:
                    raise AssertionError("Duplicate QID")
                seen.add(person["qid"])
                count += 1
                images += len(person["images"])
                spouses += len(person["spouses"])
                populated.update(k for k, value in person.items() if value)
                digest.update((person["qid"] + "\n").encode())
        report["verified"] = {"unique_people": count, "images": images, "spouses": spouses,
                              "populated_fields": dict(populated), "ordered_qids_sha256": digest.hexdigest()}
        if args.public_snapshot:
            from wikidata_bulk_people._snapshot import iter_qids
            expected = hashlib.sha256()
            for qid in iter_qids(args.public_snapshot):
                expected.update((qid + "\n").encode())
            if expected.digest() != digest.digest():
                raise AssertionError("Output membership differs from selected source entities")
        if count != args.people:
            raise AssertionError(f"Only {count} of {args.people} people")
        report["completed"] = True
    except BaseException as exc:
        report["completed"] = False
        report["last_error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        stop.set()
        watcher.join()
        snapshot()
        requests.Session.send = original


if __name__ == "__main__":
    main()
