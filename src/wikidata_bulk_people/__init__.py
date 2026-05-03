"""wikidata-bulk-people: Extract structured records from Wikipedia and Wikidata."""

import pathlib
from collections.abc import Generator
from typing import Literal

__version__ = "0.1.1"

from wikidata_bulk_people._models import (
    DateValue,
    ExtractionError,
    ImageRef,
    NotFoundError,
    PeopleFilter,
    Person,
    SpouseRecord,
    TransportError,
    WikiUtilityError,
)
from wikidata_bulk_people.enums import (
    Award,
    Country,
    Gender,
    Occupation,
    PoliticalIdeology,
    Religion,
)

__all__ = [
    "extract_people",
    "extract_people_to_csv",
    "extract_people_to_db",
    "extract_people_to_memory",
    "extract_person",
    "iter_people",
    "Person",
    "SpouseRecord",
    "ImageRef",
    "DateValue",
    "PeopleFilter",
    "Occupation",
    "Country",
    "Gender",
    "Religion",
    "Award",
    "PoliticalIdeology",
    "WikiUtilityError",
    "NotFoundError",
    "TransportError",
    "ExtractionError",
]


def extract_person(
    qid_or_title: str,
    *,
    user_agent: str = f"wikidata-bulk-people/{__version__} (https://github.com/tristan00/wikidata-bulk-people)",
) -> Person:
    """Extract a single person record by Wikidata QID or Wikipedia article title.

    Args:
        qid_or_title: A Wikidata QID (e.g. "Q937") or a Wikipedia article title
            (e.g. "Albert Einstein").
        user_agent: HTTP User-Agent header value sent to all Wikimedia APIs.

    Returns:
        A fully populated :class:`Person` dataclass.

    Raises:
        NotFoundError: If no matching entity is found.
        ExtractionError: If the entity exists but is not a person (Q5).
        TransportError: If all network retries are exhausted.

    Example::

        from wikidata_bulk_people import extract_person
        person = extract_person("Q937")
        print(person.name, person.date_of_birth)
    """
    from wikidata_bulk_people._extract import PersonExtractor

    return PersonExtractor(user_agent=user_agent).extract(qid_or_title)


def extract_people(
    out: str | pathlib.Path,
    *,
    filter: PeopleFilter | None = None,  # noqa: A002
    resume: bool = True,
    user_agent: str = f"wikidata-bulk-people/{__version__} (https://github.com/tristan00/wikidata-bulk-people)",
) -> None:
    """Extract all people matching a filter to a streaming JSONL file.

    Args:
        out: Output file path. Written in append mode; safe to resume into
            the same file.
        filter: A :class:`PeopleFilter` specifying which people to extract.
        resume: If True (default), reads the state file alongside *out* and
            resumes from the last checkpoint. If False, starts fresh.
        user_agent: HTTP User-Agent header value sent to all Wikimedia APIs.

    Raises:
        TransportError: If a partition's retries are completely exhausted.

    Example::

        from wikidata_bulk_people import extract_people, PeopleFilter
        extract_people("people.jsonl", filter=PeopleFilter(born_after=1990))
    """
    from wikidata_bulk_people._extract import JSONLSink, run_people_pipeline

    out_path = out if isinstance(out, pathlib.Path) else pathlib.Path(out)
    state_path = out_path.with_suffix("").with_suffix(".state.json")
    run_people_pipeline(
        JSONLSink(out_path),
        filter=filter if filter is not None else PeopleFilter(),
        resume=resume,
        user_agent=user_agent,
        state_path=state_path,
    )


def iter_people(
    *,
    filter: PeopleFilter | None = None,  # noqa: A002
    user_agent: str = f"wikidata-bulk-people/{__version__} (https://github.com/tristan00/wikidata-bulk-people)",
) -> Generator[Person]:
    """Iterate over all people matching a filter, yielding :class:`Person` objects.

    This is the primary API for processing people records in Python. Use this when
    you want to work with each record as a ``Person`` object rather than writing
    to a file.

    Args:
        filter: A :class:`PeopleFilter` specifying which people to include.
            Defaults to all people with an English Wikipedia article.
        user_agent: HTTP User-Agent header value sent to all Wikimedia APIs.

    Yields:
        :class:`Person` objects in QID-ascending order within each birth-year partition.

    Raises:
        TransportError: If a partition's retries are completely exhausted.

    Example::

        from wikidata_bulk_people import iter_people, PeopleFilter

        # Iterate over all people (millions of records):
        for person in iter_people():
            print(person.name, person.date_of_birth)

        # Or with a filter:
        physicists = list(iter_people(filter=PeopleFilter(occupation_qid="Q169470")))
        print(f"Found {{len(physicists)}} physicists")
    """
    from wikidata_bulk_people._extract import iter_people_pipeline

    yield from iter_people_pipeline(
        filter=filter if filter is not None else PeopleFilter(),
        user_agent=user_agent,
    )


def extract_people_to_csv(
    directory: str | pathlib.Path,
    *,
    filter: PeopleFilter | None = None,  # noqa: A002
    resume: bool = True,
    user_agent: str = f"wikidata-bulk-people/{__version__} (https://github.com/tristan00/wikidata-bulk-people)",
    if_exists: Literal["fail", "append", "replace"] = "fail",
) -> None:
    """Extract all matching people to a directory of normalized CSV files.

    Creates one file per relation:
    ``people.csv``, ``person_aliases.csv``, ``person_citizenships.csv``,
    ``person_occupations.csv``, ``person_spouses.csv``, ``person_images.csv``.
    Pipeline state is stored in ``.pipeline.state.json`` inside *directory*
    to support resumable extraction.

    Args:
        directory: Target directory path. Created if it does not exist.
        filter: Which people to include.
        resume: Resume from the last checkpoint when True (default).
        user_agent: HTTP User-Agent header value sent to all Wikimedia APIs.
        if_exists: Action when CSV files already exist in *directory*:
            ``"fail"`` (default), ``"append"``, or ``"replace"``.

    Raises:
        FileExistsError: If ``if_exists="fail"`` and CSV files are already present.
        TransportError: If a partition's retries are exhausted.

    Example::

        from wikidata_bulk_people import extract_people_to_csv, PeopleFilter, Occupation
        extract_people_to_csv(
            "writers_csv/",
            filter=PeopleFilter(occupation_qid=Occupation.WRITER, born_after=1900),
        )
    """
    from wikidata_bulk_people._extract import run_people_pipeline
    from wikidata_bulk_people._sinks import CSVSink

    dir_path = directory if isinstance(directory, pathlib.Path) else pathlib.Path(directory)
    sink = CSVSink(dir_path, if_exists=if_exists)
    run_people_pipeline(
        sink,
        filter=filter if filter is not None else PeopleFilter(),
        resume=resume,
        user_agent=user_agent,
        state_path=sink.state_path,
    )


def extract_people_to_db(
    connection_string: str,
    *,
    filter: PeopleFilter | None = None,  # noqa: A002
    resume: bool = True,
    user_agent: str = f"wikidata-bulk-people/{__version__} (https://github.com/tristan00/wikidata-bulk-people)",
    if_exists: Literal["fail", "append", "replace", "upsert"] = "fail",
    table_prefix: str = "",
    batch_size: int = 500,
) -> None:
    """Extract all matching people into a relational database via SQLAlchemy.

    Requires the ``[db]`` extra: ``pip install wikidata-bulk-people[db]``.

    Creates normalized tables ``people``, ``person_aliases``,
    ``person_citizenships``, ``person_occupations``, ``person_spouses``,
    and ``person_images`` (optionally prefixed). Pipeline state is stored
    in a ``{prefix}pipeline_state`` table for resumable extraction.

    Args:
        connection_string: SQLAlchemy URL, e.g. ``"sqlite:///out.db"`` or
            ``"postgresql://user:pass@host/db"``.
        filter: Which people to include.
        resume: Resume from the last checkpoint when True (default).
        user_agent: HTTP User-Agent header value sent to all Wikimedia APIs.
        if_exists: Action when target tables already exist:
            ``"fail"`` (default), ``"append"``, ``"replace"``, or ``"upsert"``.
        table_prefix: Prepended to all table names, e.g. ``"wk_"``.
        batch_size: Person records to buffer before each database flush.

    Raises:
        ValueError: If ``if_exists="fail"`` and tables exist, or on schema mismatch.
        ImportError: If SQLAlchemy is not installed.
        TransportError: If a partition's retries are exhausted.

    Example::

        from wikidata_bulk_people import extract_people_to_db, PeopleFilter, Occupation
        extract_people_to_db(
            "sqlite:///writers.db",
            filter=PeopleFilter(occupation_qid=Occupation.WRITER, born_after=1900),
        )
    """
    from wikidata_bulk_people._extract import run_people_pipeline
    from wikidata_bulk_people._sinks import DatabaseSink

    sink = DatabaseSink(
        connection_string,
        if_exists=if_exists,
        table_prefix=table_prefix,
        batch_size=batch_size,
    )
    run_people_pipeline(
        sink,
        filter=filter if filter is not None else PeopleFilter(),
        resume=resume,
        user_agent=user_agent,
        state_path=None,  # DatabaseSink implements StatefulSink
    )


def extract_people_to_memory(
    *,
    filter: PeopleFilter | None = None,  # noqa: A002
    user_agent: str = f"wikidata-bulk-people/{__version__} (https://github.com/tristan00/wikidata-bulk-people)",
) -> list[Person]:
    """Extract all matching people and return them as a list.

    Loads all results into memory. For very large result sets, prefer
    :func:`iter_people` to stream records one at a time.

    Args:
        filter: Which people to include.
        user_agent: HTTP User-Agent header value sent to all Wikimedia APIs.

    Returns:
        List of :class:`Person` objects.

    Raises:
        TransportError: If a partition's retries are exhausted.

    Example::

        from wikidata_bulk_people import extract_people_to_memory, PeopleFilter, Occupation
        people = extract_people_to_memory(
            filter=PeopleFilter(occupation_qid=Occupation.WRITER, born_after=1900)
        )
        print(f"Loaded {len(people)} writers")
    """
    from wikidata_bulk_people._extract import run_people_pipeline
    from wikidata_bulk_people._sinks import MemorySink

    sink = MemorySink()
    run_people_pipeline(
        sink,
        filter=filter if filter is not None else PeopleFilter(),
        resume=False,
        user_agent=user_agent,
        state_path=None,  # MemorySink does not support resume
    )
    return sink.records
