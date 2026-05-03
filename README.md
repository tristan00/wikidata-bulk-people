# wikidata-bulk-people

Extract structured records from Wikipedia and Wikidata at scale.

[![PyPI](https://img.shields.io/pypi/v/wikidata-bulk-people)](https://pypi.org/project/wikidata-bulk-people/)
[![Python](https://img.shields.io/pypi/pyversions/wikidata-bulk-people)](https://pypi.org/project/wikidata-bulk-people/)
[![CI](https://github.com/tristan00/wikidata-bulk-people/actions/workflows/ci.yml/badge.svg)](https://github.com/tristan00/wikidata-bulk-people/actions)

## Install

```bash
pip install wikidata-bulk-people          # base — JSONL, CSV, and in-memory output
pip install wikidata-bulk-people[db]      # adds SQLAlchemy for database output
```

## Quick start

```python
from wikidata_bulk_people import extract_person, iter_people, PeopleFilter, Occupation

# Single person by QID or Wikipedia article title:
einstein = extract_person("Q937")
print(einstein.name, einstein.date_of_birth)

# Iterate over matching people (streaming, no disk I/O):
for person in iter_people(filter=PeopleFilter(occupation_qid=Occupation.WRITER, born_after=1900)):
    print(person.name, person.date_of_birth)

# For large queries (>500 results), use year_partition to avoid WDQS throttling:
for person in iter_people(filter=PeopleFilter(occupation_qid=Occupation.PHYSICIST, year_partition=True)):
    print(person.name)
```

## Output formats

### JSONL (resumable streaming)

```python
from wikidata_bulk_people import extract_people, PeopleFilter, Occupation

extract_people(
    "writers.jsonl",
    filter=PeopleFilter(occupation_qid=Occupation.WRITER, born_after=1900),
)
```

One JSON object per line. Resumes automatically from a `.state.json` file alongside
the output if interrupted.

### CSV (normalized directory)

```python
from wikidata_bulk_people import extract_people_to_csv, PeopleFilter, Occupation

extract_people_to_csv(
    "writers_csv/",
    filter=PeopleFilter(occupation_qid=Occupation.WRITER, born_after=1900),
)
```

Creates one file per relation inside the target directory:

```
writers_csv/
  people.csv
  person_aliases.csv
  person_citizenships.csv
  person_occupations.csv
  person_spouses.csv
  person_images.csv
```

Use `if_exists` to control behavior when files already exist
(`"fail"` by default, also `"append"` or `"replace"`):

```python
extract_people_to_csv("writers_csv/", filter=PeopleFilter(...), if_exists="append")
```

### Database (SQLAlchemy)

SQLite:

```python
from wikidata_bulk_people import extract_people_to_db, PeopleFilter, Occupation

extract_people_to_db(
    "sqlite:///writers.db",
    filter=PeopleFilter(occupation_qid=Occupation.WRITER, born_after=1900),
)
```

PostgreSQL (requires `sqlalchemy[postgresql]` and a driver such as `psycopg2`):

```python
from wikidata_bulk_people import extract_people_to_db, PeopleFilter, Occupation

extract_people_to_db(
    "postgresql://user:pass@localhost/mydb",
    filter=PeopleFilter(occupation_qid=Occupation.WRITER, born_after=1900),
    table_prefix="wk_",          # tables become wk_people, wk_person_images, …
    if_exists="upsert",          # update existing rows by qid
)
```

Supported `if_exists` values: `"fail"` (default), `"append"`, `"replace"`, `"upsert"`.
Pipeline state is stored in a `pipeline_state` table so interrupted runs resume
automatically.

### In-memory

```python
from wikidata_bulk_people import extract_people_to_memory, PeopleFilter, Occupation

writers = extract_people_to_memory(
    filter=PeopleFilter(occupation_qid=Occupation.WRITER, born_after=1900),
)
print(f"Loaded {len(writers)} writers")
print(writers[0].name, writers[0].date_of_birth)
```

Loads all results into a `list[Person]`. For very large result sets prefer
`iter_people()` to stream records one at a time.

## CLI

```bash
# Single person
wikidata-bulk-people person Q937

# Bulk — JSONL
wikidata-bulk-people people --out writers.jsonl --occupation Q36180 --born-after 1900

# Bulk — CSV
wikidata-bulk-people people --csv-dir writers_csv/ --born-after 1900

# Bulk — SQLite
wikidata-bulk-people people --db "sqlite:///writers.db" --born-after 1900

# Bulk — PostgreSQL with upsert
wikidata-bulk-people people \
  --db "postgresql://user:pass@localhost/mydb" \
  --table-prefix wk_ \
  --if-exists upsert \
  --born-after 1900

# Year-partition mode — avoids WDQS throttling for large queries (>500 results)
wikidata-bulk-people people --out physicists.jsonl --occupation Q169470 --year-partition

wikidata-bulk-people version
```

Filter flags for `people`: `--occupation QID`, `--citizenship QID`, `--gender QID`,
`--religion QID`, `--award QID`, `--political-ideology QID`, `--born-after YEAR`,
`--born-before YEAR`, `--living`, `--deceased`, `--no-wikipedia-article`,
`--year-partition`.

## Data reference

Each `Person` object contains the following fields:

| Field | Type | Description |
|---|---|---|
| `qid` | `str` | Wikidata entity ID (e.g. `"Q937"`) |
| `wikipedia_title` | `str \| None` | English Wikipedia article title |
| `wikipedia_url` | `str \| None` | Full Wikipedia URL |
| `name` | `str \| None` | Primary English label |
| `description` | `str \| None` | Short description from Wikidata |
| `date_of_birth` | `DateValue \| None` | Structured date (year/month/day/calendar/precision) |
| `date_of_death` | `DateValue \| None` | Structured date |
| `place_of_birth` | `str \| None` | Place of birth label |
| `place_of_death` | `str \| None` | Place of death label |
| `sex_or_gender` | `str \| None` | Gender label |
| `lead_paragraph` | `str \| None` | First paragraph of the Wikipedia article |
| `aliases` | `list[str]` | Alternative names |
| `citizenships` | `list[str]` | Country labels |
| `occupations` | `list[str]` | Occupation labels |
| `spouses` | `list[SpouseRecord]` | Structured spouse relationships |
| `images` | `list[ImageRef]` | Images from Wikimedia Commons |

### Sample data — Albert Einstein (Q937)

**`people` table** (one row per person):

| qid | name | description | dob_year | dob_month | dob_day | dod_year | place_of_birth | place_of_death | sex_or_gender |
|---|---|---|---|---|---|---|---|---|---|
| Q937 | Albert Einstein | german-born theoretical physicist (1879–1955) | 1879 | 3 | 14 | 1955 | Ulm | Princeton | male |

**`person_spouses` table** (one row per marriage):

| person_qid | spouse_qid | name | start_year | start_month | start_day | end_year | end_month | end_day | is_former |
|---|---|---|---|---|---|---|---|---|---|
| Q937 | Q76346 | Mileva Marić | 1903 | 1 | 16 | 1919 | 2 | 14 | True |
| Q937 | Q68761 | Elsa Einstein | 1919 | None | None | 1936 | 12 | 20 | True |

**`person_occupations` table** (one row per occupation):

| person_qid | occupation |
|---|---|
| Q937 | theoretical physicist |
| Q937 | philosopher of science |
| Q937 | inventor |
| Q937 | … (14 total) | |

**`person_images` table** (one row per image):

| person_qid | filename | width | height | license | is_lead |
|---|---|---|---|---|---|
| Q937 | Albert Einstein (Nobel).png | 1080 | 1390 | Public domain | False |
| Q937 | … (21 total) | | | | |

## Architecture

The library queries the [Wikidata SPARQL endpoint](https://query.wikidata.org/) using a single
keyset-paginated stream (`FILTER(?item > wd:Qxxx) ORDER BY ?item LIMIT 500`) so interrupted
runs resume from the last cursor. Four HTTP clients handle Wikidata entities, Wikipedia article
extracts, Wikimedia Commons image metadata, and rendered HTML respectively — all with per-host
throttling and exponential-backoff retries.

## Known issues and future work

**WDQS pagination throttling on complex filtered queries** — When multiple filters are combined
(e.g. `occupation_qid` + `has_wikipedia_article=True`), the WDQS endpoint may silently return
an empty result set after the first 500-item page instead of returning a timeout error. The
keyset pagination logic in `QIDStream` is correct; the second page cursor is issued, but
WDQS silently drops the response under load. For queries that are known to span more than 500
results, the practical workaround is to split the request into narrower filters (e.g. narrow
the birth-year range). Birth-year sub-partitioning is available today as the
`year_partition=True` flag on `PeopleFilter` (and `--year-partition` on the CLI),
but has not been validated beyond the first two pages.

**Possible future: remove `ORDER BY` from SPARQL queries** — The keyset cursor
(`FILTER(?item > wd:Qxxx)`) technically only requires consistent ordering within a page,
not across all pages. Removing `ORDER BY` would allow WDQS to use a more efficient query
plan and may significantly reduce per-page latency for large result sets. In practice WDQS
returns results in an order that is neither strictly numeric nor lexicographic, but is likely
stable enough for cursor-based pagination. A future version should experiment with
`ORDER BY`-free queries to measure the trade-off between speed and ordering guarantees.

## License

MIT
