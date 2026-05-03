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
- `mypy --strict` is the only required static check; ruff has been removed from this project.

## Live-WDQS explore script design rules

These apply when writing `scripts/explore_s*.py` scripts that hit the live Wikidata endpoint:

1. **Each script uses a unique filter** — never reuse the same filter (e.g. Monaco) across more than 2-3 scripts. WDQS throttles per-IP per-filter within a rolling window. If 6 scripts all query Monaco in sequence, every script after the 3rd will get throttled empty results.

2. **Resume test: expected tail is IRI-order, not position-order** — `FILTER(?item > wd:Qxxx)` filters by IRI lexicographic string comparison, NOT by position in the original WDQS result. The correct expected tail is `{q for q in all_qids if q > midpoint_qid}` (Python string `>` comparison), NOT `all_qids[10:]`.

3. **Don't manufacture PASS for a test that returned wrong results** — If a live test returns 0 of 223 expected items, that is not a PASS, even if the state file says `completed=True`. Either fix the test design so it's robust, or mark it SKIP with an honest explanation. Adding a "NOTE: WDQS throttling" and returning PASS is not acceptable.

4. **Reuse data across scripts where possible** — If s07 already ran Monaco to CSV, s10 can read that CSV to get all_qids instead of running a fresh WDQS query.

5. **Prefer the simplest assertion that's still meaningful** — A resume test doesn't need to verify exact QID sets. Verifying "state is completed, no crash, midpoint QID not in resumed results (off-by-one check)" is sufficient as a live test. Exact QID matching belongs in unit tests.
