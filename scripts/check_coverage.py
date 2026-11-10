import json
import pathlib
import sys

report = json.loads(pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "coverage.json").read_text())
required = {"_clients.py", "_discovery.py", "_extract.py", "_bulk.py", "_cache.py", "_sinks.py", "_snapshot.py", "_storage.py"}
found = set()
failures = []
for name, entry in report["files"].items():
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    if base in required:
        found.add(base)
        percentage = entry["summary"]["percent_covered"]
        if percentage < 80:
            failures.append(f"{base}: {percentage:.1f}% < 80%")
if found != required:
    failures.append(f"Missing coverage: {sorted(required - found)}")
if failures:
    raise SystemExit("\n".join(failures))
print("Core-module coverage gates passed (generated enums excluded).")
