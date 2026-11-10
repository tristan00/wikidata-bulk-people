import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="wikidata-bulk-people",
        description="Extract structured records from Wikipedia and Wikidata.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")


    people_p = sub.add_parser("people", help="Bulk-extract all matching people.")
    out_group = people_p.add_mutually_exclusive_group(required=True)
    out_group.add_argument("--out", metavar="PATH", help="Output JSONL file path.")
    out_group.add_argument("--db", metavar="URL", help="SQLAlchemy connection string.")
    out_group.add_argument("--csv-dir", metavar="PATH", help="Directory for normalized CSV files.")
    people_p.add_argument("--born-after", type=int, metavar="YEAR")
    people_p.add_argument("--born-before", type=int, metavar="YEAR")
    people_p.add_argument("--occupation", metavar="QID", help="Filter by occupation QID.")
    people_p.add_argument("--citizenship", metavar="QID", help="Filter by citizenship QID.")
    people_p.add_argument("--gender", metavar="QID", help="Filter by gender QID.")
    people_p.add_argument("--religion", metavar="QID", help="Filter by religion QID.")
    people_p.add_argument("--award", metavar="QID", help="Filter by award QID.")
    people_p.add_argument(
        "--political-ideology", metavar="QID", help="Filter by political ideology QID."
    )
    article_group = people_p.add_mutually_exclusive_group()
    article_group.add_argument(
        "--has-wikipedia-article",
        dest="has_wikipedia_article",
        action="store_true",
        default=True,
        help="Require an English Wikipedia article (default).",
    )
    article_group.add_argument(
        "--no-wikipedia-article",
        dest="has_wikipedia_article",
        action="store_false",
        help="Do not require a Wikipedia article.",
    )
    living_group = people_p.add_mutually_exclusive_group()
    living_group.add_argument(
        "--living",
        dest="living",
        action="store_true",
        default=None,
        help="Only include living people.",
    )
    living_group.add_argument(
        "--deceased",
        dest="living",
        action="store_false",
        help="Only include deceased people.",
    )
    people_p.add_argument("--limit", type=int, metavar="N", help="Process at most N distinct matches.")
    people_p.add_argument("--verbose", action="store_true", help="Log discovery and enrichment progress.")
    people_p.add_argument("--job", type=str, help="Path to durable SQLite job manifest.")
    people_p.add_argument("--cache", type=str, help="Path to reusable live API cache (outside snapshot directory).")
    people_p.add_argument("--public-snapshot", type=str, metavar="DIRECTORY",
                          help="Acquire and use published public snapshots here; requires --limit and [bulk].")
    people_p.add_argument("--cache-ttl", type=float, default=86400, help="Live API cache lifetime in seconds; published sources stay frozen.")
    people_p.add_argument("--batch-size", type=int, default=50, help="People per enrichment batch (1..50).")
    people_p.add_argument("--workers", type=int, default=4, help="Assembly HTML workers (1..4); shared API concurrency is capped at 3.")
    people_p.add_argument("--interactive", action="store_true",
                          help="Foreground request: omit the optional maxlag gate while you wait; retain rate limiting and retries.")
    people_p.add_argument(
        "--if-exists",
        dest="if_exists",
        default="fail",
        choices=["fail", "append", "replace", "upsert"],
        help="Action when output already exists (default: fail). 'upsert' only valid with --db.",
    )
    people_p.add_argument(
        "--table-prefix",
        default="",
        metavar="PREFIX",
        help="Prefix for database table names (only used with --db).",
    )
    resume_group = people_p.add_mutually_exclusive_group()
    resume_group.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        default=True,
        help="Resume the matching durable job (default); a completed job is a no-op.",
    )
    resume_group.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="Start a new job using --if-exists to handle existing output.",
    )
    people_p.add_argument(
        "--year-partition",
        dest="year_partition",
        action="store_true",
        default=False,
        help=(
            "Live APIs only: discover in birth-year partitions; does not bypass WDQS limits."
        ),
    )
    people_p.add_argument(
        "--unordered",
        dest="ordered",
        action="store_false",
        default=True,
        help=(
            "Deprecated: rejected because unordered cursors can silently lose matches."
        ),
    )


    person_p = sub.add_parser("person", help="Extract a single person by QID or Wikipedia title.")
    person_p.add_argument("qid_or_title", metavar="QID_OR_TITLE")
    person_p.add_argument("--background", action="store_true",
                          help="Use maxlag=5 for unattended processing (single lookups default to foreground reads).")
    person_p.add_argument(
        "--out", metavar="PATH", help="Write JSON output to file (default: stdout)."
    )


    sub.add_parser("version", help="Print version and exit.")

    args = parser.parse_args()

    if args.command == "version":
        from wikidata_bulk_people import __version__

        print(__version__)
        sys.exit(0)

    if args.command == "person":
        import json
        import pathlib

        from wikidata_bulk_people import extract_person
        from wikidata_bulk_people._models import ExtractionError, NotFoundError, TransportError

        try:
            person = extract_person(args.qid_or_title, interactive=not args.background)
        except NotFoundError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        except ExtractionError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        except TransportError as exc:
            print(f"Transport error: {exc}", file=sys.stderr)
            sys.exit(2)

        import dataclasses

        output = json.dumps(dataclasses.asdict(person), default=str, ensure_ascii=False, indent=2)
        if args.out:
            pathlib.Path(args.out).write_text(output, encoding="utf-8")
        else:
            reconfigure = getattr(sys.stdout, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8")
            print(output)
        sys.exit(0)

    if args.command == "people":
        import pathlib

        from wikidata_bulk_people import (
            BulkOptions,
            IncompleteRunError,
            PeopleFilter,
            extract_people,
            extract_people_to_csv,
            extract_people_to_db,
        )
        from wikidata_bulk_people._models import TransportError

        if args.verbose:
            import logging
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

        f = PeopleFilter(
            born_after=args.born_after,
            born_before=args.born_before,
            occupation_qid=args.occupation,
            citizenship_qid=args.citizenship,
            gender_qid=args.gender,
            religion_qid=args.religion,
            award_qid=args.award,
            political_ideology_qid=args.political_ideology,
            has_wikipedia_article=args.has_wikipedia_article,
            living=args.living,
            year_partition=args.year_partition,
            ordered=args.ordered,
        )


        if args.if_exists == "upsert" and not args.db:
            print("Error: --if-exists upsert is only valid with --db.", file=sys.stderr)
            sys.exit(1)

        if args.table_prefix and not args.db:
            print("Error: --table-prefix is only valid with --db.", file=sys.stderr)
            sys.exit(1)

        try:
            options = BulkOptions(
                limit=args.limit, batch_size=args.batch_size, workers=args.workers,
                job_path=pathlib.Path(args.job) if args.job else None,
                cache_path=pathlib.Path(args.cache) if args.cache else None, cache_ttl=args.cache_ttl,
                public_snapshot=pathlib.Path(args.public_snapshot) if args.public_snapshot else None,
                interactive=args.interactive,
            )
            if args.db:
                extract_people_to_db(
                    args.db,
                    filter=f,
                    resume=args.resume,
                    options=options,
                    if_exists=args.if_exists,
                    table_prefix=args.table_prefix,
                )
            elif args.csv_dir:
                extract_people_to_csv(
                    pathlib.Path(args.csv_dir),
                    filter=f,
                    resume=args.resume,
                    options=options,
                    if_exists=args.if_exists,
                )
            else:
                extract_people(pathlib.Path(args.out), filter=f, resume=args.resume,
                               options=options, if_exists=args.if_exists)
        except (ValueError, FileExistsError, ImportError, IncompleteRunError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        except TransportError as exc:
            print(f"Transport error: {exc}", file=sys.stderr)
            sys.exit(2)
        sys.exit(0)

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
