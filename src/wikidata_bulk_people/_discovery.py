import dataclasses
import re
import sqlite3
import tempfile
from collections.abc import Generator
from datetime import datetime, timezone
from typing import Any

from wikidata_bulk_people._clients import BaseClient
from wikidata_bulk_people._models import PeopleFilter, TransportError

_PAGE_SIZE = 500
_WDQS = "https://query.wikidata.org/sparql"
_YP_YEAR_MIN = -3000
_YP_YEAR_MAX = max(2030, datetime.now(timezone.utc).year)
_YP_NO_DOB = "NO_DOB"
_NO_KNOWN_DOB = ("FILTER NOT EXISTS { ?item wdt:P569 ?knownDob. "
                 "BIND(YEAR(?knownDob) AS ?knownYear) FILTER(BOUND(?knownYear)) }")


def _validate_filter(filter: PeopleFilter) -> None:
    if not filter.ordered:
        raise ValueError("ordered=False can silently omit matches; exhaustive extraction requires ordering")
    if (filter.born_after is not None and filter.born_before is not None
            and filter.born_after > filter.born_before):
        raise ValueError("born_after must be <= born_before")
    for key, value in dataclasses.asdict(filter).items():
        if key.endswith("_qid") and value is not None and not re.fullmatch(r"Q[1-9][0-9]*", str(value)):
            raise ValueError(f"Invalid {key}: {value!r}")


def _build_constraints(filter: PeopleFilter) -> list[str]:
    _validate_filter(filter)
    constraints = ["?item wdt:P31 wd:Q5."]
    if filter.born_after is not None or filter.born_before is not None:
        constraints.append("?item wdt:P569 ?dob.")
        if filter.born_after is not None:
            constraints.append(f"FILTER(YEAR(?dob) >= {filter.born_after})")
        if filter.born_before is not None:
            constraints.append(f"FILTER(YEAR(?dob) <= {filter.born_before})")
    for name, pid in (("occupation_qid", "P106"), ("citizenship_qid", "P27"),
                      ("gender_qid", "P21"), ("religion_qid", "P140"), ("award_qid", "P166"),
                      ("political_ideology_qid", "P1142")):
        value = getattr(filter, name)
        if value:
            constraints.append(f"?item wdt:{pid} wd:{value}.")
    if filter.has_wikipedia_article:
        constraints.append("?article schema:about ?item; schema:isPartOf <https://en.wikipedia.org/>.")
    if filter.living is True:
        constraints.append("FILTER NOT EXISTS { ?item wdt:P570 [] }")
    elif filter.living is False:
        constraints.append("?item wdt:P570 [].")
    return constraints


def _query(constraints: list[str], last_qid: str | None) -> str:
    if last_qid:
        if not re.fullmatch(r"Q[1-9][0-9]*", last_qid):
            raise ValueError(f"Invalid cursor: {last_qid}")
        constraints = [*constraints, f'FILTER(STR(?item) > "http://www.wikidata.org/entity/{last_qid}")']
    body = "\n  ".join(constraints)
    return f"SELECT DISTINCT ?item WHERE {{\n  {body}\n}}\nORDER BY STR(?item)\nLIMIT {_PAGE_SIZE}"


def _build_query(filter: PeopleFilter, last_qid: str | None) -> str:
    return _query(_build_constraints(filter), last_qid)


def _build_query_no_dob(filter: PeopleFilter, last_qid: str | None) -> str:
    base = dataclasses.replace(filter, born_after=None, born_before=None)
    return _query([*_build_constraints(base), _NO_KNOWN_DOB], last_qid)


def _qid_from_binding(binding: dict[str, Any]) -> str | None:
    value = binding.get("item", {}).get("value", "")
    prefix = "http://www.wikidata.org/entity/"
    if isinstance(value, str) and value.startswith(prefix):
        qid = value[len(prefix):]
        if re.fullmatch(r"Q[1-9][0-9]*", qid):
            return qid
    return None


class _SparqlClient(BaseClient):
    def run_query(self, query: str) -> list[dict[str, Any]]:
        data = self._get(_WDQS, params={"query": query, "format": "json"})
        results = data.get("results")
        bindings = results.get("bindings") if isinstance(results, dict) else None
        if not isinstance(bindings, list) or not all(isinstance(b, dict) for b in bindings):
            raise TransportError(_WDQS, reason="Invalid SPARQL results.bindings; cannot infer exhaustion")
        return bindings


@dataclasses.dataclass
class DiscoveryPage:
    qids: list[str]
    last_qid: str | None
    bucket: int | str | None


class QIDStream:

    def __init__(self, filter: PeopleFilter, user_agent: str) -> None:
        _validate_filter(filter)
        self._filter = filter
        self._sparql = _SparqlClient(user_agent=user_agent)
        self.current_year_bucket: int | str | None = None

    def _buckets(self) -> list[int | str | None]:
        f = self._filter
        if not f.year_partition:
            return [None]
        lo = f.born_after if f.born_after is not None else _YP_YEAR_MIN
        hi = f.born_before if f.born_before is not None else _YP_YEAR_MAX
        buckets: list[int | str | None] = list(range(lo, hi + 1))
        if f.born_after is None:
            buckets.insert(0, "BEFORE")
        if f.born_before is None:
            buckets.append("AFTER")
        if f.born_after is None and f.born_before is None:
            buckets.append(_YP_NO_DOB)
        return buckets

    def iter_pages(self, start_after: str | None = None,
                   start_year: int | str | None = None,
                   snapshot_limit: int | None = None) -> Generator[DiscoveryPage]:


        if snapshot_limit is not None:
            if not 1 <= snapshot_limit <= 100000:
                raise ValueError("snapshot_limit must be between 1 and 100000")
            if start_after is not None or start_year is not None or self._filter.year_partition:
                raise ValueError("Snapshot discovery cannot resume a keyset or partition cursor")
            body = "\n  ".join(_build_constraints(self._filter))
            query = f"SELECT DISTINCT ?item WHERE {{\n  {body}\n}}\nLIMIT {snapshot_limit}"
            bindings = self._sparql.run_query(query)
            snapshot_qids = [_qid_from_binding(binding) for binding in bindings]
            if any(qid is None for qid in snapshot_qids):
                raise TransportError(_WDQS, reason="Invalid QID binding in bounded discovery")
            selected = sorted(qid for qid in snapshot_qids if qid is not None)
            if len(selected) > snapshot_limit or len(set(selected)) != len(selected):
                raise TransportError(_WDQS, reason="Invalid count or duplicate QID in bounded discovery")
            yield DiscoveryPage(selected, selected[-1] if selected else None, None)
            return
        buckets = self._buckets()
        if start_year is not None and start_year not in buckets:
            raise ValueError(f"Invalid saved partition: {start_year!r}")
        start = buckets.index(start_year) if start_year is not None else 0
        for index, bucket in enumerate(buckets[start:]):
            self.current_year_bucket = bucket
            cursor = start_after if index == 0 else None
            while True:
                constraints = _build_constraints(self._filter)
                if bucket == _YP_NO_DOB:
                    constraints.append(_NO_KNOWN_DOB)
                elif bucket is not None:
                    constraints.append("?item wdt:P569 ?partitionDob.")
                    if bucket == "BEFORE":
                        constraints.append(f"FILTER(YEAR(?partitionDob) < {_YP_YEAR_MIN})")
                    elif bucket == "AFTER":
                        constraints.append(f"FILTER(YEAR(?partitionDob) > {_YP_YEAR_MAX})")
                    else:
                        constraints.append(f"FILTER(YEAR(?partitionDob) = {bucket})")
                bindings = self._sparql.run_query(_query(constraints, cursor))
                qids: list[str] = []
                previous = cursor
                for binding in bindings:
                    qid = _qid_from_binding(binding)
                    if qid is None:
                        raise TransportError(_WDQS, reason="Invalid QID binding")
                    if previous is not None and qid < previous:
                        raise TransportError(_WDQS, reason="SPARQL page is not ordered")
                    if cursor is not None and qid <= cursor:
                        raise TransportError(_WDQS, reason="SPARQL cursor did not advance")
                    if not qids or qid != qids[-1]:
                        qids.append(qid)
                    previous = qid
                if qids:
                    cursor = qids[-1]
                yield DiscoveryPage(qids, cursor, bucket)
                if len(bindings) < _PAGE_SIZE:
                    break

    def __iter__(self) -> Generator[str]:
        yield from self.iter_partition()

    def iter_partition(self, start_after: str | None = None,
                       start_year: int | str | None = None) -> Generator[str]:

        with tempfile.TemporaryDirectory(prefix="wbp-discovery-") as directory:
            db = sqlite3.connect(directory + "/seen.sqlite3")
            try:
                db.execute("CREATE TABLE seen(qid TEXT PRIMARY KEY) WITHOUT ROWID")
                for page in self.iter_pages(start_after, start_year):
                    with db:
                        for qid in page.qids:
                            if db.execute("INSERT OR IGNORE INTO seen VALUES(?)", (qid,)).rowcount:
                                yield qid
            finally:
                db.close()

    def close(self) -> None:
        self._sparql.close()
