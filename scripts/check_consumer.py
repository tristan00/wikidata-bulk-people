import dataclasses
import importlib.util
import json
import pathlib
import sqlite3
import sys
import tempfile
from contextlib import closing

import requests
import wikidata_bulk_people as library


def main():
    package = pathlib.Path(library.__file__).resolve()
    assert package.is_relative_to(pathlib.Path(sys.prefix).resolve()), package
    assert sys.prefix != sys.base_prefix, "Consumer check requires an isolated venv"
    assert "include-system-site-packages = false" in pathlib.Path(sys.prefix, "pyvenv.cfg").read_text().lower()
    for name in library.__all__:
        assert getattr(library, name) is not None, name
    assert dataclasses.is_dataclass(library.Person)
    has_db = importlib.util.find_spec("sqlalchemy") is not None
    has_bulk = importlib.util.find_spec("pyarrow") is not None

    def no_network(*args, **kwargs):
        raise AssertionError("Empty consumer checks must not use HTTP")

    requests.sessions.Session.request = no_network
    with tempfile.TemporaryDirectory(prefix="wbp-consumer-") as temporary:
        directory = pathlib.Path(temporary)
        options = library.BulkOptions(limit=0)
        assert list(library.iter_people(options=options)) == []
        assert library.extract_people_to_memory(options=options) == []
        out = directory / "people.jsonl"
        library.extract_people(out, options=options, if_exists="fail")
        assert out.read_bytes() == b""
        library.extract_people(out, options=options)
        library.extract_people_to_csv(directory / "csv", options=options)
        assert len(list((directory / "csv").glob("*.csv"))) == 6
        url = "sqlite:///" + (directory / "people.db").as_posix()
        db_options = library.BulkOptions(limit=0, job_path=directory / "db.job.sqlite3")
        try:
            library.extract_people_to_db(url, options=db_options)
        except ImportError as exc:
            assert not has_db and "[db]" in str(exc), exc
        else:
            assert has_db
            with closing(sqlite3.connect(directory / "people.db")) as connection:
                assert connection.execute("SELECT COUNT(*) FROM people").fetchone() == (0,)
        if not has_bulk:
            try:
                next(library.iter_people(options=library.BulkOptions(
                    limit=1, public_snapshot=directory / "sources")))
            except ImportError as exc:
                assert "[bulk]" in str(exc), exc
            else:
                raise AssertionError("Missing bulk extra was not diagnosed before acquisition")
    print(json.dumps({"installed_path": str(package), "python": sys.version,
                      "db_extra": has_db, "bulk_extra": has_bulk,
                      "public_exports": len(library.__all__), "passed": True}))


if __name__ == "__main__":
    main()
