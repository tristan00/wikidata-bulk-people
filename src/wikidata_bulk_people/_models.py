from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from wikidata_bulk_people.enums import (
    Award,
    Country,
    Gender,
    Occupation,
    PoliticalIdeology,
    Religion,
)


_VERSION: str = "0.1.1"


@dataclass
class DateValue:

    year: int
    month: int | None
    day: int | None
    calendar: str
    precision: int


@dataclass
class ImageRef:

    filename: str
    url: str
    thumbnail_url: str
    width: int | None
    height: int | None
    alt: str | None
    description: str | None
    caption: str | None
    license: str | None
    artist: str | None
    is_lead: bool = False


@dataclass
class SpouseRecord:

    qid: str
    name: str
    start_date: DateValue | None
    end_date: DateValue | None
    end_cause: str | None
    is_former: bool


@dataclass
class Person:

    qid: str
    wikipedia_title: str | None
    wikipedia_url: str | None
    name: str
    description: str | None
    aliases: list[str]
    date_of_birth: DateValue | None
    date_of_death: DateValue | None
    place_of_birth: str | None
    place_of_death: str | None
    sex_or_gender: str | None
    citizenships: list[str]
    occupations: list[str]
    spouses: list[SpouseRecord]
    images: list[ImageRef]
    lead_paragraph: str | None
    fetched_at: datetime
    lastrevid: int
    extractor_version: str = field(default_factory=lambda: _VERSION)
    schema_version: str = "1"


@dataclass
class PeopleFilter:

    born_after: int | None = None
    born_before: int | None = None
    occupation_qid: Occupation | str | None = None
    citizenship_qid: Country | str | None = None
    gender_qid: Gender | str | None = None
    religion_qid: Religion | str | None = None
    award_qid: Award | str | None = None
    political_ideology_qid: PoliticalIdeology | str | None = None
    has_wikipedia_article: bool = True
    living: bool | None = None
    year_partition: bool = False
    ordered: bool = True


@dataclass
class BulkOptions:

    batch_size: int = 50
    workers: int = 4
    job_path: Path | None = None
    cache_path: Path | None = None
    cache_ttl: float = 86400
    limit: int | None = None
    public_snapshot: Path | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.batch_size <= 50:
            raise ValueError("batch_size must be between 1 and 50")
        if not 1 <= self.workers <= 4:
            raise ValueError("workers must be between 1 and 4")
        if self.cache_ttl < 0:
            raise ValueError("cache_ttl must be nonnegative")
        if self.limit is not None and self.limit < 0:
            raise ValueError("limit must be nonnegative")
        if self.public_snapshot is not None and (self.limit is None or self.limit < 1):
            raise ValueError("public_snapshot requires a positive limit")


class WikiUtilityError(Exception):
    pass


class IncompleteRunError(WikiUtilityError):
    pass


class NotFoundError(WikiUtilityError):

    def __init__(self, qid_or_title: str) -> None:
        super().__init__(f"Entity not found: {qid_or_title!r}")
        self.qid_or_title = qid_or_title


class TransportError(WikiUtilityError):

    def __init__(self, url: str, status_code: int | None = None, reason: str = "") -> None:
        detail = f" (HTTP {status_code})" if status_code else ""
        msg = reason or f"Transport failure{detail}: {url}"
        super().__init__(msg)
        self.url = url
        self.status_code = status_code


class ExtractionError(WikiUtilityError):

    def __init__(self, qid: str, reason: str) -> None:
        super().__init__(f"Cannot extract {qid!r}: {reason}")
        self.qid = qid
        self.reason = reason
