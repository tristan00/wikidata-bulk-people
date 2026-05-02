"""HTTP clients for all Wikimedia APIs."""

import logging
import re
import threading
import time
from html.parser import HTMLParser
from typing import Any

import requests

from wikidata_bulk_people._models import NotFoundError, TransportError

logger = logging.getLogger("wikidata_bulk_people")

# ---------------------------------------------------------------------------
# BaseClient
# ---------------------------------------------------------------------------

_DEFAULT_THROTTLE: dict[str, float] = {
    "www.wikidata.org": 0.2,
    "query.wikidata.org": 0.2,
    "en.wikipedia.org": 0.1,
    "commons.wikimedia.org": 0.1,
    "en.wikipedia.org/api/rest_v1": 0.1,
}

_MAX_RETRIES = 5
_BACKOFF_BASE = 1.0


class _HostThrottle:
    """Enforces a minimum inter-request interval per host."""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str, min_interval: float) -> None:
        with self._lock:
            now = time.monotonic()
            gap = now - self._last.get(host, 0.0)
            if gap < min_interval:
                time.sleep(min_interval - gap)
            self._last[host] = time.monotonic()


_throttle = _HostThrottle()


class BaseClient:
    """Shared HTTP base with User-Agent injection, throttling, and retry logic.

    Args:
        user_agent: Value for the ``User-Agent`` request header.
        throttle_overrides: Optional per-host minimum interval overrides (seconds).
    """

    def __init__(
        self,
        user_agent: str,
        throttle_overrides: dict[str, float] | None = None,
    ) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent})
        self._throttle_map = dict(_DEFAULT_THROTTLE)
        if throttle_overrides:
            self._throttle_map.update(throttle_overrides)

    def _min_interval(self, url: str) -> float:
        for host, interval in self._throttle_map.items():
            if host in url:
                return interval
        return 0.1

    def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """Perform a GET request with throttle, retry, and backoff.

        Args:
            url: Full URL to request.
            params: Optional query parameters.

        Returns:
            Parsed JSON response.

        Raises:
            TransportError: After *_MAX_RETRIES* failed attempts.
        """
        from urllib.parse import urlparse

        host = urlparse(url).netloc
        interval = self._min_interval(url)

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            _throttle.wait(host, interval)
            try:
                resp = self._session.get(url, params=params, timeout=30)
            except requests.RequestException as exc:
                last_exc = exc
                wait = _BACKOFF_BASE * (2**attempt)
                logger.warning(
                    "Request error on attempt %d/%d: %s; retrying in %.1fs",
                    attempt + 1,
                    _MAX_RETRIES,
                    exc,
                    wait,
                )
                time.sleep(wait)
                continue

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = float(resp.headers.get("Retry-After", _BACKOFF_BASE * (2**attempt)))
                logger.warning(
                    "HTTP %d from %s; retrying after %.1fs (attempt %d/%d)",
                    resp.status_code,
                    url,
                    retry_after,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(retry_after)
                last_exc = TransportError(url, resp.status_code)
                continue

            raise TransportError(
                url, resp.status_code, f"Unexpected HTTP {resp.status_code} from {url}"
            )

        raise TransportError(
            url,
            None,
            f"Exhausted {_MAX_RETRIES} retries for {url}",
        ) from last_exc


# ---------------------------------------------------------------------------
# WikidataClient
# ---------------------------------------------------------------------------

_WIKIDATA_API = "https://www.wikidata.org/w/api.php"


class WikidataClient(BaseClient):
    """Fetches raw entity dicts from the Wikidata API."""

    def get_entity(self, qid: str) -> dict[str, Any]:
        """Fetch a single Wikidata entity by QID.

        Args:
            qid: Wikidata QID, e.g. "Q937".

        Returns:
            Raw entity dict from the API response.

        Raises:
            NotFoundError: If the entity does not exist or is missing.
            TransportError: On network failure.
        """
        data = self._get(
            _WIKIDATA_API,
            params={
                "action": "wbgetentities",
                "ids": qid,
                "format": "json",
                "languages": "en",
                "maxlag": "5",
            },
        )
        entities: dict[str, Any] = data.get("entities", {})
        entity: dict[str, Any] = entities.get(qid, {})
        if entity.get("missing") == "":
            raise NotFoundError(qid)
        return entity

    def get_entity_by_title(self, title: str) -> dict[str, Any]:
        """Fetch a Wikidata entity by its linked English Wikipedia article title.

        Args:
            title: Wikipedia article title, e.g. "Albert Einstein".

        Returns:
            Raw entity dict.

        Raises:
            NotFoundError: If no linked entity is found.
            TransportError: On network failure.
        """
        data = self._get(
            _WIKIDATA_API,
            params={
                "action": "wbgetentities",
                "sites": "enwiki",
                "titles": title,
                "format": "json",
                "languages": "en",
                "maxlag": "5",
            },
        )
        entities: dict[str, Any] = data.get("entities", {})
        for entity_qid, entity in entities.items():
            entity_dict: dict[str, Any] = entity
            if entity_dict.get("missing") == "":
                raise NotFoundError(title)
            if not entity_qid.startswith("-"):
                return entity_dict
        raise NotFoundError(title)

    def get_labels(self, qids: list[str], lang: str = "en") -> dict[str, str]:
        """Fetch labels for a batch of QIDs.

        Args:
            qids: List of QIDs.
            lang: Language code (default "en").

        Returns:
            Mapping of QID → label string.
        """
        if not qids:
            return {}
        data = self._get(
            _WIKIDATA_API,
            params={
                "action": "wbgetentities",
                "ids": "|".join(qids),
                "props": "labels",
                "languages": lang,
                "format": "json",
                "maxlag": "5",
            },
        )
        result: dict[str, str] = {}
        for qid, entity in data.get("entities", {}).items():
            labels = entity.get("labels", {})
            label_entry = labels.get(lang)
            if label_entry:
                result[qid] = label_entry["value"]
        return result


# ---------------------------------------------------------------------------
# WikipediaClient
# ---------------------------------------------------------------------------

_WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"


class WikipediaClient(BaseClient):
    """Fetches article extracts and image lists from the English Wikipedia API."""

    def get_extract(self, title: str) -> str | None:
        """Fetch the introductory plain-text extract for a Wikipedia article.

        Args:
            title: Wikipedia article title.

        Returns:
            Plain-text intro extract, or None if not available.
        """
        data = self._get(
            _WIKIPEDIA_API,
            params={
                "action": "query",
                "titles": title,
                "prop": "extracts",
                "exintro": "1",
                "explaintext": "1",
                "format": "json",
                "maxlag": "5",
            },
        )
        pages: dict[str, Any] = data.get("query", {}).get("pages", {})
        for page in pages.values():
            extract = page.get("extract")
            if extract:
                return str(extract)
        return None

    def get_image_list(self, title: str) -> list[str]:
        """Fetch the list of image filenames used in a Wikipedia article.

        Args:
            title: Wikipedia article title.

        Returns:
            List of "File:*" titles (may include non-image files).
        """
        images: list[str] = []
        params: dict[str, Any] = {
            "action": "query",
            "titles": title,
            "prop": "images",
            "imlimit": "50",
            "format": "json",
            "maxlag": "5",
        }
        while True:
            data = self._get(_WIKIPEDIA_API, params=params)
            pages: dict[str, Any] = data.get("query", {}).get("pages", {})
            for page in pages.values():
                for img in page.get("images", []):
                    images.append(img["title"])
            cont = data.get("continue", {})
            if not cont:
                break
            params.update(cont)
        return images


# ---------------------------------------------------------------------------
# CommonsClient
# ---------------------------------------------------------------------------

_COMMONS_API = "https://commons.wikimedia.org/w/api.php"


class CommonsClient(BaseClient):
    """Fetches image metadata from Wikimedia Commons."""

    def get_image_info(self, filename: str) -> dict[str, Any]:
        """Fetch image info for a single Commons file.

        Args:
            filename: File title including "File:" prefix, e.g. "File:Einstein_1921.jpg".

        Returns:
            Dict with keys: url, thumburl, width, height, extmetadata (may be partial/empty).
        """
        data = self._get(
            _COMMONS_API,
            params={
                "action": "query",
                "titles": filename,
                "prop": "imageinfo",
                "iiprop": "url|dimensions|extmetadata",
                "iiurlwidth": "300",
                "format": "json",
            },
        )
        pages: dict[str, Any] = data.get("query", {}).get("pages", {})
        for page in pages.values():
            infos = page.get("imageinfo", [])
            if infos:
                return dict(infos[0])
        return {}


# ---------------------------------------------------------------------------
# RestClient
# ---------------------------------------------------------------------------

_REST_BASE = "https://en.wikipedia.org/api/rest_v1/page/html"

# Matches "/thumb/a/ab/Filename.jpg/300px-Filename.jpg" → "Filename.jpg"
_THUMB_RE = re.compile(r"/thumb/[0-9a-f]/[0-9a-f]{2}/(.+?)/\d+px-", re.IGNORECASE)
_UPLOAD_RE = re.compile(r"/[0-9a-f]/[0-9a-f]{2}/(.+)$", re.IGNORECASE)


class _AltParser(HTMLParser):
    """Minimal SAX-style parser that collects (src, alt) pairs from <img> tags."""

    def __init__(self) -> None:
        super().__init__()
        self.alts: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "img":
            return
        attr_map = dict(attrs)
        src = attr_map.get("src") or ""
        alt = attr_map.get("alt") or ""
        if src and alt:
            filename = _extract_filename(src)
            if filename:
                self.alts[filename.lower()] = alt


def _extract_filename(src: str) -> str | None:
    m = _THUMB_RE.search(src)
    if m:
        return m.group(1)
    m = _UPLOAD_RE.search(src)
    if m:
        return m.group(1)
    return None


class RestClient(BaseClient):
    """Fetches rendered Wikipedia HTML to extract image alt attributes."""

    def extract_image_alts(self, title: str) -> dict[str, str]:
        """Return a mapping of filename (lowercase) → alt text for all images on a page.

        Args:
            title: Wikipedia article title.

        Returns:
            Dict mapping lowercased Commons filename to alt text string.
        """
        import urllib.parse

        encoded = urllib.parse.quote(title.replace(" ", "_"))
        url = f"{_REST_BASE}/{encoded}"
        try:
            resp = self._session.get(url, timeout=30)
            if resp.status_code != 200:
                logger.debug("REST API returned %d for %s; skipping alts", resp.status_code, title)
                return {}
            html = resp.text
        except requests.RequestException as exc:
            logger.debug("REST API request failed for %s: %s; skipping alts", title, exc)
            return {}

        parser = _AltParser()
        parser.feed(html)
        return parser.alts
