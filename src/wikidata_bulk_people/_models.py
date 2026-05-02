"""Public dataclasses and exceptions for wikidata-bulk-people."""

from dataclasses import dataclass, field
from datetime import datetime

from wikidata_bulk_people.enums import (
    Award,
    Country,
    Gender,
    Occupation,
    PoliticalIdeology,
    Religion,
)

# Keep in sync with __version__ in __init__.py
_VERSION: str = "0.1.0"


@dataclass
class DateValue:
    """A parsed date from a Wikidata time snak.

    Attributes:
        year: Year (negative for BCE). May be the only field set for year-precision dates.
        month: Month (1-12), or None for year-only precision.
        day: Day (1-31), or None for year/month precision.
        calendar: "gregorian" or "julian".
        precision: Wikidata precision value (9=year, 10=month, 11=day).
    """

    year: int
    month: int | None
    day: int | None
    calendar: str
    precision: int


@dataclass
class ImageRef:
    """A reference to a media file associated with a person.

    Attributes:
        filename: Commons filename without the "File:" prefix.
        url: Full URL to the original image.
        thumbnail_url: URL to a representative thumbnail (≤300 px wide).
        width: Original image width in pixels, or None if unknown.
        height: Original image height in pixels, or None if unknown.
        alt: Alt text for the image, or None if not available.
        description: Short Commons description, or None.
        caption: Caption text from the Wikipedia article, or None.
        license: SPDX licence identifier or human-readable string, or None.
        artist: Attribution string, or None.
        is_lead: True if this is the lead/infobox image for the Wikipedia article.
    """

    filename: str
    url: str
    thumbnail_url: str
    width: int | None
    height: int | None
    alt: str | None
    description: str | None
    caption: str | None
    license: str | None  # noqa: A003
    artist: str | None
    is_lead: bool = False


@dataclass
class SpouseRecord:
    """A single spouse relationship for a person.

    Attributes:
        qid: Wikidata QID of the spouse entity.
        name: Display name of the spouse.
        start_date: Marriage start date, or None if unknown.
        end_date: Marriage end date, or None if still current or unknown.
        end_cause: Reason for end of marriage (e.g. "divorce", "death of partner"), or None.
        is_former: True if the marriage has ended.
    """

    qid: str
    name: str
    start_date: DateValue | None
    end_date: DateValue | None
    end_cause: str | None
    is_former: bool


@dataclass
class Person:
    """A structured record for a single person extracted from Wikipedia/Wikidata.

    Attributes:
        qid: Wikidata QID (e.g. "Q937").
        wikipedia_title: English Wikipedia article title.
        wikipedia_url: Full URL to the English Wikipedia article.
        name: Display name (English label).
        description: Short Wikidata description, or None.
        aliases: List of alternative names.
        date_of_birth: Parsed date of birth, or None.
        date_of_death: Parsed date of death, or None (None for living people).
        place_of_birth: Name of the place of birth, or None.
        place_of_death: Name of the place of death, or None.
        sex_or_gender: Gender label (e.g. "male", "female"), or None.
        citizenships: List of citizenship country names.
        occupations: List of occupation labels.
        spouses: List of spouse relationships.
        images: List of associated images, lead image first.
        lead_paragraph: First paragraph of the Wikipedia article, plain text.
        fetched_at: UTC timestamp when this record was fetched.
        lastrevid: Wikidata entity revision ID at time of fetch.
        extractor_version: wikidata-bulk-people version used to produce this record.
        schema_version: Schema version string (currently "1").
    """

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
    """Filter parameters for bulk person extraction.

    All fields default to "no restriction". Combine multiple fields to
    narrow the result set.

    Attributes:
        born_after: Only include people born after this year (exclusive).
        born_before: Only include people born before this year (exclusive).
        occupation_qid: Only include people with this occupation (P106).
        citizenship_qid: Only include people with this citizenship (P27).
        gender_qid: Only include people with this gender identity (P21).
        religion_qid: Only include people with this religion (P140).
        award_qid: Only include people who received this award (P166).
        political_ideology_qid: Only include people with this political ideology (P1142).
        has_wikipedia_article: Only include people with an English Wikipedia article.
        living: If True, only living people. If False, only deceased. None for both.
    """

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


# ---------------------------------------------------------------------------
# Exceptions (previously in _errors.py)
# ---------------------------------------------------------------------------


class WikiUtilityError(Exception):
    """Base class for all wikidata-bulk-people errors."""


class NotFoundError(WikiUtilityError):
    """Raised when no entity matching the given QID or title is found."""

    def __init__(self, qid_or_title: str) -> None:
        super().__init__(f"Entity not found: {qid_or_title!r}")
        self.qid_or_title = qid_or_title


class TransportError(WikiUtilityError):
    """Raised when all HTTP retries for a request are exhausted."""

    def __init__(self, url: str, status_code: int | None = None, reason: str = "") -> None:
        detail = f" (HTTP {status_code})" if status_code else ""
        msg = reason or f"Transport failure{detail}: {url}"
        super().__init__(msg)
        self.url = url
        self.status_code = status_code


class ExtractionError(WikiUtilityError):
    """Raised when an entity exists but cannot be extracted as the expected type."""

    def __init__(self, qid: str, reason: str) -> None:
        super().__init__(f"Cannot extract {qid!r}: {reason}")
        self.qid = qid
        self.reason = reason
