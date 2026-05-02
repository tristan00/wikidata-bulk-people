# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run all tests
pytest

# Run a single test
pytest tests/unit/test_claim_parsing.py::test_function_name

# Run tests with coverage (must hit ≥90%)
pytest --cov=wikidata_bulk_people --cov-report=xml

# Lint and format
ruff check src/ tests/
ruff format src/ tests/

# Type check
mypy --strict src/wikidata_bulk_people

# Regenerate enums.py from live Wikidata (takes minutes, hits SPARQL)
python scripts/generate_qid_enums.py > src/wikidata_bulk_people/enums.py
```

## Architecture

The library extracts structured `Person` records from Wikidata + Wikipedia. There are three public entry points in `__init__.py`: `extract_person()` (single record), `iter_people()` (streaming generator), and `extract_people()` (bulk JSONL pipeline).

### Module responsibilities

- **`_models.py`** — All public dataclasses (`Person`, `DateValue`, `ImageRef`, `SpouseRecord`, `PeopleFilter`) and exceptions (`WikiUtilityError`, `NotFoundError`, `TransportError`, `ExtractionError`). No I/O here.

- **`_clients.py`** — Four HTTP clients, all inheriting `BaseClient`:
  - `BaseClient`: per-host throttling (`_HostThrottle`) + exponential backoff retry (5 attempts)
  - `WikidataClient`: `wbgetentities` API for entity/label fetches
  - `WikipediaClient`: article extracts and image lists
  - `CommonsClient`: image metadata (URL, dimensions, license, attribution)
  - `RestClient`: fetches rendered HTML to extract image `alt` attributes

- **`_extract.py`** — Core extraction and pipeline:
  - `PersonExtractor`: orchestrates all four clients to assemble a `Person` record
  - `QIDStream`: issues keyset-paginated SPARQL queries against WDQS, partitioned by birth year (range -3000–2030, step 1, plus a no-DOB bucket). Uses keyset cursors (`FILTER(?item > wd:Qxxx)`) instead of OFFSET to stay resumable.
  - `run_people_pipeline` / `iter_people_pipeline`: bulk pipeline; the file variant manages a `StateFile` alongside the JSONL output
  - `StateFile`: atomic JSON state (temp+rename) tracking `completed_partitions` and `in_progress` cursor
  - `JSONLSink`: append-mode JSONL writer, flushes after every record

- **`_util.py`** — Pure parsing helpers (no I/O): `_parse_time`, `_claim_values`, `_classify_spouse_claim`, `_looks_like_chrome`, `_strip_html`

- **`enums.py`** — Auto-generated; do not edit manually. Contains `Occupation`, `Country`, `Gender`, `Religion`, `Award`, `PoliticalIdeology` as `str`-based enums keyed by Wikidata QID. Regenerate with the script above.

- **`cli.py`** — `argparse`-based CLI; subcommands are `person`, `people`, `version`.

### Key design rules

- All private modules are prefixed with `_`; only names in `__all__` are public API.
- Birth-year partitioning avoids SPARQL timeouts on the full ~10M-person dataset.
- `PeopleFilter` fields accept either enum values or raw QID strings.
- `mypy --strict` must pass; the `[tool.mypy]` `python_version = "3.10"` target means no `3.11+`-only syntax.
- `ruff` enforces `line-length = 100` and rules `E, F, I, UP, B, SIM`.
