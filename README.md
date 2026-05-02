# wikidata-bulk-people

Extract structured records from Wikipedia and Wikidata at scale.

[![PyPI](https://img.shields.io/pypi/v/wikidata-bulk-people)](https://pypi.org/project/wikidata-bulk-people/)
[![Python](https://img.shields.io/pypi/pyversions/wikidata-bulk-people)](https://pypi.org/project/wikidata-bulk-people/)
[![CI](https://github.com/example/wikidata-bulk-people/actions/workflows/ci.yml/badge.svg)](https://github.com/example/wikidata-bulk-people/actions)

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
wikidata-bulk-people people --out writers.jsonl --born-after 1900

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

wikidata-bulk-people version

```

## Architecture

See [`docs/architecture.md`](docs/architecture.md) for a full overview.

## License

MIT
