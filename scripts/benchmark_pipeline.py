import argparse
import contextlib
import csv
import ctypes
import hashlib
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.support import FixtureAPI
from wikidata_bulk_people import (
    BulkOptions, extract_people, extract_people_to_csv, extract_people_to_db,
    extract_people_to_memory, iter_people,
)
from wikidata_bulk_people._discovery import QIDStream
from wikidata_bulk_people import PeopleFilter


def rss():
    if sys.platform == "win32":
        class Counters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong),
                        ("peak", ctypes.c_size_t), ("working", ctypes.c_size_t),
                        ("poolpeak", ctypes.c_size_t), ("pool", ctypes.c_size_t),
                        ("nonpeak", ctypes.c_size_t), ("non", ctypes.c_size_t),
                        ("page", ctypes.c_size_t), ("pagepeak", ctypes.c_size_t)]
        value = Counters()
        value.cb = ctypes.sizeof(value)
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return value.working / 1024 ** 2
    if sys.platform == "linux":
        return int(pathlib.Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1024 ** 2
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 ** 2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--people", type=int, default=100000)
    parser.add_argument("--sink", choices=["jsonl", "csv", "database", "memory", "iterator", "discovery"], default="jsonl")
    parser.add_argument("--report", type=pathlib.Path, required=True)
    parser.add_argument("--interrupt-after", type=int)
    args = parser.parse_args()
    result = {"kind": "deterministic HTTP fixture; no network", "people": args.people,
              "sink": args.sink, "images_per_person": 20, "spouses_per_person": 2,
              "baseline_rss_mib": rss(), "interruption_after": args.interrupt_after}
    stopped = threading.Event()
    peaks = [rss()]
    def monitor():
        while not stopped.wait(0.1):
            peaks[0] = max(peaks[0], rss())
    sampler = threading.Thread(target=monitor, daemon=True)
    sampler.start()
    started = time.perf_counter()
    last_progress = [started]
    with tempfile.TemporaryDirectory(prefix="wbp-iteration-") as directory:
        root = pathlib.Path(directory)
        options = BulkOptions(job_path=root / "job.sqlite3")
        out = root / "out.jsonl"
        def run():
            if args.sink == "jsonl":
                extract_people(out, options=options)
            elif args.sink == "csv":
                extract_people_to_csv(root / "csv", options=options)
            elif args.sink == "database":
                extract_people_to_db("sqlite:///" + str(root / "output.db"), options=options)
            elif args.sink == "memory":
                return extract_people_to_memory(options=options)
            elif args.sink == "iterator":
                count = 0
                for person in iter_people(options=options):
                    assert len(person.images) == 20 and len(person.spouses) == 2
                    count += 1
                return count
            else:
                stream = QIDStream(PeopleFilter(), "benchmark")
                try:
                    return sum(1 for _ in stream)
                finally:
                    stream.close()
        from wikidata_bulk_people._extract import PersonExtractor
        original = PersonExtractor.extract_many
        def progress(self, qids):
            value = original(self, qids)
            now = time.perf_counter()
            if now - last_progress[0] >= 20:
                print(json.dumps({"elapsed": round(now-started, 1), "last_qid": qids[-1],
                                  "rss_mib": round(rss(), 1)}), flush=True)
                last_progress[0] = now
            return value
        from unittest.mock import patch
        with FixtureAPI(args.people) as api, patch.object(PersonExtractor, "extract_many", progress):
            if args.interrupt_after:
                from wikidata_bulk_people._sinks import JSONLSink
                from wikidata_bulk_people._sinks import CSVSink, DatabaseSink
                cls = {"jsonl": JSONLSink, "csv": CSVSink, "database": DatabaseSink}[args.sink]
                original_write = cls.write
                writes = [0]
                def interrupted(self, person):
                    original_write(self, person)
                    writes[0] += 1
                    if writes[0] == args.interrupt_after:
                        raise KeyboardInterrupt("benchmark interruption")
                with patch.object(cls, "write", interrupted):
                    try:
                        run()
                    except KeyboardInterrupt:
                        result["interrupted"] = True
            output = run()
            result["pipeline_seconds"] = time.perf_counter() - started
            result["requests"] = dict(api.calls)

            from wikidata_bulk_people._clients import _DEFAULT_THROTTLE
            by_host = {"www.wikidata.org": api.calls["entities"] + api.calls["labels"],
                       "en.wikipedia.org": api.calls["articles"] + api.calls["html"],
                       "commons.wikimedia.org": api.calls["commons"],
                       "query.wikidata.org": api.calls["discovery"]}
            result["public_throttle_floor_seconds"] = max(
                max(0, calls - 1) * _DEFAULT_THROTTLE[host] for host, calls in by_host.items())
            result["cold_live_target_verified"] = False
            result["output_bytes"] = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
            result["cache_bytes"] = sum(p.stat().st_size for p in root.glob("*.cache*"))
            if args.sink in ("iterator", "discovery"):
                count = output
            elif args.sink == "memory":
                count = len(output)
                assert all(len(p.images) == 20 and len(p.spouses) == 2 for p in output)
            elif args.sink == "jsonl":
                count = 0
                ids = sqlite3.connect(root / "verify.db")
                ids.execute("CREATE TABLE seen(qid TEXT PRIMARY KEY)")
                try:
                    with out.open(encoding="utf-8") as handle:
                        for line in handle:
                            person = json.loads(line)
                            assert 1000000 <= int(person["qid"][1:]) < 1000000 + args.people
                            assert len(person["images"]) == 20 and len(person["spouses"]) == 2
                            assert person["images"][19]["alt"] == "Portrait 19"
                            assert person["lastrevid"] == int(person["qid"][1:]) * 10
                            ids.execute("INSERT INTO seen VALUES(?)", (person["qid"],))
                            count += 1
                    ids.commit()
                finally:
                    ids.close()
                result["jsonl_bytes"] = out.stat().st_size
            elif args.sink == "csv":
                counts = {}
                for path in (root / "csv").glob("*.csv"):
                    with path.open(encoding="utf-8", newline="") as handle:
                        counts[path.stem] = sum(1 for _ in csv.DictReader(handle))
                count = counts["people"]
                assert counts["person_images"] == args.people * 20
                assert counts["person_spouses"] == counts["person_aliases"] == args.people * 2
                assert counts["person_occupations"] == args.people * 5
                with contextlib.closing(sqlite3.connect(root / "verify.db")) as verification:
                    verification.execute("CREATE TABLE seen(qid TEXT PRIMARY KEY)")
                    with (root / "csv/people.csv").open(encoding="utf-8", newline="") as handle:
                        for row in csv.DictReader(handle):
                            assert 1000000 <= int(row["qid"][1:]) < 1000000 + args.people
                            verification.execute("INSERT INTO seen VALUES(?)", (row["qid"],))
                    verification.commit()
                result["rows"] = counts
            else:
                with contextlib.closing(sqlite3.connect(root / "output.db")) as db:
                    count = db.execute("SELECT COUNT(*) FROM people").fetchone()[0]
                    assert db.execute("SELECT COUNT(*) FROM person_images").fetchone()[0] == args.people * 20
                    assert db.execute("SELECT MIN(n),MAX(n) FROM (SELECT COUNT(*) AS n FROM person_images GROUP BY person_qid)").fetchone() == (20, 20)
                    assert db.execute("SELECT MIN(qid),MAX(qid) FROM people").fetchone() == ("Q1000000", f"Q{1000000+args.people-1}")
                    assert json.loads(db.execute("SELECT value FROM pipeline_state WHERE key='state'").fetchone()[0])["completed"]
                    assert not db.execute("PRAGMA foreign_key_check").fetchall()
                    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert count == args.people, (count, args.people)
            result["verified_people"] = count
            if args.sink in ("jsonl", "csv", "database"):
                with contextlib.closing(sqlite3.connect(root / "job.sqlite3")) as db:
                    state = json.loads(db.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0])
                    assert state["completed"] and state["exported"] == state["discovered"] == count
                    result["state"] = {k: state[k] for k in ("completed", "enriched", "exported", "discovered")}
            result["verified"] = True
            result["total_seconds"] = time.perf_counter() - started
        del output
    stopped.set()
    sampler.join()
    result["peak_rss_mib"] = peaks[0]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
