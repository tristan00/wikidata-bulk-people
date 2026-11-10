import csv
import pathlib
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from wikidata_bulk_people._models import DateValue, ImageRef, Person, SpouseRecord
from wikidata_bulk_people._sinks import TABLE_FIELDS, CSVSink, DatabaseSink


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
    dob = (
        DateValue(year=1990, month=6, day=15, calendar="gregorian", precision=11)
        if with_dates
        else None
    )
    dod = (
        DateValue(year=2050, month=1, day=1, calendar="gregorian", precision=9)
        if with_dates
        else None
    )
    spouses = []
    if with_spouse:
        spouses = [
            SpouseRecord(
                qid="Q99",
                name="Bob",
                start_date=DateValue(
                    year=2010, month=None, day=None, calendar="gregorian", precision=9
                ),
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
    assert rows[0]["dob_year"] == ""


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
    with pytest.raises(FileExistsError, match="people.csv"), CSVSink(tmp_path, if_exists="fail"):
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

    assert alias_count == 2
    assert spouse_count == 1
    assert image_count == 1
    assert occupation_count == 2


def test_db_sink_if_exists_fail_raises(db_url: str) -> None:
    person = _make_person()
    with DatabaseSink(db_url, if_exists="fail") as sink:
        sink.write(person)

    with pytest.raises(ValueError, match="already exist"), DatabaseSink(db_url, if_exists="fail"):
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


    db_url = f"sqlite:///{tmp_path / 'mismatch.db'}"
    engine = sa.create_engine(db_url)
    with engine.connect() as conn:
        conn.execute(sa.text("CREATE TABLE people (qid TEXT PRIMARY KEY, wrong_col TEXT)"))
        conn.commit()
    engine.dispose()

    with (
        pytest.raises(ValueError, match="Schema mismatch"),
        DatabaseSink(db_url, if_exists="append"),
    ):
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
    state = {"completed": False, "last_qid": "Q1234"}
    sink = DatabaseSink(db_url)
    with sink:
        assert sink.read_pipeline_state() == {}
        sink.write_pipeline_state(state)
        assert sink.read_pipeline_state() == state

        sink.write_pipeline_state({"completed": True, "last_qid": None})
        assert sink.read_pipeline_state()["completed"] is True


def test_db_sink_missing_sqlalchemy(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def mock_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "sqlalchemy":
            raise ImportError("No module named 'sqlalchemy'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", mock_import)
    with pytest.raises(ImportError, match="wikidata-bulk-people\\[db\\]"):
        DatabaseSink("sqlite:///test.db")


@pytest.mark.parametrize("phase", ["open", "body", "exit"])
def test_csv_cleanup_closes_every_file_and_preserves_first_error(tmp_path, monkeypatch, phase):
    sink = CSVSink(tmp_path)
    first, second = Mock(), Mock()
    close_error = OSError("close failed")
    first.close.side_effect = close_error
    second.close.side_effect = RuntimeError("another close failed")
    sink._open_files = [first, second]
    sink._writers["people"] = Mock()
    original = ValueError("operation failed")
    monkeypatch.setattr(sink, "_open", Mock(side_effect=original if phase == "open" else None,
                                           return_value=sink))
    expected = close_error if phase == "exit" else original
    with pytest.raises(type(expected)) as caught:
        with sink:
            if phase == "body":
                raise original
    assert caught.value is expected
    first.close.assert_called_once_with()
    second.close.assert_called_once_with()
    assert sink._open_files == []
    assert sink._writers == {}


@pytest.mark.parametrize("phase", ["open", "body", "flush"])
def test_db_cleanup_preserves_operation_error(db_url, monkeypatch, phase):
    sink = DatabaseSink(db_url)
    connection, engine = Mock(), Mock()
    connection.rollback.side_effect = OSError("rollback failed")
    connection.close.side_effect = OSError("close failed")
    engine.dispose.side_effect = OSError("dispose failed")
    sink._conn, sink._engine = connection, engine
    sink._buffers["people"].append({"qid": "Q1"})
    original = ValueError("operation failed")
    monkeypatch.setattr(sink, "_open", Mock(side_effect=original if phase == "open" else None,
                                           return_value=sink))
    monkeypatch.setattr(sink, "_flush", Mock(side_effect=original if phase == "flush" else None))
    with pytest.raises(ValueError) as caught:
        with sink:
            if phase == "body":
                raise original
    assert caught.value is original
    connection.close.assert_called_once_with()
    engine.dispose.assert_called_once_with()
    assert sink._conn is None and sink._engine is None
    assert not any(sink._buffers.values())


@pytest.mark.parametrize("failed_action", ["rollback", "close", "dispose"])
def test_db_cleanup_attempts_all_actions(db_url, monkeypatch, failed_action):
    sink = DatabaseSink(db_url)
    sink._managed = True
    connection, engine = Mock(), Mock()
    sink._conn, sink._engine = connection, engine
    sink._buffers["people"].append({"qid": "Q1"})
    original = OSError(f"{failed_action} failed")
    owner = engine if failed_action == "dispose" else connection
    getattr(owner, failed_action).side_effect = original
    monkeypatch.setattr(sink, "_open", lambda: sink)
    with pytest.raises(OSError) as caught:
        with sink:
            pass
    assert caught.value is original
    connection.rollback.assert_called_once_with()
    connection.close.assert_called_once_with()
    engine.dispose.assert_called_once_with()
    assert sink._conn is None and sink._engine is None
    assert not any(sink._buffers.values())


def test_db_flush_preserves_write_failure_when_rollback_fails(db_url, monkeypatch):
    sink = DatabaseSink(db_url)
    connection, engine = Mock(), Mock()
    sink._conn, sink._engine = connection, engine
    sink._tables = dict(sink._build_schema().tables)
    original = OSError("write failed")
    connection.execute.side_effect = original
    connection.rollback.side_effect = OSError("rollback failed")
    connection.close.side_effect = OSError("close failed")
    monkeypatch.setattr(sink, "_open", lambda: sink)
    with pytest.raises(OSError) as caught:
        with sink:
            sink.write(_make_person())
    assert caught.value is original
    connection.rollback.assert_called_once_with()
    connection.close.assert_called_once_with()
    engine.dispose.assert_called_once_with()
    assert sink._conn is None and sink._engine is None
    assert not any(sink._buffers.values())
