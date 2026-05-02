"""Output sinks for the people pipeline: CSV, database, and in-memory."""

import csv
import json
import pathlib
from types import TracebackType
from typing import Any, Literal

from wikidata_bulk_people._models import DateValue, Person

# ---------------------------------------------------------------------------
# Normalized schema definition
# ---------------------------------------------------------------------------

_PEOPLE_FIELDS: list[str] = [
    "qid",
    "wikipedia_title",
    "wikipedia_url",
    "name",
    "description",
    "place_of_birth",
    "place_of_death",
    "sex_or_gender",
    "lead_paragraph",
    "fetched_at",
    "lastrevid",
    "extractor_version",
    "schema_version",
    "dob_year",
    "dob_month",
    "dob_day",
    "dob_calendar",
    "dob_precision",
    "dod_year",
    "dod_month",
    "dod_day",
    "dod_calendar",
    "dod_precision",
]

_ALIAS_FIELDS: list[str] = ["person_qid", "alias"]
_CITIZENSHIP_FIELDS: list[str] = ["person_qid", "citizenship"]
_OCCUPATION_FIELDS: list[str] = ["person_qid", "occupation"]

_SPOUSE_FIELDS: list[str] = [
    "person_qid",
    "spouse_qid",
    "name",
    "start_year",
    "start_month",
    "start_day",
    "start_calendar",
    "start_precision",
    "end_year",
    "end_month",
    "end_day",
    "end_calendar",
    "end_precision",
    "end_cause",
    "is_former",
]

_IMAGE_FIELDS: list[str] = [
    "person_qid",
    "filename",
    "url",
    "thumbnail_url",
    "width",
    "height",
    "alt",
    "description",
    "caption",
    "license",
    "artist",
    "is_lead",
]

# Ordered so that "people" (the parent) comes before child tables.
TABLE_FIELDS: dict[str, list[str]] = {
    "people": _PEOPLE_FIELDS,
    "person_aliases": _ALIAS_FIELDS,
    "person_citizenships": _CITIZENSHIP_FIELDS,
    "person_occupations": _OCCUPATION_FIELDS,
    "person_spouses": _SPOUSE_FIELDS,
    "person_images": _IMAGE_FIELDS,
}

# Child tables in the order they must be deleted (FK-safe) before removing a person row.
_CHILD_TABLES = [
    "person_images",
    "person_spouses",
    "person_occupations",
    "person_citizenships",
    "person_aliases",
]

# Name suffix for the internal pipeline-state table (prefixed like data tables).
_STATE_TABLE = "pipeline_state"

# ---------------------------------------------------------------------------
# Row-building helpers
# ---------------------------------------------------------------------------


def _date_cols(prefix: str, d: DateValue | None) -> dict[str, Any]:
    """Return a dict of prefixed column-name → value for a DateValue (or nulls)."""
    if d is None:
        return {
            f"{prefix}year": None,
            f"{prefix}month": None,
            f"{prefix}day": None,
            f"{prefix}calendar": None,
            f"{prefix}precision": None,
        }
    return {
        f"{prefix}year": d.year,
        f"{prefix}month": d.month,
        f"{prefix}day": d.day,
        f"{prefix}calendar": d.calendar,
        f"{prefix}precision": d.precision,
    }


def _person_to_rows(person: Person) -> dict[str, list[dict[str, Any]]]:
    """Expand one Person into normalized rows for each table."""
    qid = person.qid

    people_row: dict[str, Any] = {
        "qid": qid,
        "wikipedia_title": person.wikipedia_title,
        "wikipedia_url": person.wikipedia_url,
        "name": person.name,
        "description": person.description,
        "place_of_birth": person.place_of_birth,
        "place_of_death": person.place_of_death,
        "sex_or_gender": person.sex_or_gender,
        "lead_paragraph": person.lead_paragraph,
        "fetched_at": person.fetched_at.isoformat(),
        "lastrevid": person.lastrevid,
        "extractor_version": person.extractor_version,
        "schema_version": person.schema_version,
        **_date_cols("dob_", person.date_of_birth),
        **_date_cols("dod_", person.date_of_death),
    }

    spouse_rows: list[dict[str, Any]] = [
        {
            "person_qid": qid,
            "spouse_qid": s.qid,
            "name": s.name,
            **_date_cols("start_", s.start_date),
            **_date_cols("end_", s.end_date),
            "end_cause": s.end_cause,
            "is_former": s.is_former,
        }
        for s in person.spouses
    ]

    image_rows: list[dict[str, Any]] = [
        {
            "person_qid": qid,
            "filename": img.filename,
            "url": img.url,
            "thumbnail_url": img.thumbnail_url,
            "width": img.width,
            "height": img.height,
            "alt": img.alt,
            "description": img.description,
            "caption": img.caption,
            "license": img.license,
            "artist": img.artist,
            "is_lead": img.is_lead,
        }
        for img in person.images
    ]

    return {
        "people": [people_row],
        "person_aliases": [{"person_qid": qid, "alias": a} for a in person.aliases],
        "person_citizenships": [{"person_qid": qid, "citizenship": c} for c in person.citizenships],
        "person_occupations": [{"person_qid": qid, "occupation": o} for o in person.occupations],
        "person_spouses": spouse_rows,
        "person_images": image_rows,
    }


# ---------------------------------------------------------------------------
# MemorySink
# ---------------------------------------------------------------------------


class MemorySink:
    """Collects :class:`~wikidata_bulk_people.Person` records in memory.

    For large result sets prefer :func:`~wikidata_bulk_people.iter_people`, which
    streams records without accumulating them.

    Attributes:
        records: Populated after the pipeline completes.
    """

    def __init__(self) -> None:
        self.records: list[Person] = []

    def __enter__(self) -> "MemorySink":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        pass

    def write(self, person: Person) -> None:
        """Append *person* to :attr:`records`."""
        self.records.append(person)


# ---------------------------------------------------------------------------
# CSVSink
# ---------------------------------------------------------------------------


class CSVSink:
    """Writes :class:`~wikidata_bulk_people.Person` records as normalized CSV files.

    One file per table written to *directory*:

    * ``people.csv`` — scalar person fields with inlined date columns
    * ``person_aliases.csv``
    * ``person_citizenships.csv``
    * ``person_occupations.csv``
    * ``person_spouses.csv``
    * ``person_images.csv``

    All files are UTF-8 encoded with LF line endings.
    Pipeline state is saved to ``.pipeline.state.json`` inside *directory*.

    Args:
        directory: Target directory. Created if absent.
        if_exists: Action when CSV files already exist:

            * ``"fail"`` — raise :exc:`FileExistsError` (default)
            * ``"append"`` — add rows; omit header if file is non-empty
            * ``"replace"`` — truncate existing files before writing
    """

    def __init__(
        self,
        directory: pathlib.Path,
        if_exists: Literal["fail", "append", "replace"] = "fail",
    ) -> None:
        self._dir = directory
        self._if_exists = if_exists
        self._writers: dict[str, csv.DictWriter[str]] = {}
        self._open_files: list[Any] = []
        self.state_path: pathlib.Path = directory / ".pipeline.state.json"

    def __enter__(self) -> "CSVSink":
        self._dir.mkdir(parents=True, exist_ok=True)

        if self._if_exists == "fail":
            existing = [
                self._dir / f"{name}.csv"
                for name in TABLE_FIELDS
                if (self._dir / f"{name}.csv").exists()
            ]
            if existing:
                raise FileExistsError(
                    f"CSV files already exist in {self._dir}: "
                    + ", ".join(p.name for p in existing)
                    + ". Use if_exists='append' or 'replace'."
                )

        for table_name, fields in TABLE_FIELDS.items():
            csv_path = self._dir / f"{table_name}.csv"

            if self._if_exists == "replace":
                mode, write_header = "w", True
            elif self._if_exists == "append" and csv_path.exists() and csv_path.stat().st_size > 0:
                mode, write_header = "a", False
            else:
                mode, write_header = "a", True

            f = csv_path.open(mode, newline="", encoding="utf-8")
            self._open_files.append(f)
            writer: csv.DictWriter[str] = csv.DictWriter(f, fieldnames=fields)
            if write_header:
                writer.writeheader()
            self._writers[table_name] = writer

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        for f in self._open_files:
            f.close()
        self._open_files.clear()
        self._writers.clear()

    def write(self, person: Person) -> None:
        """Write all normalized rows for *person* to the appropriate CSV files."""
        for table_name, rows in _person_to_rows(person).items():
            writer = self._writers[table_name]
            for row in rows:
                writer.writerow(row)


# ---------------------------------------------------------------------------
# DatabaseSink
# ---------------------------------------------------------------------------


class DatabaseSink:
    """Writes :class:`~wikidata_bulk_people.Person` records to a relational database.

    Uses SQLAlchemy Core. Any SQLAlchemy-supported database is supported.
    Requires the ``[db]`` extra::

        pip install wikidata-bulk-people[db]

    Creates six normalized tables (optionally prefixed) plus an internal
    ``{prefix}pipeline_state`` table for resumable pipelines:

    * ``{prefix}people``
    * ``{prefix}person_aliases``
    * ``{prefix}person_citizenships``
    * ``{prefix}person_occupations``
    * ``{prefix}person_spouses``
    * ``{prefix}person_images``

    For SQLite, ``PRAGMA foreign_keys = ON`` is emitted automatically.

    Args:
        connection_string: SQLAlchemy URL, e.g. ``"sqlite:///out.db"`` or
            ``"postgresql://user:pass@host/db"``.
        if_exists: Action when target tables already exist:

            * ``"fail"`` — raise :exc:`ValueError` (default)
            * ``"append"`` — insert rows; raise on schema mismatch
            * ``"replace"`` — drop and recreate all tables
            * ``"upsert"`` — delete existing rows per person then re-insert
        table_prefix: Prepended to all table names, e.g. ``"wk_"``.
        batch_size: Persons to buffer before flushing to the database.
    """

    def __init__(
        self,
        connection_string: str,
        if_exists: Literal["fail", "append", "replace", "upsert"] = "fail",
        table_prefix: str = "",
        batch_size: int = 500,
    ) -> None:
        try:
            import sqlalchemy  # noqa: F401
        except ImportError:
            raise ImportError(
                "sqlalchemy is required for DatabaseSink. "
                "Install it with: pip install wikidata-bulk-people[db]"
            ) from None

        self._connection_string = connection_string
        self._if_exists = if_exists
        self._prefix = table_prefix
        self._batch_size = batch_size

        # Populated in __enter__
        self._engine: Any = None
        self._conn: Any = None
        self._meta: Any = None
        self._tables: dict[str, Any] = {}
        self._buffers: dict[str, list[dict[str, Any]]] = {n: [] for n in TABLE_FIELDS}

    def _tname(self, base: str) -> str:
        """Return the prefixed database table name for *base*."""
        return f"{self._prefix}{base}"

    def _state_tname(self) -> str:
        return f"{self._prefix}{_STATE_TABLE}"

    def _build_schema(self) -> Any:
        """Build and return a SQLAlchemy MetaData with all table definitions."""
        import sqlalchemy as sa  # type: ignore[import-untyped]

        meta = sa.MetaData()
        p = self._tname  # shorthand

        sa.Table(
            p("people"),
            meta,
            sa.Column("qid", sa.String, primary_key=True),
            sa.Column("wikipedia_title", sa.String),
            sa.Column("wikipedia_url", sa.String),
            sa.Column("name", sa.String, nullable=False),
            sa.Column("description", sa.String),
            sa.Column("place_of_birth", sa.String),
            sa.Column("place_of_death", sa.String),
            sa.Column("sex_or_gender", sa.String),
            sa.Column("lead_paragraph", sa.Text),
            sa.Column("fetched_at", sa.String),
            sa.Column("lastrevid", sa.BigInteger),
            sa.Column("extractor_version", sa.String),
            sa.Column("schema_version", sa.String),
            sa.Column("dob_year", sa.Integer),
            sa.Column("dob_month", sa.Integer),
            sa.Column("dob_day", sa.Integer),
            sa.Column("dob_calendar", sa.String),
            sa.Column("dob_precision", sa.Integer),
            sa.Column("dod_year", sa.Integer),
            sa.Column("dod_month", sa.Integer),
            sa.Column("dod_day", sa.Integer),
            sa.Column("dod_calendar", sa.String),
            sa.Column("dod_precision", sa.Integer),
        )

        for child_base, value_col in (
            ("person_aliases", "alias"),
            ("person_citizenships", "citizenship"),
            ("person_occupations", "occupation"),
        ):
            sa.Table(
                p(child_base),
                meta,
                sa.Column("person_qid", sa.String, sa.ForeignKey(f"{p('people')}.qid"), nullable=False),
                sa.Column(value_col, sa.String, nullable=False),
            )

        sa.Table(
            p("person_spouses"),
            meta,
            sa.Column("person_qid", sa.String, sa.ForeignKey(f"{p('people')}.qid"), nullable=False),
            sa.Column("spouse_qid", sa.String, nullable=False),
            sa.Column("name", sa.String),
            sa.Column("start_year", sa.Integer),
            sa.Column("start_month", sa.Integer),
            sa.Column("start_day", sa.Integer),
            sa.Column("start_calendar", sa.String),
            sa.Column("start_precision", sa.Integer),
            sa.Column("end_year", sa.Integer),
            sa.Column("end_month", sa.Integer),
            sa.Column("end_day", sa.Integer),
            sa.Column("end_calendar", sa.String),
            sa.Column("end_precision", sa.Integer),
            sa.Column("end_cause", sa.String),
            sa.Column("is_former", sa.Boolean),
        )

        sa.Table(
            p("person_images"),
            meta,
            sa.Column("person_qid", sa.String, sa.ForeignKey(f"{p('people')}.qid"), nullable=False),
            sa.Column("filename", sa.String, nullable=False),
            sa.Column("url", sa.String),
            sa.Column("thumbnail_url", sa.String),
            sa.Column("width", sa.Integer),
            sa.Column("height", sa.Integer),
            sa.Column("alt", sa.String),
            sa.Column("description", sa.Text),
            sa.Column("caption", sa.String),
            sa.Column("license", sa.String),
            sa.Column("artist", sa.String),
            sa.Column("is_lead", sa.Boolean),
        )

        sa.Table(
            self._state_tname(),
            meta,
            sa.Column("key", sa.String, primary_key=True),
            sa.Column("value", sa.Text, nullable=False),
        )

        return meta

    def _check_schema(self, inspector: Any) -> None:
        """Raise ValueError if any existing data table has a mismatched schema."""
        for base_name, fields in TABLE_FIELDS.items():
            tname = self._tname(base_name)
            if not inspector.has_table(tname):
                continue
            actual = {c["name"] for c in inspector.get_columns(tname)}
            expected = set(fields)
            if actual != expected:
                raise ValueError(
                    f"Schema mismatch for table '{tname}'. "
                    f"Missing columns: {sorted(expected - actual)}. "
                    f"Extra columns: {sorted(actual - expected)}."
                )

    def __enter__(self) -> "DatabaseSink":
        import sqlalchemy as sa  # type: ignore[import-untyped]

        self._engine = sa.create_engine(self._connection_string)
        self._meta = self._build_schema()
        inspector = sa.inspect(self._engine)

        if self._if_exists == "fail":
            existing = [self._tname(n) for n in TABLE_FIELDS if inspector.has_table(self._tname(n))]
            if existing:
                raise ValueError(
                    f"Tables already exist: {existing}. "
                    "Use if_exists='append', 'replace', or 'upsert'."
                )
        elif self._if_exists == "replace":
            self._meta.drop_all(self._engine, checkfirst=True)
        elif self._if_exists in ("append", "upsert"):
            self._check_schema(inspector)

        self._meta.create_all(self._engine, checkfirst=True)

        # Populate table references by base name for use in write/flush.
        for base_name in TABLE_FIELDS:
            self._tables[base_name] = self._meta.tables[self._tname(base_name)]
        self._tables["_pipeline_state"] = self._meta.tables[self._state_tname()]

        self._conn = self._engine.connect()
        if self._engine.dialect.name == "sqlite":
            self._conn.execute(sa.text("PRAGMA foreign_keys = ON"))

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        try:
            self._flush()
        finally:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            if self._engine is not None:
                self._engine.dispose()
                self._engine = None

    def write(self, person: Person) -> None:
        """Buffer all normalized rows for *person*; flush to the DB when batch is full."""
        for table_name, rows in _person_to_rows(person).items():
            self._buffers[table_name].extend(rows)
        if len(self._buffers["people"]) >= self._batch_size:
            self._flush()

    def _flush(self) -> None:
        """Write all buffered rows to the database in a single transaction."""
        import sqlalchemy as sa  # type: ignore[import-untyped]

        if not any(self._buffers.values()):
            return

        if self._if_exists == "upsert":
            person_qids = [row["qid"] for row in self._buffers["people"]]
            if person_qids:
                for child in _CHILD_TABLES:
                    t = self._tables[child]
                    self._conn.execute(sa.delete(t).where(t.c.person_qid.in_(person_qids)))
                people_t = self._tables["people"]
                self._conn.execute(sa.delete(people_t).where(people_t.c.qid.in_(person_qids)))

        for table_name, rows in self._buffers.items():
            if rows:
                self._conn.execute(sa.insert(self._tables[table_name]).values(rows))

        self._conn.commit()
        for rows in self._buffers.values():
            rows.clear()

    # --- StatefulSink protocol ---

    def read_pipeline_state(self) -> dict[str, Any]:
        """Read pipeline state from the ``{prefix}pipeline_state`` table."""
        import sqlalchemy as sa  # type: ignore[import-untyped]

        t = self._tables["_pipeline_state"]
        row = self._conn.execute(
            sa.select(t.c.value).where(t.c.key == "state")
        ).scalar()
        if row is None:
            return {}
        result: dict[str, Any] = json.loads(row)
        return result

    def write_pipeline_state(self, state: dict[str, Any]) -> None:
        """Persist pipeline state to the ``{prefix}pipeline_state`` table."""
        import sqlalchemy as sa  # type: ignore[import-untyped]

        t = self._tables["_pipeline_state"]
        self._conn.execute(sa.delete(t).where(t.c.key == "state"))
        self._conn.execute(sa.insert(t).values(key="state", value=json.dumps(state)))
        self._conn.commit()
