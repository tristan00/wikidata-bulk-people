# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-05-01

### Added
- `extract_person(qid_or_title)` — single-record extraction by QID or Wikipedia title
- `extract_people(out, filter, resume)` — bulk streaming extraction to JSONL
- `iter_people(filter)` — generator API for in-process streaming
- `Person` dataclass with identity, vitals, career, relationships, media, prose, and provenance fields
- `PeopleFilter` DSL for filtering by birth year, occupation, citizenship, and living status
- Keyset-paginated, birth-year-partitioned QID acquisition via WDQS SPARQL
- Resumable runs with atomic state file (temp-file + rename)
- `wikidata-bulk-people` CLI with `people`, `person`, and `version` subcommands
- Per-host rate limiting for all four Wikimedia API endpoints (Wikidata, Wikipedia, Commons, REST)
- Exponential backoff with `Retry-After` support for 429/5xx responses
