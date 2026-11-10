import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from wikidata_bulk_people._models import (
    DateValue,
    ExtractionError,
    NotFoundError,
    Person,
    TransportError,
)


def _make_person() -> Person:
    return Person(
        qid="Q937",
        wikipedia_title="Albert Einstein",
        wikipedia_url="https://en.wikipedia.org/wiki/Albert_Einstein",
        name="Albert Einstein",
        description="german-born theoretical physicist",
        date_of_birth=DateValue(year=1879, month=3, day=14, calendar="gregorian", precision=11),
        date_of_death=None,
        place_of_birth="Ulm",
        place_of_death=None,
        sex_or_gender="male",
        lead_paragraph="Albert Einstein was a physicist.",
        aliases=[],
        citizenships=[],
        occupations=["theoretical physicist"],
        spouses=[],
        images=[],
        fetched_at="2026-01-01T00:00:00",
        lastrevid=12345,
    )


_PATCH_EXTRACT_PERSON = "wikidata_bulk_people.extract_person"
_PATCH_EXTRACT_PEOPLE = "wikidata_bulk_people.extract_people"
_PATCH_EXTRACT_PEOPLE_TO_DB = "wikidata_bulk_people.extract_people_to_db"
_PATCH_EXTRACT_PEOPLE_TO_CSV = "wikidata_bulk_people.extract_people_to_csv"


def _invoke(*args: str) -> pytest.ExceptionInfo[SystemExit]:
    from wikidata_bulk_people import cli

    with (
        patch.object(sys, "argv", ["wikidata-bulk-people", *args]),
        pytest.raises(SystemExit) as exc_info,
    ):
        cli.main()
    return exc_info


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    from wikidata_bulk_people import __version__

    exc = _invoke("version")
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_subcommand_exits_1() -> None:
    exc = _invoke()
    assert exc.value.code == 1


def test_people_help(capsys: pytest.CaptureFixture[str]) -> None:
    exc = _invoke("people", "--help")
    assert exc.value.code == 0
    assert "--out" in capsys.readouterr().out


def test_person_help(capsys: pytest.CaptureFixture[str]) -> None:
    exc = _invoke("person", "--help")
    assert exc.value.code == 0
    assert "QID_OR_TITLE" in capsys.readouterr().out


def test_person_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    person = _make_person()
    with patch(_PATCH_EXTRACT_PERSON, return_value=person):
        exc = _invoke("person", "Q937")

    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["qid"] == "Q937"
    assert data["name"] == "Albert Einstein"


def test_person_to_file(tmp_path: Path) -> None:
    out = tmp_path / "person.json"
    with patch(_PATCH_EXTRACT_PERSON, return_value=_make_person()):
        exc = _invoke("person", "Q937", "--out", str(out))

    assert exc.value.code == 0
    data = json.loads(out.read_text())
    assert data["qid"] == "Q937"


def test_person_not_found(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(_PATCH_EXTRACT_PERSON, side_effect=NotFoundError("Q999")):
        exc = _invoke("person", "Q999")

    assert exc.value.code == 1
    assert "Error" in capsys.readouterr().err


def test_person_extraction_error(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(_PATCH_EXTRACT_PERSON, side_effect=ExtractionError("Q1", "not a person")):
        exc = _invoke("person", "Q1")

    assert exc.value.code == 1
    assert "Error" in capsys.readouterr().err


def test_person_transport_error(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(_PATCH_EXTRACT_PERSON, side_effect=TransportError("https://example.com")):
        exc = _invoke("person", "Q937")

    assert exc.value.code == 2
    assert "Transport error" in capsys.readouterr().err


def test_people_out_jsonl(tmp_path: Path) -> None:
    out = tmp_path / "out.jsonl"
    with patch(_PATCH_EXTRACT_PEOPLE) as mock_ep:
        exc = _invoke("people", "--out", str(out))

    assert exc.value.code == 0
    mock_ep.assert_called_once()


def test_people_with_born_after(tmp_path: Path) -> None:
    out = tmp_path / "out.jsonl"
    with patch(_PATCH_EXTRACT_PEOPLE) as mock_ep:
        exc = _invoke("people", "--out", str(out), "--born-after", "1900")

    assert exc.value.code == 0
    call_kwargs = mock_ep.call_args.kwargs
    assert call_kwargs["filter"].born_after == 1900


def test_people_db(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path}/out.db"
    with patch(_PATCH_EXTRACT_PEOPLE_TO_DB) as mock_db:
        exc = _invoke("people", "--db", db_url)

    assert exc.value.code == 0
    mock_db.assert_called_once()


def test_people_db_upsert(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path}/out.db"
    with patch(_PATCH_EXTRACT_PEOPLE_TO_DB) as mock_db:
        exc = _invoke("people", "--db", db_url, "--if-exists", "upsert")

    assert exc.value.code == 0
    assert mock_db.call_args.kwargs.get("if_exists") == "upsert"


def test_people_upsert_without_db(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    out = tmp_path / "out.jsonl"
    exc = _invoke("people", "--out", str(out), "--if-exists", "upsert")

    assert exc.value.code == 1
    assert "upsert" in capsys.readouterr().err


def test_people_csv_dir(tmp_path: Path) -> None:
    csv_dir = tmp_path / "csv"
    with patch(_PATCH_EXTRACT_PEOPLE_TO_CSV) as mock_csv:
        exc = _invoke("people", "--csv-dir", str(csv_dir))

    assert exc.value.code == 0
    mock_csv.assert_called_once()


def test_people_transport_error(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    out = tmp_path / "out.jsonl"
    with patch(_PATCH_EXTRACT_PEOPLE, side_effect=TransportError("https://example.com")):
        exc = _invoke("people", "--out", str(out))

    assert exc.value.code == 2
    assert "Transport error" in capsys.readouterr().err


@pytest.mark.parametrize("flag,target", [("--out", _PATCH_EXTRACT_PEOPLE), ("--csv-dir", _PATCH_EXTRACT_PEOPLE_TO_CSV), ("--db", _PATCH_EXTRACT_PEOPLE_TO_DB)])
def test_limit_is_forwarded_to_every_pipeline(flag, target):
    with patch(target) as extract:
        exc = _invoke("people", flag, "destination", "--limit", "7")
    assert exc.value.code == 0
    assert extract.call_args.kwargs["options"].limit == 7


@pytest.mark.parametrize("flags", [
    ("--has-wikipedia-article", "--no-wikipedia-article"),
    ("--resume", "--no-resume"),
])
def test_conflicting_switches_are_rejected(flags):
    with patch(_PATCH_EXTRACT_PEOPLE) as extract:
        assert _invoke("people", "--out", "unused", *flags).value.code == 2
        extract.assert_not_called()


def test_table_prefix_requires_database(capsys):
    with patch(_PATCH_EXTRACT_PEOPLE) as extract:
        assert _invoke("people", "--out", "unused", "--table-prefix", "wk_").value.code == 1
        extract.assert_not_called()
    assert "only valid with --db" in capsys.readouterr().err


@pytest.mark.parametrize("flag,target,extra", [
    ("--out", _PATCH_EXTRACT_PEOPLE, "bulk"),
    ("--db", _PATCH_EXTRACT_PEOPLE_TO_DB, "db"),
])
def test_missing_extra_is_actionable_without_traceback(flag, target, extra, capsys):
    with patch(target, side_effect=ImportError(f"pip install wikidata-bulk-people[{extra}]")):
        assert _invoke("people", flag, "unused").value.code == 1
    message = capsys.readouterr().err
    assert f"[{extra}]" in message and "Traceback" not in message
