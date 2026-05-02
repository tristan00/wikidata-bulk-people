"""Person extraction, QID iteration, bulk pipeline, and output sinks."""

import dataclasses
import json
import logging
import pathlib
import re
import signal
import sys
from collections.abc import Generator
from datetime import datetime, timezone
from types import TracebackType
from typing import IO, Any, Protocol, runtime_checkable

from wikidata_bulk_people._clients import (
    BaseClient,
    CommonsClient,
    RestClient,
    WikidataClient,
    WikipediaClient,
)
from wikidata_bulk_people._models import (
    DateValue,
    ExtractionError,
    ImageRef,
    PeopleFilter,
    Person,
    SpouseRecord,
    TransportError,
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

# ---------------------------------------------------------------------------
# StateFile
# ---------------------------------------------------------------------------


class StateFile:
    """Reads and writes pipeline state as JSON using atomic rename.

    The state schema is::

        {
          "completed_partitions": [year_or_null, ...],
          "in_progress": {"partition": year_or_null, "last_qid": "Q12345"}
        }

    Writes use a temp file + atomic rename so a crash between write and rename
    never leaves a corrupt state file.

    Args:
        path: Path to the state ``.json`` file.
    """

    _DEFAULT: dict[str, Any] = {
        "completed_partitions": [],
        "in_progress": None,
    }

    def __init__(self, path: pathlib.Path) -> None:
        self._path = path
        self._tmp = path.with_suffix(".tmp")

    def read(self) -> dict[str, Any]:
        """Read and return current state, returning defaults if file absent."""
        if not self._path.exists():
            return dict(self._DEFAULT)
        with self._path.open(encoding="utf-8") as f:
            data: dict[str, Any] = json.load(f)
        return {**self._DEFAULT, **data}

    def write(self, state: dict[str, Any]) -> None:
        """Atomically write *state* to disk.

        Uses write-to-temp then rename to ensure the file is either fully
        written or untouched, even if the process is killed mid-write.

        Args:
            state: State dict to persist.
        """
        self._tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
        self._tmp.replace(self._path)

    def mark_completed(self, partition: int | None) -> None:
        """Mark a partition as completed and clear in_progress."""
        state = self.read()
        completed: list[int | None] = state.get("completed_partitions", [])
        if partition not in completed:
            completed.append(partition)
        state["completed_partitions"] = completed
        state["in_progress"] = None
        self.write(state)

    def set_in_progress(self, partition: int | None, last_qid: str) -> None:
        """Update the in-progress cursor for the current partition."""
        state = self.read()
        state["in_progress"] = {"partition": partition, "last_qid": last_qid}
        self.write(state)


# ---------------------------------------------------------------------------
# JSONLSink
# ---------------------------------------------------------------------------


class JSONLSink:
    """Context manager that appends :class:`~wikidata_bulk_people.Person` records as JSONL.

    Each :meth:`write` call serialises one :class:`~wikidata_bulk_people.Person` to a
    JSON line and flushes immediately so partial output survives process failure.

    Args:
        path: Path to the output ``.jsonl`` file (opened in append mode).
    """

    def __init__(self, path: pathlib.Path) -> None:
        self._path = path
        self._file: IO[str] | None = None

    def __enter__(self) -> "JSONLSink":
        self._file = self._path.open("a", encoding="utf-8")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def write(self, person: Person) -> None:
        """Append one Person record as a JSON line.

        Args:
            person: A :class:`~wikidata_bulk_people.Person` dataclass instance.
        """
        if self._file is None:
            raise RuntimeError("JSONLSink must be used as a context manager")
        d: dict[str, Any] = dataclasses.asdict(person)
        self._file.write(json.dumps(d, default=str) + "\n")
        self._file.flush()


# ---------------------------------------------------------------------------
# Sink protocols
# ---------------------------------------------------------------------------


class Sink(Protocol):
    """Protocol for pipeline output destinations."""

    def __enter__(self) -> "Sink": ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...

    def write(self, person: "Person") -> None: ...


@runtime_checkable
class StatefulSink(Protocol):
    """Protocol for sinks that store pipeline state internally (e.g. DatabaseSink)."""

    def read_pipeline_state(self) -> dict[str, Any]: ...

    def write_pipeline_state(self, state: dict[str, Any]) -> None: ...


class _NullState:
    """No-op state manager used when resume=False and no state_path is given."""

    def read(self) -> dict[str, Any]:
        return {"completed_partitions": [], "in_progress": None}

    def write(self, state: dict[str, Any]) -> None:  # noqa: ARG002
        pass

    def mark_completed(self, partition: int | None) -> None:  # noqa: ARG002
        pass

    def set_in_progress(self, partition: int | None, last_qid: str) -> None:  # noqa: ARG002
        pass


class _SinkStateAdapter:
    """Adapts a :class:`StatefulSink` to the :class:`StateFile` interface."""

    _DEFAULT: dict[str, Any] = {"completed_partitions": [], "in_progress": None}

    def __init__(self, sink: StatefulSink) -> None:
        self._sink = sink

    def read(self) -> dict[str, Any]:
        data = self._sink.read_pipeline_state()
        return {**self._DEFAULT, **data}

    def write(self, state: dict[str, Any]) -> None:
        self._sink.write_pipeline_state(state)

    def mark_completed(self, partition: int | None) -> None:
        state = self.read()
        completed: list[int | None] = state.get("completed_partitions", [])
        if partition not in completed:
            completed.append(partition)
        state["completed_partitions"] = completed
        state["in_progress"] = None
        self.write(state)

    def set_in_progress(self, partition: int | None, last_qid: str) -> None:
        state = self.read()
        state["in_progress"] = {"partition": partition, "last_qid": last_qid}
        self.write(state)


# ---------------------------------------------------------------------------
# SPARQL query builder
# ---------------------------------------------------------------------------

# Birth-year range covered by birth-year partitions.
# Years outside this range are captured in the no-DOB bucket (partition_year=None).
_MIN_YEAR = -3000
_MAX_YEAR = 2030

# Step size for birth-year partitions (one year per partition for fine granularity).
_YEAR_STEP = 1

# Page size for keyset-paginated SPARQL queries.
_PAGE_SIZE = 500


def _qid_int(qid: str) -> int:
    """Return the numeric part of a QID string, e.g. "Q937" → 937."""
    return int(qid.lstrip("Q"))


def _year_partitions(filter: PeopleFilter) -> list[int | None]:  # noqa: A002
    """Return the list of birth-year partitions for *filter*, plus the no-DOB bucket.

    Each year in the range is one partition. The no-DOB bucket is always appended
    last so it can be independently resumed.

    Args:
        filter: A :class:`PeopleFilter` instance.

    Returns:
        List of integer years plus ``None`` for the no-DOB bucket.
    """
    start = filter.born_after if filter.born_after is not None else _MIN_YEAR
    end = filter.born_before if filter.born_before is not None else _MAX_YEAR
    years: list[int | None] = list(range(start, end + 1, _YEAR_STEP))
    years.append(None)  # no-DOB bucket always included
    return years


def _build_query(
    filter: PeopleFilter,  # noqa: A002
    partition_year: int | None,
    last_qid: str | None,
) -> str:
    """Build a keyset-paginated WDQS SPARQL query for a single partition.

    Args:
        filter: Filter parameters to apply.
        partition_year: Birth year for this partition, or None for the no-DOB bucket.
        last_qid: Last QID seen in this partition (keyset cursor), or None to start.

    Returns:
        A SPARQL query string ready to POST to the WDQS endpoint.
    """
    constraints = _build_constraints(filter, partition_year)

    cursor_clause = ""
    if last_qid:
        cursor_clause = f"FILTER(?item > wd:{last_qid})"

    body = "\n  ".join(constraints)
    query = f"""SELECT ?item WHERE {{
  {body}
  {cursor_clause}
}}
ORDER BY ?item
LIMIT {_PAGE_SIZE}"""
    return query


def _build_constraints(
    filter: PeopleFilter,  # noqa: A002
    partition_year: int | None,
) -> list[str]:
    """Build the SPARQL WHERE clause constraint lines for a query."""
    constraints: list[str] = []

    constraints.append("?item wdt:P31 wd:Q5.")

    if partition_year is not None:
        year_str = str(partition_year)
        constraints.append(f"?item wdt:P569 ?dob. FILTER(YEAR(?dob) = {year_str})")
    else:
        constraints.append("MINUS { ?item wdt:P569 [] }")

    if filter.occupation_qid:
        constraints.append(f"?item wdt:P106 wd:{filter.occupation_qid}.")
    if filter.citizenship_qid:
        constraints.append(f"?item wdt:P27 wd:{filter.citizenship_qid}.")
    if filter.gender_qid:
        constraints.append(f"?item wdt:P21 wd:{filter.gender_qid}.")
    if filter.religion_qid:
        constraints.append(f"?item wdt:P140 wd:{filter.religion_qid}.")
    if filter.award_qid:
        constraints.append(f"?item wdt:P166 wd:{filter.award_qid}.")
    if filter.political_ideology_qid:
        constraints.append(f"?item wdt:P1142 wd:{filter.political_ideology_qid}.")
    if filter.has_wikipedia_article:
        constraints.append(
            "?article schema:about ?item; schema:isPartOf <https://en.wikipedia.org/>."
        )
    if filter.living is True:
        constraints.append("FILTER NOT EXISTS { ?item wdt:P570 [] }")
    elif filter.living is False:
        constraints.append("?item wdt:P570 [].")

    return constraints


# ---------------------------------------------------------------------------
# QIDStream
# ---------------------------------------------------------------------------

_WDQS = "https://query.wikidata.org/sparql"


class _SparqlClient(BaseClient):
    """Thin wrapper around BaseClient for WDQS SPARQL queries."""

    def run_query(self, query: str) -> list[dict[str, Any]]:
        """Execute a SPARQL SELECT query and return the result bindings.

        Args:
            query: SPARQL query string.

        Returns:
            List of binding dicts from the JSON response.
        """
        data = self._get(_WDQS, params={"query": query, "format": "json"})
        return list(data.get("results", {}).get("bindings", []))


def _qid_from_binding(binding: dict[str, Any]) -> str | None:
    """Extract a QID string from a SPARQL result binding dict."""
    value = binding.get("item", {}).get("value", "")
    if not value:
        return None
    return value.rsplit("/", 1)[-1] or None


class QIDStream:
    """Yields Wikidata QIDs for all persons matching *filter* via keyset-paginated SPARQL.

    Iterates birth-year partitions sequentially. Within each partition, pages are
    fetched in order using a keyset cursor so queries are resumable without OFFSET.

    Args:
        filter: A :class:`PeopleFilter` controlling which persons are included.
        user_agent: User-Agent string for all HTTP requests.
    """

    def __init__(self, filter: PeopleFilter, user_agent: str) -> None:  # noqa: A002
        self._filter = filter
        self._sparql = _SparqlClient(user_agent=user_agent)

    def __iter__(self) -> Generator[str, None, None]:
        partitions = _year_partitions(self._filter)
        for i, partition_year in enumerate(partitions):
            label = str(partition_year) if partition_year is not None else "no-DOB"
            logger.info(
                "QIDStream: starting partition %s (%d/%d)",
                label,
                i + 1,
                len(partitions),
            )
            yield from self.iter_partition(partition_year)

    def iter_partition(self, partition_year: int | None) -> Generator[str, None, None]:
        last_qid: str | None = None
        page = 0
        while True:
            query = _build_query(self._filter, partition_year, last_qid)
            bindings = self._sparql.run_query(query)
            page += 1
            if not bindings:
                break
            for binding in bindings:
                qid = _qid_from_binding(binding)
                if qid:
                    yield qid
                    last_qid = qid
            logger.debug(
                "QIDStream: partition=%s page=%d yielded=%d last=%s",
                partition_year,
                page,
                len(bindings),
                last_qid,
            )
            if len(bindings) < _PAGE_SIZE:
                break


# ---------------------------------------------------------------------------
# PersonExtractor
# ---------------------------------------------------------------------------

# Wikidata property IDs used in person extraction
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
    """Orchestrates all four Wikimedia API clients to build a :class:`Person` record.

    Args:
        user_agent: User-Agent string for all HTTP requests.
    """

    def __init__(self, user_agent: str) -> None:
        self._wd = WikidataClient(user_agent=user_agent)
        self._wp = WikipediaClient(user_agent=user_agent)
        self._commons = CommonsClient(user_agent=user_agent)
        self._rest = RestClient(user_agent=user_agent)

    def extract(self, qid_or_title: str) -> Person:
        """Extract a Person record from Wikidata + Wikipedia.

        Args:
            qid_or_title: Wikidata QID (e.g. "Q937") or Wikipedia title.

        Returns:
            A fully populated :class:`Person` dataclass.

        Raises:
            NotFoundError: If the entity is not found.
            ExtractionError: If the entity is not a human (Q5).
        """
        fetched_at = datetime.now(tz=timezone.utc)

        if re.match(r"^Q\d+$", qid_or_title, re.IGNORECASE):
            entity = self._wd.get_entity(qid_or_title)
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
            raw_extract = self._wp.get_extract(wp_title)
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
        claims = entity.get("claims", {}).get(pid, [])
        for claim in claims:
            qid = _entity_qid_from_snak(claim.get("mainsnak", {}))
            if qid:
                return qid
        return None

    def _all_entity_qids(self, entity: dict[str, Any], pid: str) -> list[str]:
        claims = entity.get("claims", {}).get(pid, [])
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
        labels = self._wd.get_labels([qid])
        return labels.get(qid)

    def _extract_spouses(self, entity: dict[str, Any]) -> list[SpouseRecord]:
        claims = entity.get("claims", {}).get(_P_SPOUSE, [])
        spouse_qids = [_entity_qid_from_snak(c.get("mainsnak", {})) for c in claims]
        valid_qids = [q for q in spouse_qids if q]
        labels = self._wd.get_labels(valid_qids) if valid_qids else {}

        spouses = []
        for claim in claims:
            classified = _classify_spouse_claim(claim)
            qid = classified["qid"]
            if not qid:
                continue
            name = labels.get(qid, qid)
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

        raw_filenames = self._wp.get_image_list(wp_title)
        candidate_filenames = [
            _FILE_PREFIX_RE.sub("", f)
            for f in raw_filenames
            if not _looks_like_chrome(_FILE_PREFIX_RE.sub("", f))
        ]

        if not candidate_filenames:
            return []

        alt_map = self._rest.extract_image_alts(wp_title)

        images: list[ImageRef] = []
        is_first = True
        for filename in candidate_filenames[:20]:
            info = self._commons.get_image_info(f"File:{filename}")
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

            alt = alt_map.get(filename.lower())

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


# ---------------------------------------------------------------------------
# People pipeline
# ---------------------------------------------------------------------------


def iter_people_pipeline(
    *,
    filter: PeopleFilter,  # noqa: A002
    user_agent: str,
) -> Generator[Person, None, None]:
    """Yield :class:`Person` records for all people matching *filter*.

    Each record is extracted from Wikidata + Wikipedia. Per-record errors
    (``ExtractionError``) are logged and skipped; ``TransportError`` propagates.

    Args:
        filter: Which people to include.
        user_agent: HTTP User-Agent string.

    Yields:
        :class:`~wikidata_bulk_people.Person` objects in partition order.
    """
    extractor = PersonExtractor(user_agent=user_agent)
    stream = QIDStream(filter=filter, user_agent=user_agent)

    for qid in stream:
        try:
            person = extractor.extract(qid)
        except ExtractionError as exc:
            logger.warning("Skipping %s: %s", qid, exc)
            continue
        yield person


def run_people_pipeline(
    sink: Sink,
    *,
    filter: PeopleFilter,  # noqa: A002
    resume: bool,
    user_agent: str,
    state_path: pathlib.Path | None,
) -> None:
    """Run the full people pipeline, writing results to *sink*.

    Supports resumable extraction via a state backend. Sinks that implement
    :class:`StatefulSink` manage their own state (e.g. a database table);
    all other sinks use a :class:`StateFile` at *state_path*.

    Args:
        sink: Output destination; must be a context manager with a ``write`` method.
        filter: Which people to include.
        resume: If True, skip already-completed partitions and resume from
            the last ``in_progress`` cursor.
        user_agent: HTTP User-Agent string.
        state_path: Path for the state ``.json`` file. Required when *sink* does
            not implement :class:`StatefulSink`. Ignored when it does.
    """
    state_mgr: StateFile | _SinkStateAdapter | _NullState
    if isinstance(sink, StatefulSink):
        state_mgr = _SinkStateAdapter(sink)
    elif state_path is not None:
        state_mgr = StateFile(state_path)
    elif not resume:
        state_mgr = _NullState()
    else:
        raise ValueError(
            "state_path must be provided when resume=True and the sink does not manage "
            "its own state"
        )

    extractor = PersonExtractor(user_agent=user_agent)
    partitions = _year_partitions(filter)

    def _sigint_handler(signum: int, frame: object) -> None:  # noqa: ARG001
        logger.info("Interrupted — state saved")
        sys.exit(130)

    old_handler = signal.signal(signal.SIGINT, _sigint_handler)

    extracted = skipped = 0

    try:
        with sink:
            # State must be read after sink.__enter__() so that stateful sinks
            # (e.g. DatabaseSink) have an open connection before reading.
            state = state_mgr.read() if resume else {"completed_partitions": [], "in_progress": None}
            completed: set[int | None] = set(state.get("completed_partitions", []))
            in_progress: dict[str, object] | None = state.get("in_progress")

            for partition_year in partitions:
                if partition_year in completed:
                    logger.debug("Skipping completed partition %s", partition_year)
                    continue

                resume_from: str | None = None
                if in_progress and in_progress.get("partition") == partition_year:
                    resume_from = str(in_progress["last_qid"])
                    logger.info(
                        "Resuming partition %s from QID %s",
                        partition_year,
                        resume_from,
                    )
                    in_progress = None

                label = str(partition_year) if partition_year is not None else "no-DOB"
                logger.info("Pipeline: starting partition %s", label)

                last_qid: str | None = None
                stream = QIDStream(filter=filter, user_agent=user_agent)

                for qid in stream.iter_partition(partition_year):
                    if resume_from is not None:
                        if _qid_int(qid) <= _qid_int(resume_from):
                            continue
                        resume_from = None

                    try:
                        person = extractor.extract(qid)
                    except ExtractionError as exc:
                        logger.warning("Skipping %s: %s", qid, exc)
                        skipped += 1
                        continue
                    except TransportError:
                        logger.error("Transport error on %s — saving state and re-raising", qid)
                        if last_qid:
                            state_mgr.set_in_progress(partition_year, last_qid)
                        raise

                    sink.write(person)
                    extracted += 1
                    last_qid = qid

                    if extracted % 100 == 0:
                        logger.info(
                            "Pipeline: extracted=%d skipped=%d partition=%s last=%s",
                            extracted,
                            skipped,
                            label,
                            qid,
                        )
                        state_mgr.set_in_progress(partition_year, qid)

                state_mgr.mark_completed(partition_year)
                logger.info("Pipeline: completed partition %s", label)

    finally:
        signal.signal(signal.SIGINT, old_handler)

    logger.info(
        "Pipeline: done — extracted=%d skipped=%d",
        extracted,
        skipped,
    )
