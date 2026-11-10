from __future__ import annotations

import argparse
import pathlib
import re
import sys

_INIT_PATH = pathlib.Path("src/wikidata_bulk_people/__init__.py")
_MODELS_PATH = pathlib.Path("src/wikidata_bulk_people/_models.py")

_INIT_RE = re.compile(r'^(__version__\s*=\s*")(\d+)\.(\d+)\.(\d+)(")', re.MULTILINE)
_MODELS_RE = re.compile(r'^(_VERSION:\s*str\s*=\s*")(\d+)\.(\d+)\.(\d+)(")', re.MULTILINE)


def bump(version: str, segment: str) -> str:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not m:
        raise ValueError(f"version must be X.Y.Z, got {version!r}")
    major, minor, patch = (int(g) for g in m.groups())

    if segment == "major":
        return f"{major + 1}.0.0"
    if segment == "minor":
        return f"{major}.{minor + 1}.0"
    if segment == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"segment must be 'major', 'minor', or 'patch', got {segment!r}")


def _read_current_version(path: pathlib.Path, pattern: re.Pattern[str]) -> str:
    text = path.read_text(encoding="utf-8")
    m = pattern.search(text)
    if not m:
        raise ValueError(f"could not find version in {path}")
    return f"{m.group(2)}.{m.group(3)}.{m.group(4)}"


def _write_new_version(
    path: pathlib.Path, pattern: re.Pattern[str], new_version: str
) -> None:
    text = path.read_text(encoding="utf-8")
    new_text, n = pattern.subn(rf"\g<1>{new_version}\g<5>", text, count=1)
    if n != 1:
        raise ValueError(f"could not substitute version in {path}")
    path.write_text(new_text, encoding="utf-8")


def bump_files(segment: str, *, root: pathlib.Path | None = None) -> str:
    root = root or pathlib.Path.cwd()
    init_path = root / _INIT_PATH
    models_path = root / _MODELS_PATH

    init_version = _read_current_version(init_path, _INIT_RE)
    models_version = _read_current_version(models_path, _MODELS_RE)
    if init_version != models_version:
        raise ValueError(
            f"version drift detected: __init__.py={init_version!r} "
            f"_models.py={models_version!r}. Reconcile before bumping."
        )

    new_version = bump(init_version, segment)
    _write_new_version(init_path, _INIT_RE, new_version)
    _write_new_version(models_path, _MODELS_RE, new_version)
    return new_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bump wikidata-bulk-people version.")
    parser.add_argument("segment", choices=["major", "minor", "patch"])
    args = parser.parse_args(argv)

    new_version = bump_files(args.segment)
    print(new_version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
