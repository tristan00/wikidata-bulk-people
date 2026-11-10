# wikidata-bulk-people

Extract people from Wikidata and English Wikipedia as Python objects, JSONL, CSV, or database tables, using live APIs or published snapshots.

## Quick start

Look up a person by Wikidata QID or English Wikipedia title:

```python
from wikidata_bulk_people import extract_person

person = extract_person("Albert Einstein")  # or "Q937"
print(person.name, person.date_of_birth, person.occupations)
```

Export up to 100 physicists:

```python
from wikidata_bulk_people import BulkOptions, Occupation, PeopleFilter, extract_people

extract_people(
    "physicists.jsonl",
    filter=PeopleFilter(occupation_qid=Occupation.PHYSICIST),
    options=BulkOptions(limit=100, interactive=True),
    if_exists="fail",
)
```

Omitting `limit` requests all matches.

## Python API

| Function | Result | Resumable |
| --- | --- | --- |
| `extract_person(qid_or_title)` | One `Person`; live APIs only | No |
| `iter_people(...)` | Iterator of `Person` objects | No |
| `extract_people_to_memory(...)` | List of `Person` objects | No |
| `extract_people(path, ...)` | JSONL | Yes |
| `extract_people_to_csv(directory, ...)` | Six CSV files | Yes |
| `extract_people_to_db(connection_string, ...)` | Six data tables plus pipeline state | Yes |

All bulk functions support live APIs and snapshots through `options=BulkOptions(...)`, and selection through `filter=PeopleFilter(...)`. All extraction functions accept `user_agent`.

```python
from wikidata_bulk_people import (
    BulkOptions, extract_people_to_csv, extract_people_to_db, iter_people,
)

options = BulkOptions(limit=100, interactive=True)
for person in iter_people(options=options):
    print(person.qid, person.name)

extract_people_to_csv("people-csv", options=options)
extract_people_to_db("sqlite:///people.db", options=options)
```

CSV and database exports use `people`, `person_aliases`, `person_citizenships`, `person_occupations`, `person_spouses`, and `person_images`. Child rows join through `person_qid` → `people.qid`. CSV filenames end in `.csv`; `table_prefix` prefixes database tables.

Database connections use SQLAlchemy URLs; backends other than SQLite may need a driver. Use `iter_people` when the result may not fit in memory.

## Command line

```sh
wikidata-bulk-people person Q937
wikidata-bulk-people person "Albert Einstein" --out einstein.json
wikidata-bulk-people people --out physicists.jsonl --occupation Q169470 --limit 100 --interactive
wikidata-bulk-people people --csv-dir people-csv --born-after 1900 --born-before 1950 --limit 100 --interactive
wikidata-bulk-people people --db sqlite:///people.db --limit 100 --interactive
wikidata-bulk-people people --help
wikidata-bulk-people version
```

Bulk commands require one destination: `--out`, `--csv-dir`, or `--db`. Add `--verbose` for progress. The CLI is also available as `python -m wikidata_bulk_people.cli`.

## Filters

`PeopleFilter` fields combine with AND. QID fields accept strings or exported enum members.

| Field | Meaning |
| --- | --- |
| `born_after`, `born_before` | Inclusive birth-year bounds |
| `occupation_qid` | QID or `Occupation` |
| `citizenship_qid` | QID or `Country` |
| `gender_qid` | QID or `Gender` |
| `religion_qid` | QID or `Religion` |
| `award_qid` | QID or `Award` |
| `political_ideology_qid` | QID or `PoliticalIdeology` |
| `has_wikipedia_article` | Require an English article; default `True` |
| `living` | `True`: no recorded death; `False`: recorded death; `None`: either |
| `year_partition` | Discover by birth-year partitions; live APIs only, default `False` |

QID filters match directly, without expanding subclasses. A missing death statement does not prove someone is alive. Keep `ordered=True`; unordered discovery is unsupported.

On the CLI, use `--living` or `--deceased` for death status. `--no-wikipedia-article` allows people both with and without English articles.

## Bulk options

| `BulkOptions` field | Default | Purpose |
| --- | --- | --- |
| `limit` | `None` | Maximum distinct people |
| `batch_size` | `50` | People per enrichment batch; 1–50 |
| `workers` | `4` | HTML enrichment workers; 1–4 |
| `interactive` | `False` | Foreground requests when a user is waiting |
| `job_path` | Automatic | Durable job manifest |
| `cache_path` | Automatic for exports | Live API cache; in-memory for iterators |
| `cache_ttl` | `86400` | Live cache lifetime, seconds |
| `public_snapshot` | `None` | Published-source directory |

Bulk calls default to background requests with `maxlag=5`. Set `interactive=True` or CLI `--interactive` for foreground use. Single-person calls default to foreground; use `interactive=False` or `person --background` for background use. Both modes retain rate limiting and retries.

## Snapshots

Install the source `[bulk]` extra and set `public_snapshot` with a positive limit:

```python
from pathlib import Path
from wikidata_bulk_people import BulkOptions, extract_people

extract_people(
    "snapshot-people.jsonl",
    options=BulkOptions(limit=100_000, public_snapshot=Path("sources")),
    if_exists="fail",
)
```

```sh
wikidata-bulk-people people --out snapshot-people.jsonl --limit 100000 --public-snapshot sources --verbose
```

## Resume and existing output

Persistent exports resume by default. Rerun with the same destination, filter, limit, and job path; completed jobs are a no-op. Keep outputs, manifests, and state files together and avoid editing them during a job. Set `BulkOptions(job_path=...)` or CLI `--job` to choose the manifest location.

For a new job, `if_exists` controls existing output:

| Policy | Behavior |
| --- | --- |
| `fail` | Refuse an existing destination |
| `append` | Add records |
| `replace` | Replace exported data |
| `upsert` | Update matching people and insert new ones; database only |

Python JSONL defaults to `append`; CSV, database, and CLI exports default to `fail`. JSONL/CSV append does not deduplicate existing records.

To rebuild, use `resume=False, if_exists="replace"` or CLI `--no-resume --if-exists replace`. Legacy state without a durable manifest requires a new job or destination.

## Records and errors

`Person` contains identity and aliases, birth/death dates and places, gender, citizenships, occupations, spouses, article introduction, image metadata, and source revisions. Convert it to a dictionary with `dataclasses.asdict(person)`.

`DateValue` preserves date precision and calendar. Missing values are `None` or empty lists; unavailable labels fall back to QIDs. `fetched_at` is assembly time; `lastrevid` identifies the Wikidata revision. Images are URLs and metadata, capped at 20 per person.

Public exceptions inherit from `WikiUtilityError`:

| Exception | Meaning |
| --- | --- |
| `NotFoundError` | Entity not found |
| `ExtractionError` | Extraction failed, including non-human entities |
| `TransportError` | Request failed after retries |
| `IncompleteRunError` | Job has failed or unexported records; resume to retry |

Per-record errors are stored in the manifest's `work.error`. CLI exit codes: `1` for handled extraction/configuration errors, `2` for handled transport errors.

## Benchmarks

**Seconds, median of three runs.** Bulk calls extract 10 people with `award_qid="Q38104"` and an English article, using foreground requests, default batch/worker settings, and fresh jobs and live caches. CLI times include process startup. These small workloads include network costs; capped live and snapshot searches may select different people.

| Python API | Live HTTP | Prepared snapshot |
| --- | ---: | ---: |
| `extract_person("Q937")` | 15.215 | Unsupported |
| `extract_person("Albert Einstein")` | 15.210 | Unsupported |
| `iter_people` — fully consumed | 5.973 | 0.254 |
| `extract_people_to_memory` | 5.669 | 0.255 |
| `extract_people` — JSONL | 6.039 | 0.314 |
| `extract_people_to_csv` | 6.216 | 0.328 |
| `extract_people_to_db` — SQLite | 6.434 | 0.640 |

Iterator time to first person: **5.953 s live / 0.250 s prepared**; records are yielded after batch enrichment.

| CLI command | Live HTTP | Prepared snapshot |
| --- | ---: | ---: |
| `person Q937` — stdout | 15.514 | Unsupported |
| `person "Albert Einstein" --out` | 15.392 | Unsupported |
| `people --out` — JSONL | 6.387 | 0.577 |
| `people --csv-dir` | 6.273 | 0.583 |
| `people --db` — SQLite | 6.751 | 0.975 |

**First-use snapshots:** preparation took **1,073.584 s**; the first JSONL export including preparation took **1,073.943 s (~18 min)** for 10 people, with **37.164 GB** of Wikipedia downloads (one run). Prepared timings above reuse those sources and exclude preparation.

<details>
<summary>Resume, output policies, and CLI startup</summary>

Seconds, one run per cell. Completed resume adds no people; interrupted resume finishes 5 of 10, retaining the job's cache. Append, replace, and upsert each process 10 people with a fresh cache. Append uses existing output (10 records for JSONL/CSV, one disjoint person for SQLite); replace and upsert start with 10 people.

| Entry point | Source | Resume (0 new) | Resume (5 new) | Append | Replace | Upsert |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| API JSONL | Live | 0.171 | 3.809 | 6.108 | 5.628 | — |
| API JSONL | Prepared | 0.170 | 0.284 | 0.304 | 0.290 | — |
| API CSV | Live | 0.174 | 3.458 | 6.181 | 5.686 | — |
| API CSV | Prepared | 0.175 | 0.301 | 0.359 | 0.319 | — |
| API SQLite | Live | 0.520 | 3.540 | 7.133 | 6.414 | 5.958 |
| API SQLite | Prepared | 0.474 | 0.550 | 0.597 | 0.654 | 0.596 |
| CLI JSONL | Live | 0.400 | 3.577 | 6.632 | 5.903 | — |
| CLI JSONL | Prepared | 0.421 | 0.526 | 0.536 | 0.602 | — |
| CLI CSV | Live | 0.451 | 3.605 | 5.852 | 6.646 | — |
| CLI CSV | Prepared | 0.413 | 0.545 | 0.565 | 0.568 | — |
| CLI SQLite | Live | 0.769 | 3.740 | 6.498 | 6.870 | 6.340 |
| CLI SQLite | Prepared | 0.761 | 0.935 | 0.895 | 0.978 | 0.888 |

CLI startup, median of three runs: `version` **0.174 s**, `--help` **0.175 s**, `person --help` **0.176 s**, `people --help` **0.177 s**.

</details>

## License

[MIT](LICENSE). Extracted text and media retain their source licenses and attribution requirements.
