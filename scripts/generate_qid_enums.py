import re
import sys
import time
from typing import Any

import requests

_SPARQL_URL = "https://query.wikidata.org/sparql"
_USER_AGENT = "wikidata-bulk-people/generate-qid-enums (https://github.com/example/wikidata-bulk-people)"
_HEADERS = {"User-Agent": _USER_AGENT, "Accept": "application/sparql-results+json"}


_OCCUPATION_QUERY = """
SELECT ?occ ?occLabel ?sitelinks WHERE {
  { ?occ wdt:P31 wd:Q12737077. }
  UNION
  { ?occ wdt:P31 wd:Q28640. }
  OPTIONAL { ?occ wikibase:sitelinks ?sitelinks. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
ORDER BY DESC(?sitelinks)
LIMIT 1000
"""


_COUNTRY_QUERY = """
SELECT ?country ?countryLabel ?sitelinks WHERE {
  { ?country wdt:P31 wd:Q3624078. }
  UNION
  { ?country wdt:P31 wd:Q6256. }
  FILTER NOT EXISTS { ?country wdt:P576 []. }
  OPTIONAL { ?country wikibase:sitelinks ?sitelinks. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
ORDER BY DESC(?sitelinks)
LIMIT 500
"""


_GENDER_QUERY = """
SELECT ?gender ?genderLabel ?sitelinks WHERE {
  ?gender wdt:P31 wd:Q48264.
  OPTIONAL { ?gender wikibase:sitelinks ?sitelinks. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
ORDER BY DESC(?sitelinks)
LIMIT 100
"""


_RELIGION_QUERY = """
SELECT ?rel ?relLabel ?sitelinks WHERE {
  { ?rel wdt:P31 wd:Q9174. }
  UNION
  { ?rel wdt:P31 wd:Q7066. }
  OPTIONAL { ?rel wikibase:sitelinks ?sitelinks. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
ORDER BY DESC(?sitelinks)
LIMIT 500
"""


_AWARD_QUERY = """
SELECT ?award ?awardLabel ?sitelinks WHERE {
  ?award wdt:P31 wd:Q618779.
  OPTIONAL { ?award wikibase:sitelinks ?sitelinks. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
ORDER BY DESC(?sitelinks)
LIMIT 2000
"""


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


def _to_identifier(label: str) -> str | None:

    label = label.replace("\u2019", "").replace("\u2018", "").replace("-", "_")

    label = re.sub(r"[^\w\s]", "", label, flags=re.UNICODE)
    label = re.sub(r"\s+", "_", label.strip())
    label = re.sub(r"_+", "_", label)
    label = label.upper()

    if label and label[0].isdigit():
        label = "N_" + label
    return label or None


def _qid(binding: dict[str, Any], key: str) -> str:
    uri: str = binding[key]["value"]
    return uri.rsplit("/", 1)[-1]


def _label(binding: dict[str, Any], key: str) -> str:
    return binding[key]["value"]


def _render_enum(
    class_name: str,
    rows: list[dict[str, Any]],
    entity_key: str,
    label_key: str,
) -> list[str]:
    lines: list[str] = []
    seen_names: dict[str, str] = {}
    members: list[tuple[str, str, str, int]] = []

    for row in rows:
        qid = _qid(row, entity_key)
        raw_label = _label(row, label_key)
        count = int(row.get("sitelinks", row.get("count", {})).get("value", 0))


        if re.match(r"^Q\d+$", raw_label):
            continue

        name = _to_identifier(raw_label)
        if not name:
            continue


        if name in seen_names:
            continue
        seen_names[name] = qid
        members.append((name, qid, raw_label, count))

    if not members:
        return []

    lines.append(f"class {class_name}(_QidEnum):")
    lines.append("")

    for name, qid, raw_label, count in members:
        lines.append(f"    {name} = {qid!r}")

    return lines


_ENUM_SPECS: list[dict[str, object]] = [
    dict(
        label="occupations",
        query=_OCCUPATION_QUERY,
        class_name="Occupation",
        entity_key="occ",
        label_key="occLabel",
    ),
    dict(
        label="countries",
        query=_COUNTRY_QUERY,
        class_name="Country",
        entity_key="country",
        label_key="countryLabel",
    ),
    dict(
        label="genders",
        query=_GENDER_QUERY,
        class_name="Gender",
        entity_key="gender",
        label_key="genderLabel",
    ),
    dict(
        label="religions",
        query=_RELIGION_QUERY,
        class_name="Religion",
        entity_key="rel",
        label_key="relLabel",
    ),
    dict(
        label="awards",
        query=_AWARD_QUERY,
        class_name="Award",
        entity_key="award",
        label_key="awardLabel",
    ),
    dict(
        label="political ideologies",
        query=_POLITICAL_IDEOLOGY_QUERY,
        class_name="PoliticalIdeology",
        entity_key="ideo",
        label_key="ideoLabel",
    ),
]


def main() -> None:
    all_rows: dict[str, list[dict[str, object]]] = {}
    for spec in _ENUM_SPECS:
        label = str(spec["label"])
        print(f"Querying {label}...", file=sys.stderr)
        rows = _sparql(str(spec["query"]))
        print(f"  {len(rows)} rows", file=sys.stderr)
        all_rows[label] = rows
        time.sleep(1)

    out: list[str] = [
        "from enum import Enum", "", "",
        "class _QidEnum(str, Enum):",
        "    def __str__(self) -> str:",
        "        return str(self.value)", "",
        "    def __format__(self, format_spec: str) -> str:",
        "        return str(self.value).__format__(format_spec)", "", "",
    ]

    for spec in _ENUM_SPECS:
        label = str(spec["label"])
        lines = _render_enum(
            class_name=str(spec["class_name"]),
            rows=all_rows[label],
            entity_key=str(spec["entity_key"]),
            label_key=str(spec["label_key"]),
        )
        out.extend(lines)
        out.append("")
        out.append("")


    while out and out[-1] == "":
        out.pop()
    out.append("")

    print("\n".join(out))


if __name__ == "__main__":
    main()
