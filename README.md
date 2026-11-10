# wikidata-bulk-people

Extract structured people from Wikidata and English Wikipedia. Python 3.10+.

## Install

```bash
git clone --branch speed-reliability-iteration https://github.com/tristan00/wikidata-bulk-people.git
cd wikidata-bulk-people
python -m pip install ".[db,bulk]"
```

Base installation (`pip install .`) supports live APIs and all outputs except databases. `[db]` adds SQLAlchemy; `[bulk]` adds published datasets. This branch's features are not yet released on PyPI.

## Use

```python
from pathlib import Path
from wikidata_bulk_people import BulkOptions, extract_person, extract_people

person = extract_person("Q937")
options = BulkOptions(limit=100_000, public_snapshot=Path("sources"))
extract_people("people.jsonl", options=options, if_exists="fail")
```

| Function | Output | Resume |
|---|---|---|
| `extract_person(qid_or_title)` | One `Person`, live APIs only | No |
| `iter_people(options=options)` | Generator of `Person` | No |
| `extract_people_to_memory(options=options)` | List of `Person` | No |
| `extract_people(path, options=options)` | JSONL | Yes |
| `extract_people_to_csv(directory, options=options)` | Six normalized CSV files | Yes |
| `extract_people_to_db(url, options=options)` | Six SQL tables; SQLite tested | Yes |

All five bulk functions accept the same `filter=PeopleFilter(...)` and `options`. Filters include inclusive birth years, occupation/citizenship/gender/religion/award/political-ideology QIDs and living status. English Wikipedia articles are required by default; `has_wikipedia_article=False` removes that requirement.

Live APIs are the default. Published sources require a positive limit and about 50 GB of disk space for the measured selection. Article revisions can be older than live pages. Reuse a source directory with the same filter and limit; keep outputs outside it. Different backends can select different people for a capped search.

`BulkOptions` also configures `batch_size` (1–50), `workers` (1–4), `cache_path`, `cache_ttl` (seconds) and `job_path`. Jobs apply only to persistent outputs. Snapshot preparation uses separate bounded pools. `year_partition=True` is live-only.

Keep job sidecars to resume. Completed matching jobs are no-ops. Rebuild with `resume=False, if_exists="replace"`. New JSONL jobs default to append; CSV, database and CLI default to fail. `TransportError` reports exhausted source retries; `IncompleteRunError` leaves a persistent job resumable.

## Real-data timings

Measured 9 November 2026. Published sources completed 100,000 people from empty caches in **54m 31s**, including downloads and recovery. Inputs were the 6 November Wikidata dump and 13 May Wikipedia publication, plus live enrichment. Live APIs stopped at 150/100,000 after 41m 28s due to maxlag; a single live lookup also failed after 21.17s.

The following runs reuse the same real sources, make no HTTP requests, and exclude acquisition:

| Method | Windows / Python 3.13 | Linux WSL / Python 3.12 |
|---|---:|---:|
| JSONL, including interruption/resume | 90.20s | 83.74s |
| CSV | 167.86s | 110.71s |
| SQLite | 84.08s | 93.05s |
| Iterator, consumed and verified | 42.01s | 42.51s |
| List, including verification | 44.66s | 53.43s |

Each output verified 100,000 people, 126,484 images and 14,366 spouse records. These are observed runs, not guarantees for arbitrary filters or a controlled OS comparison.

## Output example

Selected fields from an actual result:

```json
{"qid":"Q100063","name":"Ragnhild Haga","date_of_birth":{"year":1991,"month":2,"day":12,"calendar":"gregorian","precision":11},"citizenships":["Norway"],"occupations":["cross-country skier","orienteer"]}
```

`Person` also contains aliases, description, death/birth places, gender, spouses, images, article lead, URLs and provenance. Dates preserve precision; absent values remain null/empty. Names can fall back to QIDs. Images are capped at 20 and may include page icons. `fetched_at` is assembly time; `lastrevid` is the Wikidata revision. Use `dataclasses.asdict(person)` to serialize Python objects.

## CLI and tests

```bash
wikidata-bulk-people people --out people.jsonl --limit 100000 --public-snapshot sources
wikidata-bulk-people people --help
python -m pip install ".[dev]"
python -m pytest
python -m build
python scripts/check_distribution.py
```

The distribution check installs wheels/extras and the source archive into isolated environments and runs the public API checks outside the checkout. Offline tests block unexpected HTTP; live tests require `pytest -m integration`.

Code: MIT. Wikidata facts: CC0. Wikipedia text and Commons media retain their source licenses and attribution.
