"""Unit tests for scripts/bump_version.py."""

from __future__ import annotations

import pathlib
import sys

import pytest

# Add scripts/ to sys.path so we can import bump_version directly without
# making it a proper package.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import bump_version  # noqa: E402


class TestBumpString:
    def test_patch_increments_third_segment(self) -> None:
        assert bump_version.bump("0.1.0", "patch") == "0.1.1"

    def test_minor_increments_second_resets_third(self) -> None:
        assert bump_version.bump("0.1.5", "minor") == "0.2.0"

    def test_major_increments_first_resets_others(self) -> None:
        assert bump_version.bump("1.2.3", "major") == "2.0.0"

    def test_unknown_segment_raises(self) -> None:
        with pytest.raises(ValueError, match="segment"):
            bump_version.bump("0.1.0", "build")

    def test_malformed_version_raises(self) -> None:
        with pytest.raises(ValueError, match="X.Y.Z"):
            bump_version.bump("0.1", "patch")


def _make_repo(tmp_path: pathlib.Path, version: str) -> pathlib.Path:
    """Lay out a minimal repo skeleton with __init__.py and _models.py."""
    init = tmp_path / "src" / "wikidata_bulk_people" / "__init__.py"
    models = tmp_path / "src" / "wikidata_bulk_people" / "_models.py"
    init.parent.mkdir(parents=True)
    init.write_text(
        f'"""docstring"""\n\nimport os\n\n__version__ = "{version}"\n\n'
        'def something() -> None:\n    pass\n',
        encoding="utf-8",
    )
    models.write_text(
        f'"""docstring"""\n\n# Keep in sync with __version__ in __init__.py\n'
        f'_VERSION: str = "{version}"\n\n'
        'class Foo:\n    pass\n',
        encoding="utf-8",
    )
    return tmp_path


class TestBumpFiles:
    def test_patch_bump_updates_both_files(self, tmp_path: pathlib.Path) -> None:
        root = _make_repo(tmp_path, "0.1.0")
        new_version = bump_version.bump_files("patch", root=root)

        assert new_version == "0.1.1"
        init_text = (root / "src/wikidata_bulk_people/__init__.py").read_text(encoding="utf-8")
        models_text = (root / "src/wikidata_bulk_people/_models.py").read_text(encoding="utf-8")
        assert '__version__ = "0.1.1"' in init_text
        assert '_VERSION: str = "0.1.1"' in models_text

    def test_minor_bump_resets_patch_in_both_files(self, tmp_path: pathlib.Path) -> None:
        root = _make_repo(tmp_path, "0.4.7")
        new_version = bump_version.bump_files("minor", root=root)

        assert new_version == "0.5.0"
        init_text = (root / "src/wikidata_bulk_people/__init__.py").read_text(encoding="utf-8")
        models_text = (root / "src/wikidata_bulk_people/_models.py").read_text(encoding="utf-8")
        assert '__version__ = "0.5.0"' in init_text
        assert '_VERSION: str = "0.5.0"' in models_text

    def test_major_bump_resets_others(self, tmp_path: pathlib.Path) -> None:
        root = _make_repo(tmp_path, "1.2.3")
        new_version = bump_version.bump_files("major", root=root)
        assert new_version == "2.0.0"

    def test_drift_between_files_raises(self, tmp_path: pathlib.Path) -> None:
        root = _make_repo(tmp_path, "0.1.0")
        # Manually corrupt _models.py so its version differs.
        models = root / "src/wikidata_bulk_people/_models.py"
        models.write_text(
            models.read_text(encoding="utf-8").replace("0.1.0", "0.1.5"),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="drift"):
            bump_version.bump_files("patch", root=root)

    def test_real_repo_versions_match(self) -> None:
        """Sanity: the real repo's two version constants must already be in sync."""
        init_v = bump_version._read_current_version(
            _REPO_ROOT / bump_version._INIT_PATH, bump_version._INIT_RE
        )
        models_v = bump_version._read_current_version(
            _REPO_ROOT / bump_version._MODELS_PATH, bump_version._MODELS_RE
        )
        assert init_v == models_v
