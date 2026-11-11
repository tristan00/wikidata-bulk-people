import pathlib
from collections.abc import Generator
from typing import Literal

__version__ = "0.1.1"

from wikidata_bulk_people._models import (
    BulkOptions,
    IncompleteRunError,
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
    "BulkOptions",
    "IncompleteRunError",
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
    interactive: bool = True,
) -> Person:
    from wikidata_bulk_people._extract import PersonExtractor

    extractor = PersonExtractor(user_agent=user_agent, interactive=interactive)
    try:
        return extractor.extract(qid_or_title)
    finally:
        extractor.close()


def extract_people(
    out: str | pathlib.Path,
    *,
    filter: PeopleFilter | None = None,
    resume: bool = True,
    user_agent: str = f"wikidata-bulk-people/{__version__} (https://github.com/tristan00/wikidata-bulk-people)",
    options: BulkOptions | None = None,
    if_exists: Literal["fail", "append", "replace"] = "append",
) -> None:
    from wikidata_bulk_people._sinks import JSONLSink
    from wikidata_bulk_people._bulk import run_job

    out_path = out if isinstance(out, pathlib.Path) else pathlib.Path(out)
    state_path = pathlib.Path(str(out_path) + ".state.json")
    legacy_state = out_path.with_suffix("").with_suffix(".state.json")
    job_candidate = (pathlib.Path(options.job_path) if options and options.job_path
                     else pathlib.Path(str(state_path) + ".job.sqlite3"))
    if (resume and not job_candidate.exists() and legacy_state != state_path
            and legacy_state.exists() and out_path.exists()):
        raise ValueError("Legacy JSONL state cannot guarantee integrity; use a new destination or resume=False.")
    run_job(
        JSONLSink(out_path, if_exists=if_exists),
        filter=filter if filter is not None else PeopleFilter(),
        resume=resume,
        user_agent=user_agent,
        state_path=state_path,
        options=options,
    )


def iter_people(
    *,
    filter: PeopleFilter | None = None,
    user_agent: str = f"wikidata-bulk-people/{__version__} (https://github.com/tristan00/wikidata-bulk-people)",
    options: BulkOptions | None = None,
) -> Generator[Person]:
    from wikidata_bulk_people._bulk import iter_records

    yield from iter_records(
        filter=filter if filter is not None else PeopleFilter(),
        user_agent=user_agent,
        options=options,
    )


def extract_people_to_csv(
    directory: str | pathlib.Path,
    *,
    filter: PeopleFilter | None = None,
    resume: bool = True,
    user_agent: str = f"wikidata-bulk-people/{__version__} (https://github.com/tristan00/wikidata-bulk-people)",
    if_exists: Literal["fail", "append", "replace"] = "fail",
    options: BulkOptions | None = None,
) -> None:
    from wikidata_bulk_people._bulk import run_job
    from wikidata_bulk_people._sinks import CSVSink

    dir_path = directory if isinstance(directory, pathlib.Path) else pathlib.Path(directory)
    sink = CSVSink(dir_path, if_exists=if_exists)
    run_job(
        sink,
        filter=filter if filter is not None else PeopleFilter(),
        resume=resume,
        user_agent=user_agent,
        state_path=sink.state_path,
        options=options,
    )


def extract_people_to_db(
    connection_string: str,
    *,
    filter: PeopleFilter | None = None,
    resume: bool = True,
    user_agent: str = f"wikidata-bulk-people/{__version__} (https://github.com/tristan00/wikidata-bulk-people)",
    if_exists: Literal["fail", "append", "replace", "upsert"] = "fail",
    table_prefix: str = "",
    batch_size: int = 500,
    options: BulkOptions | None = None,
) -> None:
    from wikidata_bulk_people._bulk import run_job
    from wikidata_bulk_people._sinks import DatabaseSink

    sink = DatabaseSink(
        connection_string,
        if_exists=if_exists,
        table_prefix=table_prefix,
        batch_size=batch_size,
    )
    run_job(
        sink,
        filter=filter if filter is not None else PeopleFilter(),
        resume=resume,
        user_agent=user_agent,
        state_path=None,
        options=options,
    )


def extract_people_to_memory(
    *,
    filter: PeopleFilter | None = None,
    user_agent: str = f"wikidata-bulk-people/{__version__} (https://github.com/tristan00/wikidata-bulk-people)",
    options: BulkOptions | None = None,
) -> list[Person]:
    return list(iter_people(filter=filter, user_agent=user_agent, options=options))
