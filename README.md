# wikidata-bulk-people

Extract structured records about people from Wikidata and English Wikipedia. Use the Python API or command line to look up one person, stream matching people, or export a resumable dataset to JSONL, CSV, or a database.

Records combine biographical facts, relationships, article introductions, image metadata, and source revision information. The package supports live API requests and published snapshots with API enrichment.

## Installation

Requires **Python 3.10 or newer**. From the repository root, create a virtual environment:

```sh
python -m venv .venv
```

Activate it with `.venv\Scripts\Activate.ps1` in PowerShell or `source .venv/bin/activate` on macOS/Linux, then install:

```sh
python -m pip install .
```

Choose optional dependencies for the features you need:

| Command | Adds |
| --- | --- |
| `python -m pip install ".[db]"` | SQLAlchemy for database exports |
| `python -m pip install ".[bulk]"` | PyArrow for published snapshots |
| `python -m pip install ".[db,bulk]"` | Both database and snapshot support |
| `python -m pip install -e ".[dev]"` | Editable installation and development tools |

The base installation supports live extraction, JSONL, CSV, iterators, and lists. The examples below target the code in this checkout.

## Quick start

Look up a person using a Wikidata QID or an English Wikipedia title:

```python
import json
from dataclasses import asdict

from wikidata_bulk_people import extract_person

person = extract_person("Albert Einstein")
print(person.name)
print(person.occupations)
print(json.dumps(asdict(person), default=str, ensure_ascii=False, indent=2))
```

Export up to 100 matching people to JSONL:

```python
from wikidata_bulk_people import (
    BulkOptions,
    Occupation,
    PeopleFilter,
    extract_people,
)

extract_people(
    "physicists.jsonl",
    filter=PeopleFilter(occupation_qid=Occupation.PHYSICIST),
    options=BulkOptions(limit=100),
    if_exists="fail",
)
```

Live extraction requires internet access. Start with a small limit; omitting `limit` requests all matches and can take a long time.

## Command line

```sh
wikidata-bulk-people person Q937
wikidata-bulk-people person "Albert Einstein" --out einstein.json
wikidata-bulk-people people --out physicists.jsonl --occupation Q169470 --limit 100 --verbose
wikidata-bulk-people people --csv-dir people-csv --born-after 1900 --born-before 1950 --limit 100
wikidata-bulk-people people --db sqlite:///people.db --limit 100
wikidata-bulk-people people --help
wikidata-bulk-people version
```

Database commands require the `[db]` extra. You can also invoke the CLI with `python -m wikidata_bulk_people.cli`.

Bulk commands accept exactly one destination: `--out`, `--csv-dir`, or `--db`. Use `--living` or `--deceased` to filter by recorded death status. `--no-wikipedia-article` removes the default English Wikipedia article requirement; it does not restrict results to people without articles.

## Python API and output formats

| Function | Result | Durable resume |
| --- | --- | --- |
| `extract_person(qid_or_title)` | One `Person`, using live APIs | No |
| `iter_people(...)` | Generator of `Person` objects | No |
| `extract_people_to_memory(...)` | List of `Person` objects | No |
| `extract_people(path, ...)` | JSONL, one nested record per line | Yes |
| `extract_people_to_csv(directory, ...)` | Six normalized CSV files | Yes |
| `extract_people_to_db(connection_string, ...)` | Six data tables plus pipeline state | Yes |

All five bulk functions accept `filter=PeopleFilter(...)` and `options=BulkOptions(...)`. All extraction functions accept a custom `user_agent` string.

```python
from wikidata_bulk_people import (
    BulkOptions,
    extract_people_to_csv,
    extract_people_to_db,
    iter_people,
)

options = BulkOptions(limit=100)

for person in iter_people(options=options):
    print(person.qid, person.name)

extract_people_to_csv("people-csv", options=options)
extract_people_to_db("sqlite:///people.db", options=options)
```

CSV exports contain `people.csv`, `person_aliases.csv`, `person_citizenships.csv`, `person_occupations.csv`, `person_spouses.csv`, and `person_images.csv`. Child rows join to `people.qid` through `person_qid`. Database exports use the same data table names without the `.csv` suffix; `table_prefix` adds a prefix to database tables.

Database connections use SQLAlchemy URLs. SQLite is covered by the test suite; other database backends may need additional drivers. Use `iter_people` to process records incrementally; `extract_people_to_memory` keeps the entire result in memory.

## Filters

Filter fields combine with AND. QID fields accept a raw QID string or the corresponding exported enum.

| `PeopleFilter` field | Meaning |
| --- | --- |
| `born_after`, `born_before` | Inclusive birth-year bounds |
| `occupation_qid` | Occupation QID or `Occupation` member |
| `citizenship_qid` | Citizenship QID or `Country` member |
| `gender_qid` | Sex or gender QID or `Gender` member |
| `religion_qid` | Religion QID or `Religion` member |
| `award_qid` | Award QID or `Award` member |
| `political_ideology_qid` | Political ideology QID or `PoliticalIdeology` member |
| `has_wikipedia_article` | Require an English Wikipedia article; defaults to `True` |
| `living` | `True` for no recorded death, `False` for a recorded death, or `None` for either |
| `year_partition` | Discover by birth-year partitions through live APIs; defaults to `False` |

QID filters match the specified value directly; they do not expand occupation or other subclass hierarchies. A missing death statement is not proof that a person is alive. Discovery requires `ordered=True`, the default; `ordered=False` and CLI `--unordered` are rejected.

## Bulk options and snapshots

| `BulkOptions` field | Default | Purpose |
| --- | --- | --- |
| `limit` | `None` | Maximum number of distinct matches to process |
| `batch_size` | `50` | People per enrichment batch, from 1 to 50 |
| `workers` | `4` | HTML enrichment workers, from 1 to 4 |
| `job_path` | Automatic | SQLite job manifest for persistent exports |
| `cache_path` | Automatic for exports | Reusable live API cache; iterators use an in-memory cache by default |
| `cache_ttl` | `86400` | Live cache lifetime in seconds |
| `public_snapshot` | `None` | Directory for acquiring and reusing published sources |

Live API discovery is the default and uses the Wikidata Query Service. Requests use rate limiting and retries, but server limits and outages can still interrupt a run. Birth-year partitioning does not bypass service limits.

For published sources, install `[bulk]` and pass a snapshot directory with a positive limit:

```python
from pathlib import Path

from wikidata_bulk_people import BulkOptions, extract_people

extract_people(
    "snapshot-people.jsonl",
    options=BulkOptions(
        limit=100_000,
        public_snapshot=Path("sources"),
    ),
    if_exists="fail",
)
```

The equivalent CLI command is:

```sh
wikidata-bulk-people people --out snapshot-people.jsonl --limit 100000 --public-snapshot sources --verbose
```

Snapshot preparation streams a Wikidata dump, downloads the published English Wikipedia article dataset, and enriches missing information through APIs. The article download is not restricted by the person limit, so initial preparation can require substantial disk space and download time even for a small selection.

Reuse a snapshot directory with the **same filter and limit**. Keep output, job, and live-cache paths outside that directory. Source revisions are recorded in `provenance.json`; published data stays frozen independently of the live cache TTL. Snapshot articles may be older than current pages, and capped live and snapshot searches can select different people. `year_partition=True` is only supported for live discovery.

## Resume and existing output

Persistent exports resume by default. Rerun with the same destination, filter, limit, and job path to continue an interrupted job. A completed matching job is a no-op. Retain the job manifest, state files, and output together; changing an output file externally can invalidate recovery.

For `people.jsonl`, the default manifest is `people.jsonl.state.json.job.sqlite3`, alongside the status file `people.jsonl.state.json`. Database jobs default to `.wikidata-bulk-people/jobs/`. Set `BulkOptions(job_path=...)` or CLI `--job` to choose a manifest path.

For a new job, `if_exists` controls existing destinations:

| Policy | Behavior |
| --- | --- |
| `fail` | Refuse an existing destination |
| `append` | Add records to existing output |
| `replace` | Replace the destination's export data |
| `upsert` | Update matching people and insert new people; database only |

The Python JSONL function defaults to `append`; CSV, database, and CLI exports default to `fail`. Appending a new job is separate from resuming an existing job and does not deduplicate existing JSONL or CSV records.

To deliberately rebuild an export, use `resume=False, if_exists="replace"` in Python or `--no-resume --if-exists replace` on the CLI. Legacy state files without a durable manifest cannot be resumed safely; use a new destination or start a new job.

## Record contents

`Person` is a dataclass containing:

- **Identity:** QID, name, description, aliases, Wikipedia title, and URL.
- **Biography:** birth and death dates and places, sex or gender, citizenships, and occupations.
- **Relationships:** spouse QIDs and names, relationship dates, end causes, and former-spouse flags.
- **Media and prose:** image URLs, thumbnails, dimensions, captions, alt text, license and artist metadata, and the article's lead paragraph.
- **Provenance:** `fetched_at`, Wikidata `lastrevid`, `extractor_version`, and `schema_version`.

Dates use `DateValue` to preserve year, optional month/day, calendar, and Wikidata precision. Missing values remain `None` or empty lists. Labels can fall back to QIDs when English names are unavailable. `fetched_at` records assembly time, while `lastrevid` identifies the Wikidata revision.

Images are metadata references, not downloaded image files. Extraction caps images at 20 per person, and article icons may still appear among the results.

## Errors

The public exceptions inherit from `WikiUtilityError`:

| Exception | Meaning |
| --- | --- |
| `NotFoundError` | A requested entity could not be found |
| `ExtractionError` | A record could not be extracted, including a non-human entity |
| `TransportError` | A source request failed after retries |
| `IncompleteRunError` | A persistent job still has failed or unexported records |

An incomplete persistent job remains resumable. Record errors are stored in `work.error` in its SQLite manifest. The CLI returns exit code `1` for handled extraction or configuration errors and `2` for handled transport errors.

## Development

```sh
python -m pip install -e ".[dev]"
python -m pytest
python -m mypy --strict src/wikidata_bulk_people
python -m build
python -m twine check dist/*
```

The default test run excludes live integration tests and blocks unexpected HTTP requests. Run live tests explicitly with `python -m pytest -m integration`.

Source code lives in `src/wikidata_bulk_people/` and tests in `tests/`. See [CHANGELOG.md](CHANGELOG.md) for release notes.

## License

The project code is licensed under the [MIT License](LICENSE). Extracted text and media retain their source licenses and attribution requirements; the project's code license does not license the exported content.
