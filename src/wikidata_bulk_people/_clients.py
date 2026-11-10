import logging
import re
import threading
import time
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlparse, urljoin

import requests

from wikidata_bulk_people._cache import ObjectCache
from wikidata_bulk_people._models import NotFoundError, TransportError

logger = logging.getLogger("wikidata_bulk_people")
_DEFAULT_THROTTLE = {"www.wikidata.org": 0.2, "query.wikidata.org": 0.2,
                     "en.wikipedia.org": 0.1, "commons.wikimedia.org": 0.1}
_MAX_RETRIES = 5
_BACKOFF_BASE = 1.0
_WIKIDATA_API = "https://www.wikidata.org/w/api.php"
_WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
_COMMONS_API = "https://commons.wikimedia.org/w/api.php"
_REST_BASE = "https://en.wikipedia.org/w/rest.php/v1/page"


def chunks(values: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


class _HostThrottle:

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._cooldown: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str, min_interval: float) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                delay = max(self._last.get(host, -min_interval) + min_interval,
                            self._cooldown.get(host, 0.0)) - now
                if delay <= 0:
                    self._last[host] = now
                    return
            time.sleep(delay)

    def defer(self, host: str, delay: float) -> None:
        with self._lock:
            self._cooldown[host] = max(self._cooldown.get(host, 0.0), time.monotonic() + delay)


_throttle = _HostThrottle()
_api_slots = threading.BoundedSemaphore(3)
_WIKIMEDIA_API_HOSTS = {"www.wikidata.org", "en.wikipedia.org", "commons.wikimedia.org"}


def _retry_after(value: str | None, fallback: float) -> float:
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                dt = parsedate_to_datetime(value)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return fallback


class BaseClient:
    def __init__(self, user_agent: str, throttle_overrides: dict[str, float] | None = None,
                 cache: ObjectCache | None = None, *, interactive: bool = False) -> None:
        self._interactive = interactive
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent})
        self._session_owner = threading.get_ident()
        self._thread_sessions: dict[int, requests.Session] = {}
        self._sessions_lock = threading.Lock()
        self._metrics_lock = threading.Lock()
        self._throttle_map = {**_DEFAULT_THROTTLE, **(throttle_overrides or {})}
        self.cache = cache if cache is not None else ObjectCache()
        self.requests = self.retries = 0

    def close(self) -> None:
        self._session.close()
        for session in self._thread_sessions.values():
            session.close()
        self._thread_sessions.clear()

    def _request_session(self) -> requests.Session:
        owner = threading.get_ident()
        if owner == self._session_owner:
            return self._session
        with self._sessions_lock:
            if owner not in self._thread_sessions:
                session = requests.Session()
                session.headers.update(self._session.headers)
                self._thread_sessions[owner] = session
            return self._thread_sessions[owner]

    def _min_interval(self, url: str) -> float:
        return self._throttle_map.get(urlparse(url).netloc, 0.1)

    def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self._request(url, params, text=False)

    def _api_get(self, url: str, params: dict[str, Any]) -> Any:
        # MediaWiki permits foreground reads without maxlag. Background jobs
        # still yield to replication/query-service lag, independently of 429s.
        if not self._interactive:
            params = {**params, "maxlag": "5"}
        return self._get(url, params)

    def _send(self, session: requests.Session, url: str,
              params: dict[str, Any] | None) -> requests.Response:
        original_host = urlparse(url).netloc
        post = bool(params and len(urlencode(params)) > 7000)
        for _ in range(10):
            host = urlparse(url).netloc
            _throttle.wait(host, self._min_interval(url))
            with _api_slots:
                if host in _WIKIMEDIA_API_HOSTS:
                    _throttle.wait("wikimedia-api", 0.3)
                with self._metrics_lock:
                    self.requests += 1
                if post:
                    response = session.post(url, data=params, timeout=(10, 65), allow_redirects=False,
                                            headers={"Promise-Non-Write-API-Action": "true"})
                else:
                    response = session.get(url, params=params, timeout=(10, 65), allow_redirects=False)
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get("Location")
            target = urljoin(url, location or "")
            if (not location or urlparse(target).scheme != "https" or
                    (original_host in _WIKIMEDIA_API_HOSTS and urlparse(target).netloc not in _WIKIMEDIA_API_HOSTS)):
                response.close()
                raise TransportError(url, reason="Invalid or unexpected API redirect")
            if response.status_code in (301, 302, 303):
                post = False
            if not post:
                params = None
            response.close()
            url = target
        raise TransportError(url, reason="Too many API redirects")

    def _request(self, url: str, params: dict[str, Any] | None = None,
                 *, text: bool = False) -> Any:
        host = urlparse(url).netloc
        session = self._request_session()
        last: Exception | None = None
        status: int | None = None
        for attempt in range(_MAX_RETRIES):
            retry_header = None
            shared_cooldown = False
            lagged = False
            try:


                resp = self._send(session, url, params)
                status = resp.status_code
                retry_header = resp.headers.get("Retry-After")
                if status == 200:
                    if text:
                        content_type = resp.headers.get("Content-Type", "text/html")
                        if "html" not in content_type or not resp.text.lstrip().startswith("<"):
                            raise ValueError("Expected rendered HTML, not an empty/error response")
                        return resp.text
                    data = resp.json()
                    if not isinstance(data, dict):
                        raise ValueError("Expected a JSON object")
                    error = data.get("error")
                    if not error:
                        return data
                    code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
                    shared_cooldown = code == "ratelimited"
                    lagged = code == "maxlag"
                    reason = f"Wikimedia API error {code}: {error}"
                    if code not in {"maxlag", "ratelimited", "readonly", "internal_api_error_DBQueryError",
                                    "internal_api_error_DBConnectionError", "timeout"}:
                        raise TransportError(url, status, reason)
                    last = TransportError(url, status, reason)
                elif status == 429 or status >= 500:
                    shared_cooldown = status == 429
                    last = TransportError(url, status)
                else:
                    raise TransportError(url, status, f"Unexpected HTTP {status} from {url}")
            except (requests.RequestException, ValueError) as exc:
                last = exc
            delay = _retry_after(retry_header, _BACKOFF_BASE * 2 ** attempt)
            if lagged:
                delay = max(5.0, delay)
            if attempt + 1 == _MAX_RETRIES:
                if lagged:
                    _throttle.defer(host, delay)
                if shared_cooldown and host in _WIKIMEDIA_API_HOSTS:
                    _throttle.defer("wikimedia-api", _retry_after(retry_header, 5))
                break
            with self._metrics_lock:
                self.retries += 1
            logger.warning("Retrying %s in %.1fs (%d/%d): %s", host, delay,
                           attempt + 1, _MAX_RETRIES, last)
            _throttle.defer(host, delay)
            if shared_cooldown and host in _WIKIMEDIA_API_HOSTS:
                _throttle.defer("wikimedia-api", delay)
        raise TransportError(url, status, f"Exhausted {_MAX_RETRIES} attempts for {url}: {last}") from last

    def _entities(self, data: Any) -> dict[str, Any]:
        entities = data.get("entities")
        if not isinstance(entities, dict) or not entities:
            raise TransportError(_WIKIDATA_API, reason="Missing or invalid entities response")
        return entities


class WikidataClient(BaseClient):
    def get_entities(self, qids: list[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        pending = []
        for qid in dict.fromkeys(qids):
            if not re.fullmatch(r"Q[1-9][0-9]*", qid):
                raise ValueError(f"Invalid QID: {qid!r}")
            hit, value = self.cache.get("entity:en:" + qid)
            if hit:
                result[qid] = value
            else:
                pending.append(qid)
        for batch in chunks(pending, 50):
            entities = self._entities(self._api_get(_WIKIDATA_API, {
                "action": "wbgetentities", "ids": "|".join(batch), "format": "json",
                "languages": "en", "sitefilter": "enwiki"}))
            for qid in batch:
                entity = entities.get(qid)
                if not isinstance(entity, dict) or ("missing" not in entity and entity.get("id") != qid):
                    raise TransportError(_WIKIDATA_API, reason=f"Incomplete entity batch: {qid}")
                result[qid] = entity
            self.cache.put_many({"entity:en:" + q: result[q] for q in batch})
        return result

    def get_entity(self, qid: str) -> dict[str, Any]:
        entity = self.get_entities([qid.upper()])[qid.upper()]
        if "missing" in entity:
            raise NotFoundError(qid)
        return entity

    def get_entity_by_title(self, title: str) -> dict[str, Any]:
        entities = self._entities(self._api_get(_WIKIDATA_API, {
            "action": "wbgetentities", "sites": "enwiki", "titles": title,
            "format": "json", "languages": "en", "sitefilter": "enwiki"}))
        for qid, entity in entities.items():
            if "missing" not in entity and re.fullmatch(r"Q[1-9][0-9]*", qid):
                self.cache.put_many({"entity:en:" + qid: entity})
                return dict(entity)
        raise NotFoundError(title)

    def get_labels(self, qids: list[str], lang: str = "en") -> dict[str, str]:
        result = {}
        pending = []
        for qid in dict.fromkeys(qids):
            hit, value = self.cache.get(f"label:{lang}:{qid}")
            if hit:
                if value is not None:
                    result[qid] = str(value)
            else:
                pending.append(qid)
        for batch in chunks(pending, 50):
            entities = self._entities(self._api_get(_WIKIDATA_API, {
                "action": "wbgetentities", "ids": "|".join(batch), "props": "labels",
                "languages": lang, "format": "json"}))
            values = {}
            for qid in batch:
                if qid not in entities:
                    raise TransportError(_WIKIDATA_API, reason=f"Incomplete labels batch: {qid}")
                label = entities[qid].get("labels", {}).get(lang, {}).get("value")
                values[f"label:{lang}:{qid}"] = label
                if label is not None:
                    result[qid] = str(label)
            self.cache.put_many(values)
        return result


def _pages(data: Any, url: str) -> dict[str, Any]:
    pages = data.get("query", {}).get("pages")
    if not isinstance(pages, dict) or not pages:
        raise TransportError(url, reason="Missing or invalid query.pages response")
    return pages


def _resolve_titles(titles: list[str], pages: dict[str, Any], aliases: dict[str, str],
                    url: str) -> dict[str, Any]:
    result = {}
    for title in titles:
        target = title
        visited = set()
        while target in aliases and target not in visited:
            visited.add(target)
            target = aliases[target]
        if target not in pages:
            raise TransportError(url, reason=f"Incomplete page batch: {title}")
        result[title] = pages[target]
    return result


class WikipediaClient(BaseClient):
    def get_pages(self, titles: list[str]) -> dict[str, dict[str, Any]]:
        result = {}
        pending = []
        for title in dict.fromkeys(titles):
            hit, value = self.cache.get("enwiki:page:" + title)
            if hit:
                result[title] = value
            else:
                pending.append(title)
        for batch in chunks(pending, 20):
            params: dict[str, Any] = {"action": "query", "titles": "|".join(batch),
                "prop": "extracts|images", "exintro": "1", "explaintext": "1", "exlimit": "20",
                "imlimit": "500", "redirects": "1", "format": "json"}
            pages: dict[str, Any] = {}
            aliases = {}
            continuations = set()
            while True:
                data = self._api_get(_WIKIPEDIA_API, params)
                for entry in _pages(data, _WIKIPEDIA_API).values():
                    title = entry.get("title")
                    if not isinstance(title, str):
                        raise TransportError(_WIKIPEDIA_API, reason="Page is missing its title")
                    page = pages.setdefault(title, {"images": []})
                    page["images"].extend(img["title"] for img in entry.get("images", []))
                    if "extract" in entry:
                        page["extract"] = entry["extract"]
                    if "missing" in entry:
                        page["missing"] = True
                for kind in ("normalized", "redirects", "converted"):
                    aliases.update({x["from"]: x["to"] for x in data.get("query", {}).get(kind, [])})
                cont = data.get("continue")
                if not cont:
                    break
                token = repr(sorted(cont.items()))
                if token in continuations:
                    raise TransportError(_WIKIPEDIA_API, reason="Repeated continuation token")
                continuations.add(token)
                params.update(cont)
            resolved = _resolve_titles(batch, pages, aliases, _WIKIPEDIA_API)
            for page in resolved.values():
                page["images"] = list(dict.fromkeys(page["images"]))
                if "extract" not in page and not page.get("missing"):
                    raise TransportError(_WIKIPEDIA_API, reason="Incomplete extracts response")
            self.cache.put_many({"enwiki:page:" + k: v for k, v in resolved.items()})
            result.update(resolved)
        return result

    def get_extract(self, title: str) -> str | None:
        value = self.get_pages([title])[title].get("extract")
        return str(value) if value else None

    def get_image_list(self, title: str) -> list[str]:
        return list(self.get_pages([title])[title]["images"])


class CommonsClient(BaseClient):
    def __init__(self, user_agent: str, throttle_overrides: dict[str, float] | None = None,
                 cache: ObjectCache | None = None, *, interactive: bool = False) -> None:
        super().__init__(user_agent, throttle_overrides, cache, interactive=interactive)
        self._pool = ThreadPoolExecutor(max_workers=3)

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)
        super().close()

    def get_image_infos(self, filenames: list[str]) -> dict[str, dict[str, Any]]:
        result = {}
        pending = []
        for filename in dict.fromkeys(filenames):
            hit, value = self.cache.get("commons:info300:" + filename)
            if hit:
                result[filename] = value
            else:
                pending.append(filename)


        batches = iter(chunks(pending, 50))
        futures = {self._pool.submit(self._fetch_image_batch, batch)
                   for batch in [next(batches, None) for _ in range(3)] if batch is not None}
        try:
            while futures:
                done, futures = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    result.update(future.result())
                    batch = next(batches, None)
                    if batch is not None:
                        futures.add(self._pool.submit(self._fetch_image_batch, batch))
        finally:

            for future in futures:
                future.cancel()
            if futures:
                wait(futures)
        return result

    def _fetch_image_batch(self, batch: list[str]) -> dict[str, dict[str, Any]]:
        data = self._api_get(_COMMONS_API, {"action": "query", "titles": "|".join(batch),
            "prop": "imageinfo", "iiprop": "url|dimensions|extmetadata", "iiurlwidth": "300",
            "iiextmetadatafilter": "ImageDescription|LicenseShortName|Artist", "iiextmetadatalanguage": "en",
            "format": "json"})
        pages = {p["title"]: p for p in _pages(data, _COMMONS_API).values()}
        aliases = {x["from"]: x["to"] for x in data.get("query", {}).get("normalized", [])}
        resolved = _resolve_titles(batch, pages, aliases, _COMMONS_API)
        values = {}
        for filename, page in resolved.items():
            infos = page.get("imageinfo", [])
            if not infos and "missing" not in page and "invalid" not in page:
                raise TransportError(_COMMONS_API, reason=f"Missing imageinfo: {filename}")
            values[filename] = dict(infos[0]) if infos else {}
        self.cache.put_many({"commons:info300:" + k: v for k, v in values.items()})
        return values

    def get_image_info(self, filename: str) -> dict[str, Any]:
        return self.get_image_infos([filename])[filename]


_THUMB_RE = re.compile(r"/thumb/[0-9a-f]/[0-9a-f]{2}/(.+?)/[^/]+$", re.IGNORECASE)
_UPLOAD_RE = re.compile(r"/[0-9a-f]/[0-9a-f]{2}/(.+)$", re.IGNORECASE)


def _extract_filename(src: str) -> str | None:
    path = urlparse(src).path
    match = _THUMB_RE.search(path) or _UPLOAD_RE.search(path)
    return unquote(match.group(1)) if match else None


def _file_key(filename: str) -> str:
    return unquote(filename).replace("_", " ").lower()


class _AltParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.alts: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "img":
            attributes = dict(attrs)
            filename = _extract_filename(attributes.get("src") or "")
            alt = attributes.get("alt")
            if filename and alt:
                self.alts[_file_key(filename)] = alt


class RestClient(BaseClient):
    def extract_image_alts(self, title: str) -> dict[str, str]:
        hit, value = self.cache.get("enwiki:alts:" + title)
        if hit:
            return dict(value)
        url = f"{_REST_BASE}/{quote(title.replace(' ', '_'), safe='')}/html"
        html = self._request(url, text=True)
        parser = _AltParser()
        parser.feed(html)
        self.cache.put_many({"enwiki:alts:" + title: parser.alts})
        return parser.alts
