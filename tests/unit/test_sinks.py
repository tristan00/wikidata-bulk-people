"""Unit tests for CSVSink, DatabaseSink, and MemorySink in _sinks.py."""

import csv
import pathlib
from datetime import datetime, timezone

import pytest

from wikidata_bulk_people._models import DateValue, ImageRef, Person, SpouseRecord
from wikidata_bulk_people._sinks import TABLE_FIELDS, CSVSink, DatabaseSink, MemorySink

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _make_person(
    qid: str = "Q1",
    name: str = "Alice",
    *,
    with_dates: bool = True,
    with_spouse: bool = False,
    with_image: bool = False,
    with_aliases: bool = False,
) -> Person:
    dob = DateValue(year=1990, month=6, day=15, calendar="gregorian", precision=11) if with_dates else None
    dod = DateValue(year=2050, month=1, day=1, calendar="gregorian", precision=9) if with_dates else None
    spouses = []
    if with_spouse:
        spouses = [
            SpouseRecord(
                qid="Q99",
                name="Bob",
                start_date=DateValue(year=2010, month=None, day=None, calendar="gregorian", precision=9),
                end_date=None,
                end_cause=None,
                is_former=False,
            )
        ]
    images = []
    if with_image:
        images = [
            ImageRef(
                filename="Alice.jpg",
                url="https://upload.wikimedia.org/Alice.jpg",
                thumbnail_url="https://upload.wikimedia.org/Alice_thumb.jpg",
                width=800,
                height=600,
                alt="Portrait of Alice",
                description="A photo",
                caption=None,
                license="CC-BY-SA-4.0",
                artist="Photographer",
                is_lead=True,
            )
        ]
    aliases = ["Alicia", "Al"] if with_aliases else []
    return Person(
        qid=qid,
        wikipedia_title=f"{name}_Wikipedia",
        wikipedia_url=f"https://en.wikipedia.org/wiki/{name}",
        name=name,
        description=f"A person named {name}",
        aliases=aliases,
        date_of_birth=dob,
        date_of_death=dod,
        place_of_birth="Springfield",
        place_of_death=None,
        sex_or_gender="female",
        citizenships=["United States"],
        occupations=["writer", "teacher"],
        spouses=spouses,
        images=images,
        lead_paragraph="Alice was a notable person.",
        fetched_at=_NOW,
        lastrevid=12345,
    )


# ---------------------------------------------------------------------------
# MemorySink
# ---------------------------------------------------------------------------


def test_memory_sink_collects_records() -> None:
    p1 = _make_person("Q1", "Alice")
    p2 = _make_person("Q2", "Bob")
    sink = MemorySink()
    with sink:
        sink.write(p1)
        sink.write(p2)
    assert sink.records == [p1, p2]


def test_memory_sink_starts_empty() -> None:
    sink = MemorySink()
    assert sink.records == []
    with sink:
        pass
    assert sink.records == []


# ---------------------------------------------------------------------------
# CSVSink
# ---------------------------------------------------------------------------


def test_csv_sink_creates_all_files(tmp_path: pathlib.Path) -> None:
    person = _make_person(with_spouse=True, with_image=True, with_aliases=True)
    with CSVSink(tmp_path) as sink:
        sink.write(person)

    for table_name in TABLE_FIELDS:
        assert (tmp_path / f"{table_name}.csv").exists()


def test_csv_sink_people_row(tmp_path: pathlib.Path) -> None:
    person = _make_person(with_dates=True)
    with CSVSink(tmp_path) as sink:
        sink.write(person)

    rows = list(csv.DictReader((tmp_path / "people.csv").open(encoding="utf-8")))
    assert len(rows) == 1
    r = rows[0]
    assert r["qid"] == "Q1"
    assert r["name"] == "Alice"
    assert r["dob_year"] == "1990"
    assert r["dob_month"] == "6"
    assert r["dob_calendar"] == "gregorian"
    assert r["dod_year"] == "2050"


def test_csv_sink_null_dates(tmp_path: pathlib.Path) -> None:
    person = _make_person(with_dates=False)
    with CSVSink(tmp_path) as sink:
        sink.write(person)

    rows = list(csv.DictReader((tmp_path / "people.csv").open(encoding="utf-8")))
    assert rows[0]["dob_year"] == ""   # None serialized as empty string in CSV


def test_csv_sink_alias_rows(tmp_path: pathlib.Path) -> None:
    person = _make_person(with_aliases=True)
    with CSVSink(tmp_path) as sink:
        sink.write(person)

    rows = list(csv.DictReader((tmp_path / "person_aliases.csv").open(encoding="utf-8")))
    assert {r["alias"] for r in rows} == {"Alicia", "Al"}
    assert all(r["person_qid"] == "Q1" for r in rows)


def test_csv_sink_spouse_rows(tmp_path: pathlib.Path) -> None:
    person = _make_person(with_spouse=True)
    with CSVSink(tmp_path) as sink:
        sink.write(person)

    rows = list(csv.DictReader((tmp_path / "person_spouses.csv").open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["spouse_qid"] == "Q99"
    assert rows[0]["start_year"] == "2010"
    assert rows[0]["is_former"] == "False"


def test_csv_sink_image_rows(tmp_path: pathlib.Path) -> None:
    person = _make_person(with_image=True)
    with CSVSink(tmp_path) as sink:
        sink.write(person)

    rows = list(csv.DictReader((tmp_path / "person_images.csv").open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["filename"] == "Alice.jpg"
    assert rows[0]["is_lead"] == "True"


def test_csv_sink_if_exists_fail_raises(tmp_path: pathlib.Path) -> None:
    (tmp_path / "people.csv").write_text("existing\n", encoding="utf-8")
    with pytest.raises(FileExistsError, match="people.csv"):
        with CSVSink(tmp_path, if_exists="fail"):
            pass


def test_csv_sink_if_exists_replace(tmp_path: pathlib.Path) -> None:
    p1 = _make_person("Q1", "Alice")
    p2 = _make_person("Q2", "Bob")

    with CSVSink(tmp_path, if_exists="fail") as sink:
        sink.write(p1)

    with CSVSink(tmp_path, if_exists="replace") as sink:
        sink.write(p2)

    rows = list(csv.DictReader((tmp_path / "people.csv").open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["qid"] == "Q2"


def test_csv_sink_if_exists_append(tmp_path: pathlib.Path) -> None:
    p1 = _make_person("Q1", "Alice")
    p2 = _make_person("Q2", "Bob")

    with CSVSink(tmp_path, if_exists="fail") as sink:
        sink.write(p1)

    with CSVSink(tmp_path, if_exists="append") as sink:
        sink.write(p2)

    rows = list(csv.DictReader((tmp_path / "people.csv").open(encoding="utf-8")))
    assert len(rows) == 2
    assert {r["qid"] for r in rows} == {"Q1", "Q2"}


def test_csv_sink_state_path(tmp_path: pathlib.Path) -> None:
    sink = CSVSink(tmp_path)
    assert sink.state_path == tmp_path / ".pipeline.state.json"


# ---------------------------------------------------------------------------
# DatabaseSink
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_url(tmp_path: pathlib.Path) -> str:
    return f"sqlite:///{tmp_path / 'test.db'}"


def test_db_sink_creates_tables(db_url: str) -> None:
    import sqlalchemy as sa

    person = _make_person(with_spouse=True, with_image=True, with_aliases=True)
    with DatabaseSink(db_url) as sink:
        sink.write(person)

    engine = sa.create_engine(db_url)
    inspector = sa.inspect(engine)
    for table_name in TABLE_FIELDS:
        assert inspector.has_table(table_name), f"Missing table: {table_name}"
    engine.dispose()


def test_db_sink_people_row(db_url: str) -> None:
    import sqlalchemy as sa

    person = _make_person(with_dates=True)
    with DatabaseSink(db_url) as sink:
        sink.write(person)

    engine = sa.create_engine(db_url)
    with engine.connect() as conn:
        row = conn.execute(sa.text("SELECT * FROM people WHERE qid = 'Q1'")).mappings().one()
    engine.dispose()

    assert row["name"] == "Alice"
    assert row["dob_year"] == 1990
    assert row["dob_month"] == 6
    assert row["dob_calendar"] == "gregorian"
    assert row["dod_year"] == 2050


def test_db_sink_null_dates(db_url: str) -> None:
    import sqlalchemy as sa

    person = _make_person(with_dates=False)
    with DatabaseSink(db_url) as sink:
        sink.write(person)

    engine = sa.create_engine(db_url)
    with engine.connect() as conn:
        row = conn.execute(sa.text("SELECT dob_year FROM people WHERE qid = 'Q1'")).mappings().one()
    engine.dispose()
    assert row["dob_year"] is None


def test_db_sink_child_tables(db_url: str) -> None:
    import sqlalchemy as sa

    person = _make_person(with_spouse=True, with_image=True, with_aliases=True)
    with DatabaseSink(db_url) as sink:
        sink.write(person)

    engine = sa.create_engine(db_url)
    with engine.connect() as conn:
        alias_count = conn.execute(sa.text("SELECT COUNT(*) FROM person_aliases")).scalar()
        spouse_count = conn.execute(sa.text("SELECT COUNT(*) FROM person_spouses")).scalar()
        image_count = conn.execute(sa.text("SELECT COUNT(*) FROM person_images")).scalar()
        occupation_count = conn.execute(sa.text("SELECT COUNT(*) FROM person_occupations")).scalar()
    engine.dispose()

    assert alias_count == 2  # "Alicia", "Al"
    assert spouse_count == 1
    assert image_count == 1
    assert occupation_count == 2  # "writer", "teacher"


def test_db_sink_if_exists_fail_raises(db_url: str) -> None:
    person = _make_person()
    with DatabaseSink(db_url, if_exists="fail") as sink:
        sink.write(person)

    with pytest.raises(ValueError, match="already exist"):
        with DatabaseSink(db_url, if_exists="fail"):
            pass


def test_db_sink_if_exists_replace(db_url: str) -> None:
    import sqlalchemy as sa

    p1 = _make_person("Q1", "Alice")
    p2 = _make_person("Q2", "Bob")

    with DatabaseSink(db_url, if_exists="fail") as sink:
        sink.write(p1)

    with DatabaseSink(db_url, if_exists="replace") as sink:
        sink.write(p2)

    engine = sa.create_engine(db_url)
    with engine.connect() as conn:
        count = conn.execute(sa.text("SELECT COUNT(*) FROM people")).scalar()
        qids = [r[0] for r in conn.execute(sa.text("SELECT qid FROM people")).fetchall()]
    engine.dispose()

    assert count == 1
    assert qids == ["Q2"]


def test_db_sink_if_exists_append(db_url: str) -> None:
    import sqlalchemy as sa

    p1 = _make_person("Q1", "Alice")
    p2 = _make_person("Q2", "Bob")

    with DatabaseSink(db_url, if_exists="fail") as sink:
        sink.write(p1)

    with DatabaseSink(db_url, if_exists="append") as sink:
        sink.write(p2)

    engine = sa.create_engine(db_url)
    with engine.connect() as conn:
        count = conn.execute(sa.text("SELECT COUNT(*) FROM people")).scalar()
    engine.dispose()
    assert count == 2


def test_db_sink_if_exists_upsert(db_url: str) -> None:
    import sqlalchemy as sa

    original = _make_person("Q1", "Alice")
    updated = _make_person("Q1", "Alice Updated")

    with DatabaseSink(db_url, if_exists="fail") as sink:
        sink.write(original)

    with DatabaseSink(db_url, if_exists="upsert") as sink:
        sink.write(updated)

    engine = sa.create_engine(db_url)
    with engine.connect() as conn:
        count = conn.execute(sa.text("SELECT COUNT(*) FROM people")).scalar()
        name = conn.execute(sa.text("SELECT name FROM people WHERE qid='Q1'")).scalar()
    engine.dispose()

    assert count == 1
    assert name == "Alice Updated"


def test_db_sink_append_schema_mismatch(tmp_path: pathlib.Path) -> None:
    import sqlalchemy as sa

    # Create a table with a wrong schema manually.
    db_url = f"sqlite:///{tmp_path / 'mismatch.db'}"
    engine = sa.create_engine(db_url)
    with engine.connect() as conn:
        conn.execute(sa.text("CREATE TABLE people (qid TEXT PRIMARY KEY, wrong_col TEXT)"))
        conn.commit()
    engine.dispose()

    with pytest.raises(ValueError, match="Schema mismatch"):
        with DatabaseSink(db_url, if_exists="append"):
            pass


def test_db_sink_table_prefix(tmp_path: pathlib.Path) -> None:
    import sqlalchemy as sa

    db_url = f"sqlite:///{tmp_path / 'prefix.db'}"
    person = _make_person()
    with DatabaseSink(db_url, table_prefix="wk_") as sink:
        sink.write(person)

    engine = sa.create_engine(db_url)
    inspector = sa.inspect(engine)
    assert inspector.has_table("wk_people")
    assert not inspector.has_table("people")
    engine.dispose()


def test_db_sink_pipeline_state(db_url: str) -> None:
    """DatabaseSink correctly implements StatefulSink for state persistence."""
    state = {"completed_partitions": [1990, 1991], "in_progress": None}
    sink = DatabaseSink(db_url)
    with sink:
        assert sink.read_pipeline_state() == {}
        sink.write_pipeline_state(state)
        assert sink.read_pipeline_state() == state
        # Overwrite
        sink.write_pipeline_state({"completed_partitions": [1990], "in_progress": None})
        assert sink.read_pipeline_state()["completed_partitions"] == [1990]


def test_db_sink_missing_sqlalchemy(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def mock_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "sqlalchemy":
            raise ImportError("No module named 'sqlalchemy'")
        return real_import(name, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(builtins, "__import__", mock_import)
    with pytest.raises(ImportError, match="wikidata-bulk-people\\[db\\]"):
        DatabaseSink("sqlite:///test.db")


# ---------------------------------------------------------------------------
# run_people_pipeline accepts all three sinks
# ---------------------------------------------------------------------------


def test_pipeline_accepts_memory_sink() -> None:
    """run_people_pipeline runs end-to-end with MemorySink (no I/O)."""
    from unittest.mock import MagicMock, patch

    from wikidata_bulk_people._extract import run_people_pipeline
    from wikidata_bulk_people._models import PeopleFilter

    person = _make_person()

    with (
        patch("wikidata_bulk_people._extract.QIDStream") as mock_stream_cls,
        patch("wikidata_bulk_people._extract.PersonExtractor") as mock_extractor_cls,
    ):
        mock_stream = MagicMock()
        mock_stream.iter_partition.return_value = iter(["Q1"])
        mock_stream_cls.return_value = mock_stream

        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = person
        mock_extractor_cls.return_value = mock_extractor

        sink = MemorySink()
        run_people_pipeline(
            sink,
            filter=PeopleFilter(born_after=2020, born_before=2020),
            resume=False,
            user_agent="test/1.0",
            state_path=None,
        )

    assert len(sink.records) == 1
    assert sink.records[0].qid == "Q1"
