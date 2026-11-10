# Changelog

## [Unreleased]

- Add batched extraction, durable recovery and reusable source caches.
- Add published-source extraction for every bulk output, preserving Person fields.
- Correct discovery ordering, retries, article revision selection and interrupted exports.
- Validate incompatible options and optional dependencies consistently.
- Test installed libraries in isolated environments and all outputs with 100,000 real records.
- Remove audit artifacts, extra documents, comments, docstrings and automatic bot commits.

## [0.1.0] - 2026-05-01

### Added
- `extract_person(qid_or_title)` - single-record extraction by QID or Wikipedia title
- `extract_people(out, filter, resume)` - bulk streaming extraction to JSONL
- `iter_people(filter)` - generator API for in-process streaming
- `Person` dataclass with identity, vitals, career, relationships, media, prose, and provenance fields
- `PeopleFilter` DSL for filtering by birth year, occupation, citizenship, and living status
- Keyset-paginated, birth-year-partitioned QID acquisition via WDQS SPARQL
- Resumable runs with atomic state file (temp-file + rename)
- `wikidata-bulk-people` CLI with `people`, `person`, and `version` subcommands
- Per-host rate limiting for all four Wikimedia API endpoints (Wikidata, Wikipedia, Commons, REST)
- Exponential backoff with `Retry-After` support for 429/5xx responses
