import logging
import re
import threading
from datetime import datetime, timezone
from typing import Any
from concurrent.futures import ThreadPoolExecutor

from wikidata_bulk_people._cache import ObjectCache
from wikidata_bulk_people._clients import _file_key
from wikidata_bulk_people._util import _best_claims, _valid_claims

from wikidata_bulk_people._clients import (
    CommonsClient,
    RestClient,
    WikidataClient,
    WikipediaClient,
)
from wikidata_bulk_people._models import (
    DateValue,
    ExtractionError,
    ImageRef,
    Person,
    SpouseRecord,
)
from wikidata_bulk_people._util import (
    _claim_values,
    _classify_spouse_claim,
    _entity_qid_from_snak,
    _label,
    _looks_like_chrome,
    _parse_time,
    _strip_html,
)

logger = logging.getLogger("wikidata_bulk_people")


_P_INSTANCE_OF = "P31"
_P_DOB = "P569"
_P_DOD = "P570"
_P_PLACE_OF_BIRTH = "P19"
_P_PLACE_OF_DEATH = "P20"
_P_SEX = "P21"
_P_CITIZENSHIP = "P27"
_P_OCCUPATION = "P106"
_P_SPOUSE = "P26"
_P_ENWIKI = "enwiki"

_Q_HUMAN = "Q5"

_FILE_PREFIX_RE = re.compile(r"^File:", re.IGNORECASE)


class PersonExtractor:

    def __init__(self, user_agent: str, cache: ObjectCache | None = None,
                 workers: int = 4) -> None:
        self._user_agent = user_agent
        self._workers = workers
        self._pool = ThreadPoolExecutor(max_workers=workers)
        self._worker_local = threading.local()
        self._worker_clients: list[RestClient] = []
        self._worker_lock = threading.Lock()
        self._cache = cache if cache is not None else ObjectCache()
        self._batch_entities: dict[str, Any] | None = None
        self._batch_labels: dict[str, str] | None = None
        self._batch_pages: dict[str, Any] | None = None
        self._batch_infos: dict[str, Any] | None = None
        self._batch_alts: dict[str, dict[str, str]] | None = None
        self._wd = WikidataClient(user_agent=user_agent, cache=self._cache)
        self._wp = WikipediaClient(user_agent=user_agent, cache=self._cache)
        self._commons = CommonsClient(user_agent=user_agent, cache=self._cache)
        self._rest = RestClient(user_agent=user_agent, cache=self._cache)

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)
        for worker_client in self._worker_clients:
            worker_client.close()
        for client in (self._wd, self._wp, self._commons, self._rest):
            client.close()

    def extract_many(self, qids: list[str]) -> dict[str, Person | ExtractionError]:
        entities = self._wd.get_entities(qids)
        label_qids = []
        titles = []
        for entity in entities.values():
            for pid in (_P_PLACE_OF_BIRTH, _P_PLACE_OF_DEATH, _P_SEX,
                        _P_CITIZENSHIP, _P_OCCUPATION, _P_SPOUSE):
                label_qids.extend(self._all_entity_qids(entity, pid))
            for claim in _valid_claims(entity, _P_SPOUSE):
                for qualifier in claim.get("qualifiers", {}).get("P1534", []):
                    qid = _entity_qid_from_snak(qualifier)
                    if qid:
                        label_qids.append(qid)
            title = entity.get("sitelinks", {}).get("enwiki", {}).get("title")
            if title:
                titles.append(title)
        labels = self._wd.get_labels(label_qids)
        pages = self._wp.get_pages(titles)
        files = []
        for page in pages.values():
            files.extend([name for name in page["images"]
                          if not _looks_like_chrome(_FILE_PREFIX_RE.sub("", name))][:20])
        infos = self._commons.get_image_infos(files)

        def fetch_alts(title: str) -> tuple[str, dict[str, str]]:

            if not hasattr(self._worker_local, "client"):
                client = RestClient(self._user_agent, cache=self._cache)
                self._worker_local.client = client
                with self._worker_lock:
                    self._worker_clients.append(client)
            worker_client: RestClient = self._worker_local.client
            return title, worker_client.extract_image_alts(title)

        alts = dict(self._pool.map(fetch_alts, [t for t, p in pages.items()
            if any(not _looks_like_chrome(_FILE_PREFIX_RE.sub("", f)) for f in p["images"])]))
        self._batch_entities, self._batch_labels = entities, labels
        self._batch_pages, self._batch_infos, self._batch_alts = pages, infos, alts
        result: dict[str, Person | ExtractionError] = {}
        try:
            for qid in qids:
                if "missing" in entities[qid]:
                    result[qid] = ExtractionError(qid, "entity is missing")
                    continue
                try:
                    result[qid] = self.extract(qid)
                except ExtractionError as exc:
                    result[qid] = exc
        finally:
            self._batch_entities = self._batch_labels = None
            self._batch_pages = self._batch_infos = self._batch_alts = None
        return result

    def extract(self, qid_or_title: str) -> Person:
        fetched_at = datetime.now(tz=timezone.utc)

        if re.match(r"^Q\d+$", qid_or_title, re.IGNORECASE):
            entity = (self._batch_entities[qid_or_title] if self._batch_entities is not None
                      else self._wd.get_entity(qid_or_title))
        else:
            entity = self._wd.get_entity_by_title(qid_or_title)

        qid: str = entity.get("id", qid_or_title)
        lastrevid: int = int(entity.get("lastrevid", 0))

        instance_of_vals = _claim_values(entity, _P_INSTANCE_OF)
        qids_instance = [v.get("id") for v in instance_of_vals if isinstance(v, dict)]
        if _Q_HUMAN not in qids_instance:
            raise ExtractionError(qid, "entity is not instance of Q5 (human)")

        sitelinks = entity.get("sitelinks", {})
        wp_entry = sitelinks.get(_P_ENWIKI, {})
        wp_title: str | None = wp_entry.get("title") if wp_entry else None
        wp_url = f"https://en.wikipedia.org/wiki/{wp_title.replace(' ', '_')}" if wp_title else None

        name = _label(entity) or qid
        desc_entry = entity.get("descriptions", {}).get("en")
        description = desc_entry["value"] if desc_entry else None
        aliases_raw = entity.get("aliases", {}).get("en", [])
        aliases = [a["value"] for a in aliases_raw if isinstance(a, dict)]

        dob = self._first_date(entity, _P_DOB)
        dod = self._first_date(entity, _P_DOD)

        place_of_birth = self._first_place_label(entity, _P_PLACE_OF_BIRTH)
        place_of_death = self._first_place_label(entity, _P_PLACE_OF_DEATH)

        sex_qid = self._first_entity_qid(entity, _P_SEX)
        sex_or_gender = self._resolve_label(sex_qid)

        citizenship_qids = self._all_entity_qids(entity, _P_CITIZENSHIP)
        citizenships = [self._resolve_label(q) or q for q in citizenship_qids]

        occupation_qids = self._all_entity_qids(entity, _P_OCCUPATION)
        occupations = [self._resolve_label(q) or q for q in occupation_qids]

        spouses = self._extract_spouses(entity)

        lead_paragraph: str | None = None
        if wp_title:
            raw_extract = (self._batch_pages[wp_title].get("extract")
                           if self._batch_pages is not None else self._wp.get_extract(wp_title))
            if raw_extract:
                paras = [p.strip() for p in raw_extract.split("\n") if p.strip()]
                lead_paragraph = _strip_html(paras[0]) if paras else None

        images = self._extract_images(entity, wp_title)

        return Person(
            qid=qid,
            wikipedia_title=wp_title,
            wikipedia_url=wp_url,
            name=name,
            description=description,
            aliases=aliases,
            date_of_birth=dob,
            date_of_death=dod,
            place_of_birth=place_of_birth,
            place_of_death=place_of_death,
            sex_or_gender=sex_or_gender,
            citizenships=citizenships,
            occupations=occupations,
            spouses=spouses,
            images=images,
            lead_paragraph=lead_paragraph,
            fetched_at=fetched_at,
            lastrevid=lastrevid,
        )

    def _first_date(self, entity: dict[str, Any], pid: str) -> DateValue | None:
        vals = _claim_values(entity, pid)
        for val in vals:
            dv = _parse_time(val)
            if dv is not None:
                return dv
        return None

    def _first_entity_qid(self, entity: dict[str, Any], pid: str) -> str | None:
        claims = _best_claims(entity, pid)
        for claim in claims:
            qid = _entity_qid_from_snak(claim.get("mainsnak", {}))
            if qid:
                return qid
        return None

    def _all_entity_qids(self, entity: dict[str, Any], pid: str) -> list[str]:
        claims = _valid_claims(entity, pid) if pid == _P_SPOUSE else _best_claims(entity, pid)
        result = []
        for claim in claims:
            qid = _entity_qid_from_snak(claim.get("mainsnak", {}))
            if qid:
                result.append(qid)
        return result

    def _first_place_label(self, entity: dict[str, Any], pid: str) -> str | None:
        qid = self._first_entity_qid(entity, pid)
        return self._resolve_label(qid)

    def _resolve_label(self, qid: str | None) -> str | None:
        if not qid:
            return None
        labels = (self._batch_labels if self._batch_labels is not None
                  else self._wd.get_labels([qid]))
        return labels.get(qid)

    def _extract_spouses(self, entity: dict[str, Any]) -> list[SpouseRecord]:
        claims = _valid_claims(entity, _P_SPOUSE)
        spouse_qids = [_entity_qid_from_snak(c.get("mainsnak", {})) for c in claims]
        valid_qids = [q for q in spouse_qids if q]
        labels = (self._batch_labels if self._batch_labels is not None
                  else self._wd.get_labels(valid_qids))

        spouses = []
        for claim in claims:
            classified = _classify_spouse_claim(claim)
            qid = classified["qid"]
            if not qid:
                continue
            name = labels.get(qid, qid)
            for qualifier in claim.get("qualifiers", {}).get("P1534", []):
                cause_qid = _entity_qid_from_snak(qualifier)
                if cause_qid:
                    classified["end_cause"] = self._resolve_label(cause_qid) or cause_qid
                    break
            spouses.append(
                SpouseRecord(
                    qid=qid,
                    name=name,
                    start_date=classified["start_date"],
                    end_date=classified["end_date"],
                    end_cause=classified["end_cause"],
                    is_former=classified["is_former"],
                )
            )
        return spouses

    def _extract_images(self, entity: dict[str, Any], wp_title: str | None) -> list[ImageRef]:
        if not wp_title:
            return []

        raw_filenames = (self._batch_pages[wp_title]["images"] if self._batch_pages is not None
                         else self._wp.get_image_list(wp_title))
        candidate_filenames = [
            _FILE_PREFIX_RE.sub("", f)
            for f in raw_filenames
            if not _looks_like_chrome(_FILE_PREFIX_RE.sub("", f))
        ]

        if not candidate_filenames:
            return []

        alt_map = (self._batch_alts.get(wp_title, {}) if self._batch_alts is not None
                   else self._rest.extract_image_alts(wp_title))

        images: list[ImageRef] = []
        is_first = True
        for filename in candidate_filenames[:20]:
            info = (self._batch_infos[f"File:{filename}"] if self._batch_infos is not None
                    else self._commons.get_image_info(f"File:{filename}"))
            if not info:
                continue

            url = info.get("url") or ""
            thumburl = info.get("thumburl") or url
            width = info.get("width")
            height = info.get("height")

            extmeta: dict[str, Any] = info.get("extmetadata", {})
            description = _strip_html((extmeta.get("ImageDescription") or {}).get("value"))
            license_val = (extmeta.get("LicenseShortName") or {}).get("value")
            artist = _strip_html((extmeta.get("Artist") or {}).get("value"))

            alt = alt_map.get(_file_key(filename))

            images.append(
                ImageRef(
                    filename=filename,
                    url=url,
                    thumbnail_url=thumburl,
                    width=int(width) if width is not None else None,
                    height=int(height) if height is not None else None,
                    alt=alt,
                    description=description,
                    caption=None,
                    license=license_val,
                    artist=artist,
                    is_lead=is_first,
                )
            )
            is_first = False

        return images
