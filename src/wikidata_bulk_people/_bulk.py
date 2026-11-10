import dataclasses
import hashlib
import itertools
import json
import logging
import os
import pathlib
import sqlite3
import time
import uuid
from collections.abc import Generator
from datetime import datetime
from typing import Any

from wikidata_bulk_people._cache import ObjectCache
from wikidata_bulk_people._discovery import QIDStream, _validate_filter
from wikidata_bulk_people._storage import job_lock, write_json
from wikidata_bulk_people._models import (
    BulkOptions, DateValue, ExtractionError, ImageRef, IncompleteRunError,
    PeopleFilter, Person, SpouseRecord,
)

logger = logging.getLogger("wikidata_bulk_people")
_JOB_FORMAT = 2


def _encode(person: Person) -> str:
    return json.dumps(dataclasses.asdict(person), default=str, ensure_ascii=False)


def _decode(body: str) -> Person:
    data = json.loads(body)
    for key in ("date_of_birth", "date_of_death"):
        if data[key] is not None:
            data[key] = DateValue(**data[key])
    for spouse in data["spouses"]:
        for key in ("start_date", "end_date"):
            if spouse[key] is not None:
                spouse[key] = DateValue(**spouse[key])
    data["spouses"] = [SpouseRecord(**s) for s in data["spouses"]]
    data["images"] = [ImageRef(**i) for i in data["images"]]
    data["fetched_at"] = datetime.fromisoformat(data["fetched_at"])
    return Person(**data)


class JobStore:

    def __init__(self, path: pathlib.Path, config: dict[str, Any], resume: bool) -> None:
        self.resume_requested = resume
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS work(
                seq INTEGER PRIMARY KEY, qid TEXT NOT NULL UNIQUE,
                person TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS work_ready ON work(seq) WHERE person IS NOT NULL;
            CREATE INDEX IF NOT EXISTS work_failed ON work(seq) WHERE error IS NOT NULL;
            CREATE INDEX IF NOT EXISTS work_unfinished ON work(seq) WHERE person IS NULL;
            CREATE INDEX IF NOT EXISTS work_pending ON work(seq)
                WHERE person IS NULL AND error IS NULL;
        """)
        self.state = self.get("state", {})
        if self.state and resume:
            if self.get("config") != config:
                self.db.close()
                raise ValueError("Resume configuration changed. Use a new destination/job or resume=False.")
        else:
            self.state = {"format": _JOB_FORMAT, "job_id": uuid.uuid4().hex,
                          "completed": False, "discovery_complete": False, "discovered": 0,
                          "enriched": 0, "exported": 0, "export_seq": 0,
                          "last_qid": None, "yp_year": None, "started_at": time.time()}
            with self.db:
                self.db.execute("DELETE FROM work")
                self.db.execute("DELETE FROM meta")
                self.set("config", config)
                self.set("state", self.state)

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))

    def save(self) -> None:
        with self.db:
            self.set("state", self.state)

    def close(self) -> None:
        self.db.close()


def _file_paths(sink: Any) -> list[pathlib.Path]:
    from wikidata_bulk_people._sinks import JSONLSink
    from wikidata_bulk_people._sinks import CSVSink, TABLE_FIELDS
    if isinstance(sink, JSONLSink):
        return [sink._path]
    if isinstance(sink, CSVSink):
        return [sink._dir / f"{name}.csv" for name in TABLE_FIELDS]
    return []


def _snapshot(path: pathlib.Path, size: int | None = None) -> dict[str, Any]:
    size = path.stat().st_size if size is None else size
    with path.open("rb") as handle:
        handle.seek(max(0, size - 4096))
        tail = handle.read(min(size, 4096))
    return {"size": size, "tail": hashlib.sha256(tail).hexdigest()}


def _restore_files(files: list[pathlib.Path], offsets: list[dict[str, Any]],
                   *, truncate: bool) -> None:
    if len(files) != len(offsets):
        raise ValueError("Output file manifest does not match destination")

    for path, saved in zip(files, offsets):
        if not path.exists():
            if saved["size"] == 0:
                continue
            raise ValueError(f"Committed output is missing: {path}")
        if path.stat().st_size < saved["size"] or _snapshot(path, saved["size"]) != saved:
            raise ValueError(f"Committed output was changed or truncated: {path}")
    if truncate:
        for path, saved in zip(files, offsets):
            if path.exists() and path.stat().st_size != saved["size"]:
                with path.open("r+b") as handle:
                    handle.truncate(saved["size"])
                    handle.flush()
                    os.fsync(handle.fileno())


def _flush_files(sink: Any, files: list[pathlib.Path]) -> list[dict[str, Any]]:
    handles = getattr(sink, "_open_files", None)
    if handles is None:
        handles = [sink._file] if files else []
    for handle in handles:
        handle.flush()
        os.fsync(handle.fileno())
    return [_snapshot(path) for path in files]


def _destination(sink: Any) -> str:
    from wikidata_bulk_people._sinks import DatabaseSink
    if isinstance(sink, DatabaseSink):

        from sqlalchemy.engine import make_url
        url = make_url(sink._connection_string)
        if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
            url = url.set(database=str(pathlib.Path(url.database).resolve()))
        return "database:" + hashlib.sha256(
            (url.render_as_string(hide_password=False) + "|" + sink._prefix).encode()).hexdigest()
    return "|".join(str(p.resolve()) for p in _file_paths(sink))


def _discover(job: JobStore, filter: PeopleFilter, user_agent: str, limit: int | None) -> None:
    if job.state["discovery_complete"]:
        return
    if limit is not None and job.state["discovered"] >= limit:
        job.state["discovery_complete"] = True
        job.save()
        return
    stream = QIDStream(filter=filter, user_agent=user_agent)
    try:
        if limit != 0:
            snapshot = (limit is not None and 1000 <= limit <= 100000
                        and not filter.year_partition and not job.state["discovered"])
            pages = (stream.iter_pages(snapshot_limit=limit) if snapshot else
                     stream.iter_pages(job.state["last_qid"], job.state["yp_year"]))
            for page in pages:
                next_state = dict(job.state)
                with job.db:
                    for qid in page.qids:
                        if limit is not None and next_state["discovered"] >= limit:
                            break
                        added = job.db.execute("INSERT OR IGNORE INTO work(qid) VALUES(?)", (qid,)).rowcount
                        next_state["discovered"] += added
                    next_state.update(last_qid=page.last_qid, yp_year=page.bucket)
                    if snapshot:
                        next_state["discovery_complete"] = True
                        next_state["discovery_method"] = "bounded_snapshot"
                    job.set("state", next_state)
                job.state = next_state
                logger.info("Discovery: %d distinct people; partition=%s cursor=%s",
                            job.state["discovered"], page.bucket, page.last_qid)
                if limit is not None and job.state["discovered"] >= limit:
                    break
        job.state["discovery_complete"] = True
        job.save()
    finally:
        stream.close()


def _status(job: JobStore, state_path: pathlib.Path | None) -> None:
    job.state["failed"] = job.db.execute("SELECT COUNT(*) FROM work WHERE error IS NOT NULL").fetchone()[0]
    job.state["elapsed_seconds"] = time.time() - job.state["started_at"]
    job.save()
    if state_path is not None:
        write_json(state_path, job.state)


def _export(job: JobStore, sink: Any, files: list[pathlib.Path], batch_size: int) -> None:
    from wikidata_bulk_people._sinks import DatabaseSink
    if isinstance(sink, DatabaseSink):
        batch_size = min(batch_size, sink._batch_size)
    while True:
        rows = job.db.execute(
            "SELECT seq,person FROM work WHERE seq>? AND person IS NOT NULL ORDER BY seq LIMIT ?",
            (job.state["export_seq"], batch_size)).fetchall()
        if not rows:
            return

        pending = job.db.execute("SELECT MIN(seq) FROM work WHERE seq>? AND person IS NULL",
                                 (job.state["export_seq"],)).fetchone()[0]
        if pending is not None:
            rows = [r for r in rows if r[0] < pending]
        if not rows:
            return
        for _, body in rows:
            sink.write(_decode(body))
        next_state = {**job.state, "export_seq": rows[-1][0],
                      "exported": job.state["exported"] + len(rows)}
        if isinstance(sink, DatabaseSink):
            sink.write_pipeline_state(next_state)
        else:
            next_state["files"] = _flush_files(sink, files)
        job.state = next_state
        job.save()


def _run(job: JobStore, sink: Any, filter: PeopleFilter, user_agent: str,
         options: BulkOptions, state_path: pathlib.Path | None, cache_path: pathlib.Path) -> None:
    from wikidata_bulk_people._extract import PersonExtractor
    from wikidata_bulk_people._sinks import JSONLSink
    from wikidata_bulk_people._sinks import CSVSink, DatabaseSink
    files = _file_paths(sink)
    if "files" in job.state:
        _restore_files(files, job.state["files"], truncate=not job.state["completed"])
        if isinstance(sink, (CSVSink, JSONLSink)):
            sink._if_exists = "append"
    elif files:
        if getattr(sink, "_if_exists", None) == "fail" and any(p.exists() for p in files):
            raise FileExistsError("Output already exists; use append/replace or a different destination")
        replace = getattr(sink, "_if_exists", None) == "replace"
        empty = {"size": 0, "tail": hashlib.sha256(b"").hexdigest()}
        job.state["files"] = [_snapshot(p) if p.exists() and not replace else empty for p in files]
        job.save()
    if isinstance(sink, DatabaseSink):
        sink._managed = True
        if job.state.get("database_opened"):
            sink._if_exists = "upsert" if sink._if_exists == "upsert" else "append"
        elif job.state.get("database_initializing"):
            sink._if_exists = "replace" if job.state["initial_policy"] == "replace" else "append"
        else:
            sink.validate_initial()
            job.state["database_initializing"] = True
            job.state["initial_policy"] = sink._if_exists
            job.save()
    elif job.state["completed"]:
        return
    cache = ObjectCache(cache_path, ttl=options.cache_ttl)
    extractor = PersonExtractor(user_agent=user_agent, cache=cache, workers=options.workers)
    try:
        with sink:
            if isinstance(sink, DatabaseSink):
                saved = sink.read_pipeline_state()
                if saved and saved.get("job_id") == job.state["job_id"]:
                    job.state["export_seq"] = saved["export_seq"]
                    job.state["exported"] = saved["exported"]
                elif job.state["exported"] or (saved and job.resume_requested):
                    raise ValueError("Database output state is missing or belongs to another job")
                job.state["database_opened"] = True
                job.save()
                if job.state["completed"]:
                    return
            if options.public_snapshot is not None:
                from wikidata_bulk_people._snapshot import SnapshotCache, iter_qids, prepare
                source_directory = pathlib.Path(options.public_snapshot)
                assert options.limit is not None
                provenance = prepare(source_directory, filter, options.limit, user_agent)
                previous_source = job.get("source_provenance")
                if previous_source is not None and previous_source != provenance:
                    raise ValueError("Prepared source revisions changed; use a new job and destination")
                job.set("source_provenance", provenance)
                extractor.close()
                cache.close()
                cache = SnapshotCache(source_directory, cache_path, options.cache_ttl)
                extractor = PersonExtractor(user_agent=user_agent, cache=cache, workers=options.workers)
                if not job.state["discovery_complete"]:
                    next_state = dict(job.state)
                    with job.db:
                        job.db.executemany("INSERT OR IGNORE INTO work(qid) VALUES(?)",
                                           ((q,) for q in iter_qids(source_directory)))
                        next_state.update(discovered=job.db.execute("SELECT COUNT(*) FROM work").fetchone()[0],
                                          discovery_complete=True, discovery_method="public_snapshot")
                        job.set("state", next_state)
                    job.state = next_state
            else:
                _discover(job, filter, user_agent, options.limit)

            with job.db:
                job.db.execute("UPDATE work SET error=NULL WHERE error IS NOT NULL")
            _export(job, sink, files, options.batch_size)
            while True:
                qids = [r[0] for r in job.db.execute(
                    "SELECT qid FROM work WHERE person IS NULL AND error IS NULL ORDER BY seq LIMIT ?",
                    (options.batch_size,))]
                if not qids:
                    break
                with job.db:
                    job.db.executemany("UPDATE work SET attempts=attempts+1 WHERE qid=?", ((q,) for q in qids))
                results = extractor.extract_many(qids)
                if set(results) != set(qids):
                    raise ValueError("Extractor returned an incomplete batch")
                next_state = dict(job.state)
                with job.db:
                    for qid in qids:
                        result = results[qid]
                        if isinstance(result, ExtractionError):
                            job.db.execute("UPDATE work SET error=? WHERE qid=?",
                                           (str(result), qid))
                        else:
                            job.db.execute("UPDATE work SET person=? WHERE qid=?",
                                           (_encode(result), qid))
                            next_state["enriched"] += 1
                    job.set("state", next_state)
                job.state = next_state
                _export(job, sink, files, options.batch_size)
                job.state["cache_hits"] = cache.hits
                job.state["cache_misses"] = cache.misses
                job.state["cache_ttl_seconds"] = options.cache_ttl
                _status(job, state_path)
                logger.info("Enriched=%d exported=%d failed=%d of %d",
                            job.state["enriched"], job.state["exported"],
                            job.state["failed"], job.state["discovered"])
            _status(job, state_path)
            if job.state["failed"]:
                raise IncompleteRunError(
                    f"{job.state['failed']} record(s) failed; inspect work.error in the job manifest and resume")
            _export(job, sink, files, options.batch_size)
            if job.state["exported"] != job.state["discovered"]:
                raise IncompleteRunError("Not all discovered people were exported")

            if files:
                job.state["files"] = _flush_files(sink, files)
                job.save()

        completed_state = {**job.state, "completed": True}
        completed_state.pop("last_error", None)
        if isinstance(sink, DatabaseSink):
            sink.finalize_pipeline_state(completed_state)
        job.state = completed_state
        _status(job, state_path)
    except BaseException as exc:
        job.state["last_error"] = f"{type(exc).__name__}: {exc}"
        _status(job, state_path)
        raise
    finally:
        extractor.close()
        cache.close()


def _validate_selection(filter: PeopleFilter, options: BulkOptions) -> None:
    _validate_filter(filter)
    if options.public_snapshot is not None and filter.year_partition:
        raise ValueError("year_partition only applies to live API discovery; omit it for public snapshots")


def run_job(sink: Any, *, filter: PeopleFilter, resume: bool, user_agent: str,
            state_path: pathlib.Path | None, options: BulkOptions | None = None) -> None:
    from wikidata_bulk_people._sinks import DatabaseSink
    from wikidata_bulk_people import __version__
    options = options or BulkOptions()
    _validate_selection(filter, options)
    destination = _destination(sink)
    if options.job_path is not None:
        job_path = pathlib.Path(options.job_path)
    elif state_path is not None:
        job_path = pathlib.Path(str(state_path) + ".job.sqlite3")
    else:
        job_path = pathlib.Path(".wikidata-bulk-people/jobs") / (destination.split(":")[-1] + ".sqlite3")
    job_path.parent.mkdir(parents=True, exist_ok=True)
    if resume and not job_path.exists() and state_path is not None and state_path.exists():
        raise ValueError("Legacy state cannot guarantee output integrity. Use a new output or resume=False.")
    config = {"format": _JOB_FORMAT, "filter": dataclasses.asdict(filter), "limit": options.limit,
              "destination": destination, "extractor_version": __version__, "schema_version": "1"}
    cache_path = pathlib.Path(options.cache_path) if options.cache_path else pathlib.Path(str(job_path) + ".cache")
    if options.public_snapshot is not None:
        config["public_snapshot"] = str(pathlib.Path(options.public_snapshot).resolve())
    protected = {p.resolve() for p in _file_paths(sink)}
    if isinstance(sink, DatabaseSink):
        from sqlalchemy.engine import make_url
        url = make_url(sink._connection_string)
        if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
            protected.add(pathlib.Path(url.database).resolve())
    if state_path is not None:
        protected.add(state_path.resolve())
    if job_path.resolve() in protected or cache_path.resolve() in protected or cache_path.resolve() == job_path.resolve():
        raise ValueError("Output, state, job and cache paths must be distinct")
    if options.public_snapshot is not None:
        source_dir = pathlib.Path(options.public_snapshot).resolve()
        if any(p == source_dir or source_dir in p.parents for p in protected | {job_path.resolve(), cache_path.resolve()}):
            raise ValueError("The public snapshot directory must be separate from output, job and live cache paths")
    with job_lock(pathlib.Path(str(job_path) + ".lock")):
        job = JobStore(job_path, config, resume)
        try:
            _run(job, sink, filter, user_agent, options, state_path, cache_path)
        finally:
            job.close()


def iter_records(*, filter: PeopleFilter, user_agent: str,
                 options: BulkOptions | None = None) -> Generator[Person]:
    from wikidata_bulk_people._extract import PersonExtractor
    options = options or BulkOptions()
    _validate_selection(filter, options)
    live_cache = pathlib.Path(options.cache_path) if options.cache_path else None
    cache: ObjectCache
    if options.public_snapshot is not None:
        from wikidata_bulk_people._snapshot import SnapshotCache, iter_qids, prepare
        directory = pathlib.Path(options.public_snapshot)
        assert options.limit is not None
        if live_cache is not None and (directory.resolve() == live_cache.resolve()
                                      or directory.resolve() in live_cache.resolve().parents):
            raise ValueError("The live cache must be outside the public snapshot directory")
        prepare(directory, filter, options.limit, user_agent)
        cache = SnapshotCache(directory, live_cache, options.cache_ttl)
    else:
        cache = ObjectCache(live_cache, ttl=options.cache_ttl)
    extractor = PersonExtractor(user_agent, cache=cache, workers=options.workers)
    stream = QIDStream(filter, user_agent)
    if options.public_snapshot is not None:
        source_qids = iter_qids(pathlib.Path(options.public_snapshot))
    elif options.limit is not None and 1000 <= options.limit <= 100000 and not filter.year_partition:
        source_qids = (qid for page in stream.iter_pages(snapshot_limit=options.limit) for qid in page.qids)
    else:
        source_qids = iter(stream)
    try:
        qids = itertools.islice(source_qids, options.limit)
        while True:
            batch = list(itertools.islice(qids, options.batch_size))
            if not batch:
                break
            for result in extractor.extract_many(batch).values():
                if isinstance(result, ExtractionError):
                    raise result
                yield result
    finally:
        source_qids.close()
        stream.close()
        extractor.close()
        cache.close()
