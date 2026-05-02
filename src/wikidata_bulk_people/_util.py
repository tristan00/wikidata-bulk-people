"""Private parsing and filtering helpers. Not part of the public API."""

import re
from html.parser import HTMLParser
from typing import Any

from wikidata_bulk_people._models import DateValue

# QID of the Gregorian calendar in Wikidata
_GREGORIAN_QID = "Q1985727"

# Image extensions to keep (everything else is filtered as "chrome")
_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".svg", ".tif", ".tiff", ".webp"})

# Patterns that identify non-person/UI images
_CHROME_PATTERNS = re.compile(
    r"""(?xi)
    ^(
        Commons[-_]logo |
        Wikidata[-_]logo |
        Wikipedia[-_]logo |
        Flag[-_]of[-_] |
        OOjs[-_]UI[-_] |
        Edit[-_] |
        Gnome[-_] |
        Nuvola[-_] |
        Crystal[-_]Clear |
        Padlock |
        Disambig |
        Symbol[-_] |
        Ambox |
        Red[-_]Pencil |
        Portal[-_] |
        WP |
        P[-_]vip |
        Coat[-_]of[-_]arms[-_]placeholder |
        Map[-_]of |
        Noimage |
        No[-_]image |
        Question[-_]mark
    )
    """,
    re.IGNORECASE,
)

# Upload URL patterns for filename extraction
_THUMB_RE = re.compile(
    r"/thumb/[0-9a-f]/[0-9a-f]{2}/(.+?)/\d+px-",
    re.IGNORECASE,
)
_DIRECT_RE = re.compile(
    r"upload\.wikimedia\.org/wikipedia/(?:commons|en)/[0-9a-f]/[0-9a-f]{2}/(.+)$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------


def _parse_time(value: dict[str, Any] | None) -> DateValue | None:
    """Parse a Wikidata time snak value into a :class:`DateValue`.

    Args:
        value: The ``value`` dict from a time datavalue, or None.

    Returns:
        A :class:`DateValue`, or None if the input is absent or unparseable.
    """
    if not value:
        return None
    time_str: str = value.get("time", "")
    if not time_str:
        return None
    precision: int = int(value.get("precision", 11))
    calendar_model: str = value.get("calendarmodel", "")
    calendar = "gregorian" if _GREGORIAN_QID in calendar_model else "julian"

    # Format: ±YYYY-MM-DDTHH:MM:SSZ
    sign = -1 if time_str.startswith("-") else 1
    stripped = time_str.lstrip("+-")
    parts = stripped.split("T")[0].split("-")
    if not parts:
        return None

    year = sign * int(parts[0])
    has_month = precision >= 10 and len(parts) > 1 and parts[1] != "00"
    has_day = precision >= 11 and len(parts) > 2 and parts[2] != "00"
    month: int | None = int(parts[1]) if has_month else None
    day: int | None = int(parts[2]) if has_day else None

    return DateValue(year=year, month=month, day=day, calendar=calendar, precision=precision)


# ---------------------------------------------------------------------------
# Claim helpers
# ---------------------------------------------------------------------------


def _datavalue(claim: dict[str, Any]) -> Any:
    """Extract the datavalue value from a claim dict.

    Returns None for novalue/somevalue snaks or missing data.
    """
    mainsnak = claim.get("mainsnak", {})
    snaktype = mainsnak.get("snaktype", "value")
    if snaktype != "value":
        return None
    dv = mainsnak.get("datavalue")
    if dv is None:
        return None
    return dv.get("value")


def _label(entity: dict[str, Any], lang: str = "en") -> str | None:
    """Return the label for *entity* in *lang*, or None."""
    labels = entity.get("labels", {})
    entry = labels.get(lang)
    if not entry:
        return None
    return str(entry["value"])


def _claim_values(entity: dict[str, Any], pid: str) -> list[Any]:
    """Return non-None datavalues for all claims with property *pid*."""
    claims = entity.get("claims", {}).get(pid, [])
    result = []
    for claim in claims:
        val = _datavalue(claim)
        if val is not None:
            result.append(val)
    return result


def _entity_qid_from_snak(snak: dict[str, Any]) -> str | None:
    """Extract the QID from a wikibase-item snak, or return None."""
    snaktype = snak.get("snaktype", "value")
    if snaktype != "value":
        return None
    dv = snak.get("datavalue", {})
    if dv.get("type") != "wikibase-entityid":
        return None
    return str(dv["value"]["id"])


# ---------------------------------------------------------------------------
# Spouse classification
# ---------------------------------------------------------------------------


def _classify_spouse_claim(claim: dict[str, Any]) -> dict[str, Any]:
    """Extract spouse metadata and is_former flag from a P26 claim.

    Returns a dict with keys: qid, start_date, end_date, end_cause, is_former.
    """
    qualifiers: dict[str, Any] = claim.get("qualifiers", {})

    # Spouse QID
    qid = _entity_qid_from_snak(claim.get("mainsnak", {})) or ""

    # Start date (P580)
    start_date: DateValue | None = None
    for q in qualifiers.get("P580", []):
        dv = q.get("datavalue", {}).get("value")
        if dv:
            start_date = _parse_time(dv)
            break

    # End date (P582)
    end_date: DateValue | None = None
    for q in qualifiers.get("P582", []):
        dv = q.get("datavalue", {}).get("value")
        if dv:
            end_date = _parse_time(dv)
            break

    # End cause (P1534)
    end_cause: str | None = None
    if "P1534" in qualifiers:
        end_cause = "ended"  # label resolved later via WikidataClient.get_labels

    is_former = (end_date is not None) or (end_cause is not None)

    return {
        "qid": qid,
        "start_date": start_date,
        "end_date": end_date,
        "end_cause": end_cause,
        "is_former": is_former,
    }


# ---------------------------------------------------------------------------
# Image filtering
# ---------------------------------------------------------------------------


def _looks_like_chrome(filename: str) -> bool:
    """Return True if *filename* looks like a UI element, icon, flag, or non-image file."""
    import os

    ext = os.path.splitext(filename)[1].lower()
    if ext and ext not in _IMAGE_EXTS:
        return True
    return bool(_CHROME_PATTERNS.match(filename))


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------


class _TagStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts)


def _strip_html(text: str | None) -> str | None:
    """Strip HTML tags and normalise whitespace. Returns None for empty input."""
    if not text:
        return None
    stripper = _TagStripper()
    stripper.feed(text)
    result = " ".join(stripper.get_text().split())
    return result or None


def _filename_from_upload_url(url: str) -> str | None:
    """Extract the Commons filename from a Wikimedia upload URL.

    Handles direct and thumb URL formats, including SVG-rendered-as-PNG thumbs.
    """
    if not url:
        return None
    m = _THUMB_RE.search(url)
    if m:
        name = m.group(1)
        # SVG rendered as PNG: "Flag.svg/300px-Flag.svg.png" → keep "Flag.svg"
        if name.endswith(".png") and ".svg" in name:
            name = name[: name.rfind(".png")]
        return name
    m = _DIRECT_RE.search(url)
    if m:
        return m.group(1)
    return None
