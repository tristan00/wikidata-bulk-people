import dataclasses
import gzip
import hashlib
import json
import logging
import pathlib
import re
import sqlite3
import time
from collections.abc import Generator
from contextlib import closing
from concurrent.futures import FIRST_COMPLETED, CancelledError, Future, ThreadPoolExecutor, wait
from itertools import islice
from threading import Event
from typing import Any

import requests
from urllib3.exceptions import HTTPError as StreamHTTPError

from wikidata_bulk_people._cache import ObjectCache
from wikidata_bulk_people._storage import job_lock, write_json as _write
from wikidata_bulk_people._clients import CommonsClient, RestClient, WikipediaClient, _extract_filename, _file_key, _retry_after
from wikidata_bulk_people._discovery import _SparqlClient, _validate_filter
from wikidata_bulk_people._models import PeopleFilter, TransportError
from wikidata_bulk_people._util import _best_claims, _claim_values, _looks_like_chrome, _parse_time

logger = logging.getLogger("wikidata_bulk_people")
_DUMP = "https://dumps.wikimedia.org/wikidatawiki/entities/latest-all.json.gz"
_REPO = "https://huggingface.co/api/datasets/wikimedia/structured-wikipedia"
_RESOLVE = "https://huggingface.co/datasets/wikimedia/structured-wikipedia/resolve"
_PROPERTIES = {"P31", "P569", "P570", "P19", "P20", "P21", "P27", "P106", "P26"}


def _read(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _check_cancelled(stop: Event | None) -> None:
    if stop is not None and stop.is_set():
        raise CancelledError("Snapshot preparation cancelled")


def _pause(delay: float, stop: Event | None) -> None:
    if stop is None:
        time.sleep(delay)
    else:
        stop.wait(delay)
        _check_cancelled(stop)


def _cancel_on_failure(future: Future[Any], stop: Event) -> None:
    if future.cancelled() or future.exception() is not None:
        stop.set()


def _request(url: str, user_agent: str, *, headers: dict[str, str] | None = None,
             stream: bool = False, stop: Event | None = None) -> requests.Response:
    last: Exception | None = None
    for attempt in range(5):
        _check_cancelled(stop)
        delay = 2.0 ** attempt
        try:
            response = requests.get(url, headers={"User-Agent": user_agent, **(headers or {})},
                                    stream=stream, timeout=(10, 65))
            if stop is not None and stop.is_set():
                response.close()
                _check_cancelled(stop)
            if response.status_code in (200, 206):
                return response
            delay = _retry_after(response.headers.get("Retry-After"), delay)
            code = response.status_code
            response.close()
            if code != 429 and code < 500:
                raise TransportError(url, code)
            last = TransportError(url, code)
        except requests.RequestException as exc:
            last = exc
        if attempt < 4:
            _pause(delay, stop)
    raise TransportError(url, reason=f"Source download failed: {last}") from last


def _json(url: str, user_agent: str, stop: Event | None = None) -> Any:
    with _request(url, user_agent, stop=stop) as response:
        return response.json()


def matches(entity: dict[str, Any], filter: PeopleFilter) -> bool:
    def ids(pid: str) -> set[str]:
        return {v["id"] for v in _claim_values(entity, pid) if isinstance(v, dict) and "id" in v}
    if "Q5" not in ids("P31"):
        return False
    if filter.has_wikipedia_article and not entity.get("sitelinks", {}).get("enwiki"):
        return False
    for name, pid in (("occupation_qid", "P106"), ("citizenship_qid", "P27"), ("gender_qid", "P21"),
                      ("religion_qid", "P140"), ("award_qid", "P166"), ("political_ideology_qid", "P1142")):
        value = getattr(filter, name)
        if value is not None and value not in ids(pid):
            return False
    if filter.born_after is not None or filter.born_before is not None:
        dates = [_parse_time(v) for v in _claim_values(entity, "P569") if isinstance(v, dict)]
        if not any(d is not None and (filter.born_after is None or d.year >= filter.born_after)
                   and (filter.born_before is None or d.year <= filter.born_before) for d in dates):
            return False
    death = any(c.get("mainsnak", {}).get("snaktype") != "novalue" for c in _best_claims(entity, "P570"))
    return filter.living is None or death != filter.living


def _project(entity: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {k: entity[k] for k in ("id", "lastrevid", "type") if k in entity}
    for key in ("labels", "aliases", "descriptions"):
        result[key] = {"en": entity[key]["en"]} if "en" in entity.get(key, {}) else {}
    result["sitelinks"] = {"enwiki": entity["sitelinks"]["enwiki"]} if "enwiki" in entity.get("sitelinks", {}) else {}
    result["claims"] = {p: [{k: v for k, v in c.items() if k != "references"} for c in cs]
                        for p, cs in entity.get("claims", {}).items() if p in _PROPERTIES}
    return result


def _dump_lines(response: requests.Response) -> Generator[bytes]:
    try:
        with gzip.GzipFile(fileobj=response.raw) as source:
            yield from source
    except (EOFError, OSError, StreamHTTPError) as exc:
        raise TransportError(_DUMP, reason=f"Interrupted compressed source stream: {exc}") from exc


def acquire_entities(directory: pathlib.Path, filter: PeopleFilter, limit: int, user_agent: str,
                     stop: Event | None = None) -> dict[str, Any]:
    _check_cancelled(stop)
    meta_path = directory / "wikidata.json"
    meta: dict[str, Any] = _read(meta_path) if meta_path.exists() else {"started_epoch": time.time(), "source": _DUMP}
    db = sqlite3.connect(directory / "entities.sqlite3")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("CREATE TABLE IF NOT EXISTS entities(qid TEXT PRIMARY KEY, title TEXT, body TEXT NOT NULL);"
                     "CREATE INDEX IF NOT EXISTS entity_title ON entities(title);"
                     "CREATE TABLE IF NOT EXISTS labels(qid TEXT PRIMARY KEY, value TEXT NOT NULL);")
    try:
        count = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        if meta.get("complete"):
            if count != meta["selected"]:
                raise ValueError("Snapshot entity inventory is incomplete")
            return dict(meta)
        with _request(_DUMP, user_agent, stream=True, stop=stop) as response:
            etag = response.headers.get("ETag")
            if not etag:
                raise TransportError(_DUMP, reason="Source has no revision ETag")
            if meta.get("etag") and meta["etag"] != etag:
                raise ValueError("The Wikidata dump changed during acquisition; use a new source directory")
            meta.update(etag=etag, last_modified=response.headers.get("Last-Modified"), complete=False)
            _write(meta_path, meta)
            scanned, last_progress = 0, time.monotonic()
            for line in _dump_lines(response):
                _check_cancelled(stop)
                line = line.strip().rstrip(b",")
                if line in (b"[", b"]"):
                    continue
                entity = json.loads(line)
                scanned += 1
                qid = entity.get("id", "")
                if not re.fullmatch(r"Q[1-9][0-9]*", qid):
                    continue
                label = entity.get("labels", {}).get("en", {}).get("value")
                if label is not None:
                    db.execute("INSERT OR REPLACE INTO labels VALUES(?,?)", (qid, label))
                if matches(entity, filter):
                    title = entity.get("sitelinks", {}).get("enwiki", {}).get("title")
                    count += db.execute("INSERT OR IGNORE INTO entities VALUES(?,?,?)",
                                        (qid, title, json.dumps(_project(entity)))).rowcount
                if scanned % 1000 == 0:
                    db.commit()
                if time.monotonic() - last_progress > 30:
                    logger.info("Snapshot selection: %d/%d people; %d entities scanned", count, limit, scanned)
                    last_progress = time.monotonic()
                if count >= limit:
                    break
            _check_cancelled(stop)
            db.commit()
            meta.update(selected=count, scanned_this_attempt=scanned, compressed_bytes_this_attempt=response.raw.tell(),
                        complete=True, elapsed_seconds=time.time() - meta["started_epoch"])
            _write(meta_path, meta)
            return dict(meta)
    finally:
        db.close()


def _download(entry: dict[str, Any], revision: str, directory: pathlib.Path, user_agent: str,
              stop: Event | None = None) -> None:
    _check_cancelled(stop)
    name = pathlib.PurePosixPath(entry["path"]).name
    target = directory / name
    digest = entry["lfs"]["oid"]
    def checksum(path: pathlib.Path) -> str:
        sha = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                _check_cancelled(stop)
                sha.update(block)
        return sha.hexdigest()
    if target.exists():
        if target.stat().st_size != entry["size"] or checksum(target) != digest:
            raise ValueError(f"Corrupt snapshot file: {target}")
        return
    partial = target.with_suffix(".partial")
    url = f"{_RESOLVE}/{revision}/{entry['path']}"
    for attempt in range(5):
        _check_cancelled(stop)
        offset = partial.stat().st_size if partial.exists() else 0
        if offset == entry["size"]:
            break
        if offset > entry["size"]:
            raise ValueError(f"Oversized partial snapshot file: {partial}")
        try:
            with _request(url, user_agent, headers={"Range": f"bytes={offset}-"} if offset else None,
                          stream=True, stop=stop) as response:
                if offset and (response.status_code != 206 or not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-")):
                    raise TransportError(url, reason="Source did not honor the download resume offset")
                with partial.open("ab") as handle:
                    for block in response.iter_content(1024 * 1024):
                        _check_cancelled(stop)
                        handle.write(block)
            break
        except requests.RequestException:
            if attempt == 4:
                raise
            _pause(2 ** attempt, stop)
    if partial.stat().st_size != entry["size"] or checksum(partial) != digest:
        raise ValueError(f"Snapshot checksum mismatch: {name}")
    _check_cancelled(stop)
    partial.replace(target)


def acquire_articles(directory: pathlib.Path, user_agent: str,
                     stop: Event | None = None) -> dict[str, Any]:
    stop = stop if stop is not None else Event()
    _check_cancelled(stop)
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        manifest = _read(manifest_path)
    else:
        revision = _json(_REPO, user_agent, stop)["sha"]
        with _request(f"{_REPO}/tree/{revision}/enwiki/data", user_agent, stop=stop) as response:
            if response.links.get("next"):
                raise ValueError("The snapshot listing is paginated; refusing incomplete acquisition")
            entries = response.json()
        if not entries or any(e.get("type") != "file" or not e["path"].endswith(".parquet") for e in entries):
            raise ValueError("Unexpected article snapshot inventory")
        with _request(f"{_RESOLVE}/{revision}/README.md", user_agent, stop=stop) as response:
            readme = response.text
        date = re.search(r"Dataset extracted:\s*([^\r\n]+)", readme)
        manifest = {"source": "wikimedia/structured-wikipedia", "revision": revision, "files": entries,
                    "dataset_date": date[1].strip() if date else "unspecified by publisher",
                    "started_epoch": time.time()}
        _write(manifest_path, manifest)
    with ThreadPoolExecutor(max_workers=3) as pool:
        entries = iter(manifest["files"])
        futures = set()
        try:
            for entry in islice(entries, 3):
                future = pool.submit(_download, entry, manifest["revision"], directory, user_agent, stop)
                future.add_done_callback(lambda f: _cancel_on_failure(f, stop))
                futures.add(future)
            completed = 0
            while futures:
                done, pending = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    future.result()
                _check_cancelled(stop)
                futures = pending
                completed += len(done)
                for entry in islice(entries, len(done)):
                    future = pool.submit(_download, entry, manifest["revision"], directory, user_agent, stop)
                    future.add_done_callback(lambda f: _cancel_on_failure(f, stop))
                    futures.add(future)
                logger.info("Article snapshot files: %d/%d", completed, len(manifest["files"]))
        except BaseException as exc:
            stop.set()
            for future in futures:
                future.cancel()
            wait(futures)
            if isinstance(exc, CancelledError):
                for future in futures:
                    if not future.cancelled():
                        error = future.exception()
                        if error is not None and not isinstance(error, CancelledError):
                            raise error from exc
            raise
    _check_cancelled(stop)
    manifest["complete"] = True
    manifest["elapsed_seconds"] = time.time() - manifest["started_epoch"]
    _write(manifest_path, manifest)
    return dict(manifest)


def article_objects(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    files: set[str] = set()
    alts: dict[str, str] = {}
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("content_url"):
                filename = _extract_filename(value["content_url"])
                if filename:
                    files.add("File:" + filename.replace("_", " "))
                    if value.get("alternative_text"):
                        alts[_file_key(filename)] = str(value["alternative_text"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(row.get("image"))
    sections = json.loads(row["sections"]) if row.get("sections") else []
    visit(sections)
    for key in ("infoboxes", "tables"):
        if row.get(key):
            visit(json.loads(row[key]))
    extract = row.get("abstract") or ""
    for section in sections:
        if section.get("name") == "Abstract":
            paragraphs = [p["value"] for p in section.get("has_parts", [])
                          if p.get("type") == "paragraph" and p.get("value")]
            if paragraphs:
                extract = "\n\n".join(paragraphs)
            break
    return {"extract": extract, "images": sorted(files, key=lambda s: s.replace(" ", "_"))}, alts


def _collect_ids(value: Any) -> Generator[str]:
    if isinstance(value, dict):
        if isinstance(value.get("id"), str) and re.fullmatch(r"Q[1-9][0-9]*", value["id"]):
            yield value["id"]
        for child in value.values():
            yield from _collect_ids(child)
    elif isinstance(value, list):
        for child in value:
            yield from _collect_ids(child)


def _selected_articles(path: pathlib.Path) -> int:
    with closing(sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)) as inventory:
        return int(inventory.execute(
            "SELECT COUNT(*) FROM objects WHERE key GLOB 'enwiki:selection:*'").fetchone()[0])


def populate(directory: pathlib.Path, articles: dict[str, Any], user_agent: str,
             acquisition: Future[dict[str, Any]] | None = None,
             stop: Event | None = None) -> dict[str, Any]:
    _check_cancelled(stop)
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ImportError("Public snapshots require: pip install wikidata-bulk-people[bulk]") from exc
    progress_path = directory / "prepared.json"
    initial = {"indexed_shards": 0, "matched_articles": 0, "article_selection_format": 2}
    progress = _read(progress_path) if progress_path.exists() else dict(initial)
    cache_path = directory / "source-cache.sqlite3"
    if progress.get("indexed_shards") and not cache_path.exists():
        raise ValueError("Checkpointed source cache is missing")
    cache = ObjectCache(cache_path, ttl=float("inf"))
    if progress.get("indexed_shards") or progress.get("complete"):
        hit, saved = cache.get("snapshot:checkpoint")
        if (not hit or saved["indexed_shards"] < progress["indexed_shards"]
                or (progress.get("complete") and not saved.get("complete"))):
            cache.close()
            raise ValueError("Source cache does not match its preparation checkpoint")
    if progress.get("article_selection_format") != 2:


        progress = dict(initial)
    if progress.get("complete"):
        cache.close()
        return dict(progress)
    db = sqlite3.connect(directory / "entities.sqlite3")
    commons = CommonsClient(user_agent, cache=cache)
    sparql = _SparqlClient(user_agent)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS needed(qid TEXT PRIMARY KEY)")
        values = {}
        for qid, body in db.execute("SELECT qid,body FROM entities"):
            _check_cancelled(stop)
            entity = json.loads(body)
            values["entity:en:" + qid] = entity
            db.executemany("INSERT OR IGNORE INTO needed VALUES(?)", ((q,) for q in _collect_ids(entity["claims"])))
            if len(values) >= 500:
                cache.put_many(values)
                values.clear()
        cache.put_many(values)
        db.commit()
        values = {}
        for qid, label in db.execute("SELECT n.qid,l.value FROM needed n JOIN labels l USING(qid)"):
            _check_cancelled(stop)
            values["label:en:" + qid] = label
            if len(values) >= 1000:
                cache.put_many(values)
                values.clear()
        cache.put_many(values)
        missing = db.execute("SELECT n.qid FROM needed n LEFT JOIN labels l USING(qid) WHERE l.qid IS NULL")
        while rows := missing.fetchmany(400):
            _check_cancelled(stop)
            batch = [q for (q,) in rows if not cache.get("label:en:" + q)[0]]
            if not batch:
                continue
            query = ("SELECT ?item ?label WHERE { VALUES ?item { " + " ".join("wd:" + q for q in batch)
                     + ' } OPTIONAL { ?item rdfs:label ?label. FILTER(LANG(?label)="en") } }')
            labels = {r["item"]["value"].rsplit("/", 1)[-1]: r.get("label", {}).get("value")
                      for r in sparql.run_query(query)}
            if set(labels) != set(batch):
                raise TransportError("https://query.wikidata.org/sparql", reason="Incomplete related-label batch")
            cache.put_many({"label:en:" + q: label for q, label in labels.items()})
        columns = ["name", "abstract", "image", "sections", "infoboxes", "tables", "main_entity"]
        for index, entry in enumerate(articles["files"]):
            _check_cancelled(stop)
            if index < progress["indexed_shards"]:
                continue
            path = directory / "articles" / pathlib.PurePosixPath(entry["path"]).name
            while not path.exists():
                if acquisition is None:
                    raise FileNotFoundError(path)
                if acquisition.done():
                    acquisition.result()
                    raise ValueError(f"Completed acquisition is missing a listed shard: {path.name}")
                _pause(1, stop)
            image_candidates: set[str] = set()
            parquet = pq.ParquetFile(path)
            read_columns = columns + (["identifier"] if "identifier" in parquet.schema_arrow.names else [])
            if "version" in parquet.schema_arrow.names:
                read_columns.append("version.identifier")
            for batch_data in parquet.iter_batches(batch_size=512, columns=read_columns):
                _check_cancelled(stop)


                identities = batch_data.column(columns.index("main_entity")).to_pylist()
                qids = [(identity or {}).get("identifier") for identity in identities]
                placeholders = ",".join("?" for _ in qids)
                selected = dict(db.execute(
                    f"SELECT qid,title FROM entities WHERE qid IN ({placeholders})", qids))
                if not selected:
                    continue
                batch_rows = batch_data.filter(pa.array([qid in selected for qid in qids])).to_pylist()
                values = {}
                for row in batch_rows:
                    qid = (row.get("main_entity") or {}).get("identifier")
                    title = selected.get(qid)
                    if not isinstance(qid, str) or not title:
                        continue
                    choice_key = "enwiki:selection:" + qid
                    rank = [int(row["name"] == title), int((row.get("version") or {}).get("identifier") or 0),
                            int(row.get("identifier") or 0), row["name"]]
                    previous = values.get(choice_key)
                    if previous is None:
                        previous = cache.get(choice_key)[1]
                    if previous is not None and previous["rank"] >= rank:
                        cached_page = values.get("enwiki:page:" + title)
                        if cached_page is None:
                            cached_page = cache.get("enwiki:page:" + title)[1]
                        if not isinstance(cached_page, dict):
                            raise ValueError("Selected article content is missing from the source cache")
                        image_candidates.update([f for f in cached_page["images"]
                                                 if not _looks_like_chrome(f[5:])][:20])
                        continue
                    page, alts = article_objects(row)
                    values["enwiki:page:" + title] = page
                    values["enwiki:alts:" + title] = alts
                    values[choice_key] = {"rank": rank, "title": row["name"], "revision": rank[1]}
                    image_candidates.update([f for f in page["images"] if not _looks_like_chrome(f[5:])][:20])
                cache.put_many(values)
                if len(image_candidates) >= 2000:
                    commons.get_image_infos(sorted(image_candidates))
                    image_candidates.clear()
            commons.get_image_infos(sorted(image_candidates))
            progress.update(indexed_shards=index + 1, matched_articles=_selected_articles(cache_path))
            cache.put_many({"snapshot:checkpoint": dict(progress)})
            _write(progress_path, progress)
            logger.info("Snapshot enriched: %d/%d article shards; %d matched articles", index + 1,
                        len(articles["files"]), progress["matched_articles"])


        progress["fallback_articles"] = _prefetch_fallbacks(db, cache, commons, user_agent, stop)
        _check_cancelled(stop)
        progress.update(complete=True, people=db.execute("SELECT COUNT(*) FROM entities").fetchone()[0])
        cache.put_many({"snapshot:checkpoint": dict(progress)})
        _write(progress_path, progress)
        return dict(progress)
    finally:
        sparql.close()
        commons.close()
        cache.close()
        db.close()


def _prefetch_fallbacks(db: sqlite3.Connection, cache: ObjectCache, commons: CommonsClient,
                        user_agent: str, stop: Event | None = None) -> int:
    wikipedia = WikipediaClient(user_agent, cache=cache)
    rest = RestClient(user_agent, cache=cache)
    count = 0
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            cursor = db.execute("SELECT title FROM entities WHERE title IS NOT NULL ORDER BY qid")
            missing = (title for (title,) in cursor if not cache.get("enwiki:page:" + title)[0]
                       or not cache.get("enwiki:alts:" + title)[0])
            while titles := list(islice(missing, 500)):
                _check_cancelled(stop)
                batches = [titles[i:i + 20] for i in range(0, len(titles), 20)]
                pages = {title: page for group in pool.map(wikipedia.get_pages, batches)
                         for title, page in group.items()}
                filenames = [f for page in pages.values() for f in
                             [f for f in page["images"] if not _looks_like_chrome(f[5:])][:20]]
                commons.get_image_infos(filenames)

                for offset in range(0, len(titles), 50):
                    needed = [t for t in titles[offset:offset + 50]
                              if any(not _looks_like_chrome(f[5:]) for f in pages[t]["images"])]
                    list(pool.map(rest.extract_image_alts, needed))
                cache.put_many({"enwiki:alts:" + t: {} for t in titles
                                if not any(not _looks_like_chrome(f[5:]) for f in pages[t]["images"])})
                count += len(titles)
                logger.info("Prepared %d article fallbacks", count)
        return count
    finally:
        rest.close()
        wikipedia.close()


def prepare(directory: pathlib.Path, filter: PeopleFilter, limit: int, user_agent: str) -> dict[str, Any]:
    _validate_filter(filter)
    if limit < 1:
        raise ValueError("Public snapshot acquisition requires a positive limit")

    try:
        import pyarrow.parquet
    except ImportError as exc:
        raise ImportError("Public snapshots require: pip install wikidata-bulk-people[bulk]") from exc
    directory.mkdir(parents=True, exist_ok=True)
    config = {"format": 1, "filter": dataclasses.asdict(filter), "limit": limit}
    with job_lock(directory / "source.lock"):
        config_path = directory / "config.json"
        if config_path.exists() and _read(config_path) != config:
            raise ValueError("Source directory belongs to a different selection; use a new directory")
        _write(config_path, config)
        provenance_path = directory / "provenance.json"
        if provenance_path.exists():
            if not (directory / "source-cache.sqlite3").exists() or not (directory / "entities.sqlite3").exists():
                raise ValueError("Prepared source files are missing")
            return dict(_read(provenance_path))
        started = time.time()
        stop = Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            entities = pool.submit(acquire_entities, directory, filter, limit, user_agent, stop)
            entities.add_done_callback(lambda f: _cancel_on_failure(f, stop))
            articles = pool.submit(acquire_articles, directory / "articles", user_agent, stop)
            articles.add_done_callback(lambda f: _cancel_on_failure(f, stop))
            try:
                entity_info = entities.result()
                article_manifest = directory / "articles" / "manifest.json"
                while not articles.done() and not article_manifest.exists():
                    _pause(1, stop)
                article_info = articles.result() if articles.done() else _read(article_manifest)
                prepared = populate(directory, article_info, user_agent, articles, stop)
                article_info = articles.result()
            except BaseException as exc:
                stop.set()
                for future in (entities, articles):
                    future.cancel()
                wait((entities, articles))
                if isinstance(exc, CancelledError):
                    for future in (entities, articles):
                        if not future.cancelled():
                            error = future.exception()
                            if error is not None and not isinstance(error, CancelledError):
                                raise error from exc
                raise
        provenance = {"mode": "published snapshots with API enrichment", "wikidata": entity_info,
                      "articles": {k: v for k, v in article_info.items() if k != "files"},
                      "prepared": prepared, "last_attempt_seconds": time.time() - started,
                      "completed_epoch": time.time()}
        _write(provenance_path, provenance)
        return provenance


def iter_qids(directory: pathlib.Path) -> Generator[str]:
    db = sqlite3.connect(f"file:{(directory / 'entities.sqlite3').resolve().as_posix()}?mode=ro", uri=True)
    try:
        for row in db.execute("SELECT qid FROM entities ORDER BY qid"):
            yield row[0]
    finally:
        db.close()


class SnapshotCache(ObjectCache):
    def __init__(self, directory: pathlib.Path, live_path: pathlib.Path | None, ttl: float) -> None:
        super().__init__(live_path, ttl=ttl)
        self.source = ObjectCache(directory / "source-cache.sqlite3", ttl=float("inf"))

    def get(self, key: str) -> tuple[bool, Any]:
        hit, value = self.source.get(key)
        if hit:
            with self._lock:
                self.hits += 1
            return True, value
        return super().get(key)

    def close(self) -> None:
        self.source.close()
        super().close()
