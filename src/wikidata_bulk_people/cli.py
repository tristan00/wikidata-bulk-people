"""wikidata-bulk-people command-line interface."""

import argparse
import sys


def main() -> None:
    """Entry point for the wikidata-bulk-people console script."""
    parser = argparse.ArgumentParser(
        prog="wikidata-bulk-people",
        description="Extract structured records from Wikipedia and Wikidata.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # wikidata-bulk-people people
    people_p = sub.add_parser("people", help="Bulk-extract all matching people.")
    out_group = people_p.add_mutually_exclusive_group(required=True)
    out_group.add_argument("--out", metavar="PATH", help="Output JSONL file path.")
    out_group.add_argument("--db", metavar="URL", help="SQLAlchemy connection string.")
    out_group.add_argument("--csv-dir", metavar="PATH", help="Directory for normalized CSV files.")
    people_p.add_argument("--born-after", type=int, metavar="YEAR")
    people_p.add_argument("--born-before", type=int, metavar="YEAR")
    people_p.add_argument("--occupation", metavar="QID", help="Filter by occupation QID.")
    people_p.add_argument("--citizenship", metavar="QID", help="Filter by citizenship QID.")
    people_p.add_argument("--limit", type=int, metavar="N", help="Stop after N records.")
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
    people_p.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        default=True,
        help="Resume from state file (default).",
    )
    people_p.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="Start fresh, ignoring any state file.",
    )

    # wikidata-bulk-people person
    person_p = sub.add_parser("person", help="Extract a single person by QID or Wikipedia title.")
    person_p.add_argument("qid_or_title", metavar="QID_OR_TITLE")
    person_p.add_argument(
        "--out", metavar="PATH", help="Write JSON output to file (default: stdout)."
    )  # noqa: E501

    # wikidata-bulk-people version
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
            person = extract_person(args.qid_or_title)
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
            print(output)
        sys.exit(0)

    if args.command == "people":
        import itertools
        import pathlib

        from wikidata_bulk_people import (
            PeopleFilter,
            extract_people,
            extract_people_to_csv,
            extract_people_to_db,
            iter_people,
        )
        from wikidata_bulk_people._models import TransportError

        f = PeopleFilter(
            born_after=args.born_after,
            born_before=args.born_before,
            occupation_qid=args.occupation,
            citizenship_qid=args.citizenship,
        )

        # Validate --upsert only with --db
        if args.if_exists == "upsert" and not args.db:
            print("Error: --if-exists upsert is only valid with --db.", file=sys.stderr)
            sys.exit(1)

        try:
            if args.db:
                extract_people_to_db(
                    args.db,
                    filter=f,
                    resume=args.resume,
                    if_exists=args.if_exists,
                    table_prefix=args.table_prefix,
                )
            elif args.csv_dir:
                extract_people_to_csv(
                    pathlib.Path(args.csv_dir),
                    filter=f,
                    resume=args.resume,
                    if_exists=args.if_exists,
                )
            elif args.limit is not None:
                # Stream-and-write manually to honour the limit (JSONL only)
                from wikidata_bulk_people._extract import JSONLSink

                out_path = pathlib.Path(args.out)
                with JSONLSink(out_path) as sink:
                    for person in itertools.islice(iter_people(filter=f), args.limit):
                        sink.write(person)
            else:
                extract_people(pathlib.Path(args.out), filter=f, resume=args.resume)
        except (ValueError, FileExistsError) as exc:
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
