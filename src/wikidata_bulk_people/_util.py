import re
from html.parser import HTMLParser
from typing import Any

from wikidata_bulk_people._models import DateValue


_GREGORIAN_QID = "Q1985727"


_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".svg", ".tif", ".tiff", ".webp"})


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


_THUMB_RE = re.compile(
    r"/thumb/[0-9a-f]/[0-9a-f]{2}/(.+?)/\d+px-",
    re.IGNORECASE,
)
_DIRECT_RE = re.compile(
    r"upload\.wikimedia\.org/wikipedia/(?:commons|en)/[0-9a-f]/[0-9a-f]{2}/(.+)$",
    re.IGNORECASE,
)


def _parse_time(value: dict[str, Any] | None) -> DateValue | None:
    if not value:
        return None
    time_str: str = value.get("time", "")
    if not time_str:
        return None
    try:
        precision = int(value.get("precision", 11))
        calendar_model = value.get("calendarmodel", "")
        calendar = "gregorian" if _GREGORIAN_QID in calendar_model else "julian"
        match = re.fullmatch(r"([+-]?)([0-9]+)-([0-9]{2})-([0-9]{2})T.*", time_str)
        if match is None:
            return None
        year = int(match[2]) * (-1 if match[1] == "-" else 1)
        month = int(match[3]) if precision >= 10 and match[3] != "00" else None
        day = int(match[4]) if precision >= 11 and match[4] != "00" else None
        if month is not None and not 1 <= month <= 12:
            return None
        if day is not None and not 1 <= day <= 31:
            return None
        return DateValue(year=year, month=month, day=day, calendar=calendar, precision=precision)
    except (ValueError, TypeError):
        return None


def _datavalue(claim: dict[str, Any]) -> Any:
    mainsnak = claim.get("mainsnak", {})
    snaktype = mainsnak.get("snaktype", "value")
    if snaktype != "value":
        return None
    dv = mainsnak.get("datavalue")
    if dv is None:
        return None
    return dv.get("value")


def _label(entity: dict[str, Any], lang: str = "en") -> str | None:
    labels = entity.get("labels", {})
    entry = labels.get(lang)
    if not entry:
        return None
    return str(entry["value"])


def _valid_claims(entity: dict[str, Any], pid: str) -> list[dict[str, Any]]:
    return [c for c in entity.get("claims", {}).get(pid, []) if c.get("rank") != "deprecated"]


def _best_claims(entity: dict[str, Any], pid: str) -> list[dict[str, Any]]:
    claims = _valid_claims(entity, pid)
    preferred = [c for c in claims if c.get("rank") == "preferred"]
    return preferred or claims


def _claim_values(entity: dict[str, Any], pid: str) -> list[Any]:
    claims = _best_claims(entity, pid)
    result = []
    for claim in claims:
        val = _datavalue(claim)
        if val is not None:
            result.append(val)
    return result


def _entity_qid_from_snak(snak: dict[str, Any]) -> str | None:
    snaktype = snak.get("snaktype", "value")
    if snaktype != "value":
        return None
    dv = snak.get("datavalue", {})
    if dv.get("type") != "wikibase-entityid":
        return None
    return str(dv["value"]["id"])


def _classify_spouse_claim(claim: dict[str, Any]) -> dict[str, Any]:
    qualifiers: dict[str, Any] = claim.get("qualifiers", {})


    qid = _entity_qid_from_snak(claim.get("mainsnak", {})) or ""


    start_date: DateValue | None = None
    for q in qualifiers.get("P580", []):
        dv = q.get("datavalue", {}).get("value")
        if dv:
            start_date = _parse_time(dv)
            break


    end_date: DateValue | None = None
    for q in qualifiers.get("P582", []):
        dv = q.get("datavalue", {}).get("value")
        if dv:
            end_date = _parse_time(dv)
            break


    end_cause: str | None = None
    if "P1534" in qualifiers:
        end_cause = "ended"

    is_former = (end_date is not None) or (end_cause is not None)

    return {
        "qid": qid,
        "start_date": start_date,
        "end_date": end_date,
        "end_cause": end_cause,
        "is_former": is_former,
    }


def _looks_like_chrome(filename: str) -> bool:
    import os

    ext = os.path.splitext(filename)[1].lower()
    if ext and ext not in _IMAGE_EXTS:
        return True


    return bool(_CHROME_PATTERNS.match(filename.replace(" ", "_")))


class _TagStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts)


def _strip_html(text: str | None) -> str | None:
    if not text:
        return None
    stripper = _TagStripper()
    stripper.feed(text)
    result = " ".join(stripper.get_text().split())
    return result or None


def _filename_from_upload_url(url: str) -> str | None:
    if not url:
        return None
    m = _THUMB_RE.search(url)
    if m:
        name = m.group(1)

        if name.endswith(".png") and ".svg" in name:
            name = name[: name.rfind(".png")]
        return name
    m = _DIRECT_RE.search(url)
    if m:
        return m.group(1)
    return None
