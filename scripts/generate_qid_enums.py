#!/usr/bin/env python3
"""Generate src/wikidata_bulk_people/enums.py from live Wikidata SPARQL queries.

Usage:
    python scripts/generate_qid_enums.py > src/wikidata_bulk_people/enums.py

Queries the Wikidata Query Service for:
  - Occupations (P106) held by humans
  - Citizenships / countries (P27)
  - Gender identities (P21)
  - Religions (P140)
  - Awards (P166)
  - Political ideologies (P1142)

Produces Python source for str-based Enum subclasses, one per concept.
"""

import re
import sys
import time
from typing import Any

import requests

_SPARQL_URL = "https://query.wikidata.org/sparql"
_USER_AGENT = "wikidata-bulk-people/generate-qid-enums (https://github.com/example/wikidata-bulk-people)"
_HEADERS = {"User-Agent": _USER_AGENT, "Accept": "application/sparql-results+json"}

# Items that are classified as an occupation or profession in Wikidata.
# This is an indexed entity-class lookup and runs in <5s on WDQS.
_OCCUPATION_QUERY = """
SELECT ?occ ?occLabel ?sitelinks WHERE {
  { ?occ wdt:P31 wd:Q12737077. }  # instance of: occupation
  UNION
  { ?occ wdt:P31 wd:Q28640. }     # instance of: profession
  OPTIONAL { ?occ wikibase:sitelinks ?sitelinks. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
ORDER BY DESC(?sitelinks)
LIMIT 1000
"""

# Sovereign states and current countries.
_COUNTRY_QUERY = """
SELECT ?country ?countryLabel ?sitelinks WHERE {
  { ?country wdt:P31 wd:Q3624078. }  # sovereign state
  UNION
  { ?country wdt:P31 wd:Q6256. }     # country
  FILTER NOT EXISTS { ?country wdt:P576 []. }   # exclude dissolved states
  OPTIONAL { ?country wikibase:sitelinks ?sitelinks. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
ORDER BY DESC(?sitelinks)
LIMIT 500
"""

# Gender identities used as values for P21 (sex or gender).
_GENDER_QUERY = """
SELECT ?gender ?genderLabel ?sitelinks WHERE {
  ?gender wdt:P31 wd:Q48264.  # instance of: gender identity
  OPTIONAL { ?gender wikibase:sitelinks ?sitelinks. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
ORDER BY DESC(?sitelinks)
LIMIT 100
"""

# Religions and religious denominations used as P140 (religion) values.
_RELIGION_QUERY = """
SELECT ?rel ?relLabel ?sitelinks WHERE {
  { ?rel wdt:P31 wd:Q9174. }   # instance of: religion
  UNION
  { ?rel wdt:P31 wd:Q7066. }   # instance of: religious denomination
  OPTIONAL { ?rel wikibase:sitelinks ?sitelinks. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
ORDER BY DESC(?sitelinks)
LIMIT 500
"""

# Awards used as P166 (award received) values, ordered by prominence.
_AWARD_QUERY = """
SELECT ?award ?awardLabel ?sitelinks WHERE {
  ?award wdt:P31 wd:Q618779.  # instance of: award
  OPTIONAL { ?award wikibase:sitelinks ?sitelinks. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
ORDER BY DESC(?sitelinks)
LIMIT 2000
"""

# Political ideologies: query actual P1142 values used in Wikidata.
# DISTINCT over P1142 is fast (far fewer statements than P106/P27).
_POLITICAL_IDEOLOGY_QUERY = """
SELECT DISTINCT ?ideo ?ideoLabel ?sitelinks WHERE {
  ?person wdt:P1142 ?ideo.
  OPTIONAL { ?ideo wikibase:sitelinks ?sitelinks. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
ORDER BY DESC(?sitelinks)
LIMIT 500
"""


def _sparql(query: str) -> list[dict[str, Any]]:
    resp = requests.get(
        _SPARQL_URL,
        params={"query": query, "format": "json"},
        headers=_HEADERS,
        timeout=90,
    )
    resp.raise_for_status()
    return list(resp.json()["results"]["bindings"])


def _to_identifier(label: str) -> str:
    """Convert a Wikidata label to a valid Python UPPER_CASE identifier."""
    # Normalise unicode dashes/apostrophes to ASCII
    label = label.replace("\u2019", "").replace("\u2018", "").replace("-", "_")
    # Keep only alphanumeric + spaces (→ underscores)
    label = re.sub(r"[^\w\s]", "", label, flags=re.UNICODE)
    label = re.sub(r"\s+", "_", label.strip())
    label = re.sub(r"_+", "_", label)
    label = label.upper()
    # Identifiers can't start with a digit
    if label and label[0].isdigit():
        label = "N_" + label
    return label or None  # type: ignore[return-value]


def _qid(binding: dict[str, Any], key: str) -> str:
    uri: str = binding[key]["value"]
    return uri.rsplit("/", 1)[-1]


def _label(binding: dict[str, Any], key: str) -> str:
    return binding[key]["value"]


def _render_enum(
    class_name: str,
    docstring: str,
    rows: list[dict[str, Any]],
    entity_key: str,
    label_key: str,
) -> list[str]:
    lines: list[str] = []
    seen_names: dict[str, str] = {}  # identifier → qid (dedup)
    members: list[tuple[str, str, str]] = []  # (name, qid, original_label)

    for row in rows:
        qid = _qid(row, entity_key)
        raw_label = _label(row, label_key)
        count = int(row.get("sitelinks", row.get("count", {})).get("value", 0))

        # Skip auto-generated fallback labels like "Q12345"
        if re.match(r"^Q\d+$", raw_label):
            continue

        name = _to_identifier(raw_label)
        if not name:
            continue

        # First occurrence wins (highest count due to ORDER BY DESC)
        if name in seen_names:
            continue
        seen_names[name] = qid
        members.append((name, qid, raw_label, count))

    if not members:
        return []

    lines.append(f"class {class_name}(_QidEnum):")
    lines.append(f'    """{docstring}"""')
    lines.append("")

    for name, qid, raw_label, count in members:
        # Inline comment: original label + count
        base = f"    {name} = {qid!r}"
        comment = f"  # {raw_label} ({count:,})"
        line = base + comment if len(base) + len(comment) <= 100 else base
        lines.append(line)

    return lines


_ENUM_SPECS: list[dict[str, object]] = [
    dict(
        label="occupations",
        query=_OCCUPATION_QUERY,
        class_name="Occupation",
        entity_key="occ",
        label_key="occLabel",
        docstring=(
            "Wikidata QIDs for human occupations, ordered by prominence.\n\n"
            "    Use with PeopleFilter::\n\n"
            "        PeopleFilter(occupation_qid=Occupation.PHYSICIST)"
        ),
    ),
    dict(
        label="countries",
        query=_COUNTRY_QUERY,
        class_name="Country",
        entity_key="country",
        label_key="countryLabel",
        docstring=(
            "Wikidata QIDs for countries/territories, ordered by prominence.\n\n"
            "    Use with PeopleFilter::\n\n"
            "        PeopleFilter(citizenship_qid=Country.UNITED_STATES)"
        ),
    ),
    dict(
        label="genders",
        query=_GENDER_QUERY,
        class_name="Gender",
        entity_key="gender",
        label_key="genderLabel",
        docstring=(
            "Wikidata QIDs for gender identities (P21).\n\n"
            "    Use with PeopleFilter::\n\n"
            "        PeopleFilter(gender_qid=Gender.FEMALE)"
        ),
    ),
    dict(
        label="religions",
        query=_RELIGION_QUERY,
        class_name="Religion",
        entity_key="rel",
        label_key="relLabel",
        docstring=(
            "Wikidata QIDs for religions and denominations (P140).\n\n"
            "    Use with PeopleFilter::\n\n"
            "        PeopleFilter(religion_qid=Religion.ISLAM)"
        ),
    ),
    dict(
        label="awards",
        query=_AWARD_QUERY,
        class_name="Award",
        entity_key="award",
        label_key="awardLabel",
        docstring=(
            "Wikidata QIDs for awards (P166), ordered by prominence.\n\n"
            "    Use with PeopleFilter::\n\n"
            "        PeopleFilter(award_qid=Award.NOBEL_PRIZE_IN_PHYSICS)"
        ),
    ),
    dict(
        label="political ideologies",
        query=_POLITICAL_IDEOLOGY_QUERY,
        class_name="PoliticalIdeology",
        entity_key="ideo",
        label_key="ideoLabel",
        docstring=(
            "Wikidata QIDs for political ideologies (P1142).\n\n"
            "    Use with PeopleFilter::\n\n"
            "        PeopleFilter(political_ideology_qid=PoliticalIdeology.SOCIALISM)"
        ),
    ),
]


def main() -> None:
    all_rows: dict[str, list[dict[str, object]]] = {}
    for spec in _ENUM_SPECS:
        label = str(spec["label"])
        print(f"# Querying {label}...", file=sys.stderr)
        rows = _sparql(str(spec["query"]))
        print(f"  {len(rows)} rows", file=sys.stderr)
        all_rows[label] = rows
        time.sleep(1)

    out: list[str] = [
        '"""Wikidata QID enums for use with PeopleFilter.',
        "",
        "Generated by scripts/generate_qid_enums.py — do not edit by hand.",
        "To refresh: python scripts/generate_qid_enums.py > src/wikidata_bulk_people/enums.py",
        '"""',
        "",
        "from enum import Enum",
        "",
        "",
        "class _QidEnum(str, Enum):",
        '    """Base for QID enums: str value is the QID, usable in f-strings."""',
        "",
        "    def __str__(self) -> str:",
        "        return self.value",
        "",
        "    def __format__(self, format_spec: str) -> str:",
        "        return self.value.__format__(format_spec)",
        "",
        "",
    ]

    for spec in _ENUM_SPECS:
        label = str(spec["label"])
        lines = _render_enum(
            class_name=str(spec["class_name"]),
            docstring=str(spec["docstring"]),
            rows=all_rows[label],
            entity_key=str(spec["entity_key"]),
            label_key=str(spec["label_key"]),
        )
        out.extend(lines)
        out.append("")
        out.append("")

    # Trim trailing blank lines to one
    while out and out[-1] == "":
        out.pop()
    out.append("")

    print("\n".join(out))


if __name__ == "__main__":
    main()
