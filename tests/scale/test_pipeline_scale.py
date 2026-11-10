import pathlib
import subprocess
import sys
import pytest

pytestmark = pytest.mark.scale
ROOT = pathlib.Path(__file__).resolve().parents[2]

@pytest.mark.parametrize("sink", ["jsonl", "csv", "database", "iterator", "memory"])
def test_one_hundred_thousand_full_people(tmp_path, sink):
    command = [sys.executable, str(ROOT / "scripts/benchmark_pipeline.py"),
               "--people", "100000", "--sink", sink, "--report", str(tmp_path / "result.json")]
    if sink in ("jsonl", "csv", "database"):
        command.extend(["--interrupt-after", "50037"])
    subprocess.run(command, cwd=ROOT, check=True, timeout=3600)

def test_million_qid_discovery(tmp_path):
    subprocess.run([sys.executable, str(ROOT / "scripts/benchmark_pipeline.py"),
                    "--people", "1000000", "--sink", "discovery", "--report", str(tmp_path / "result.json")],
                   cwd=ROOT, check=True, timeout=3600)
