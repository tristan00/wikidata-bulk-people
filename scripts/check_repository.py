import argparse
import ast
import io
import pathlib
import subprocess
import sys
import tokenize


def git(*args, input=None):
    return subprocess.run(["git", *args], input=input, capture_output=True,
                          text=True, encoding="utf-8", check=True).stdout


def forbidden(path):
    p = pathlib.PurePosixPath(path)
    parts = {part.lower() for part in p.parts}
    return (bool(parts & {"audit", "docs", ".runs", "runs", "benchmarks", "sources",
                         "public-sources", ".venv", ".wikidata-bulk-people", "agents.md"})
            or p.suffix.lower() in {".parquet", ".sqlite3", ".bundle", ".pdf", ".doc", ".docx",
                                    ".rst", ".adoc", ".txt", ".rtf", ".odt", ".html"}
            or p.suffix.lower() == ".md" and path not in {"README.md", "CHANGELOG.md"}
            or path.lower().endswith((".sqlite3-wal", ".sqlite3-shm")))


def source_errors(path, source):
    if path.endswith(".py"):
        tree = ast.parse(source, filename=path)
        if any(t.type == tokenize.COMMENT for t in tokenize.generate_tokens(io.StringIO(source).readline)):
            yield f"Comments: {path}"
        if any(isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
               and ast.get_docstring(n) is not None for n in ast.walk(tree)):
            yield f"Docstrings: {path}"
    elif path.endswith((".yml", ".yaml", ".toml")) or path in {".gitignore", ".gitattributes"}:
        for line in source.splitlines():
            quote = None
            escaped = False
            for character in line:
                if escaped:
                    escaped = False
                elif character == "\\" and quote != "'":
                    escaped = True
                elif quote is not None:
                    if character == quote:
                        quote = None
                elif character in {"'", '"'}:
                    quote = character
                elif character == "#":
                    yield f"Comments: {path}"
                    return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args()
    if git("rev-parse", "--is-shallow-repository").strip() == "true":
        parser.error("Full Git history is required")
    objects = {}
    for row in git("rev-list", "--objects", "HEAD").splitlines():
        sha, _, path = row.partition(" ")
        objects[sha] = path
    errors = []
    for row in git("ls-files", "--stage", "-z").split("\0"):
        if not row:
            continue
        info, path = row.split("\t", 1)
        mode, sha, stage = info.split()
        if stage != "0":
            errors.append(f"Unmerged file: {path}")
            continue
        objects[sha] = path
        if forbidden(path):
            errors.append(f"Forbidden tracked path: {path}")
        if path.endswith((".py", ".yml", ".yaml", ".toml")) or path in {".gitignore", ".gitattributes"}:
            errors.extend(source_errors(path, git("cat-file", "blob", sha)))
    largest = (0, "")
    rows = git("cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
               input="\n".join(objects) + "\n")
    for row in rows.splitlines():
        sha, kind, size = row.split()
        if kind == "blob":
            size = int(size)
            largest = max(largest, (size, objects[sha]))
            if size > 1024 * 1024:
                errors.append(f"File exceeds 1 MiB: {objects[sha]} ({size:,} bytes)")
    revisions = "HEAD" if set(args.base) == {"0"} else f"{args.base}..HEAD"
    for sha in git("rev-list", revisions).splitlines():
        if git("show", "-s", "--format=%B", sha).strip():
            errors.append(f"Nonempty commit message: {sha}")
        for path in git("diff-tree", "--root", "--no-commit-id", "--name-only", "-r", sha).splitlines():
            if forbidden(path):
                errors.append(f"Forbidden path in branch history: {path}")
    if errors:
        print("\n".join(sorted(set(errors))), file=sys.stderr)
        return 1
    print(f"Repository checks passed. Largest historical/indexed blob: {largest[0]:,} bytes ({largest[1]}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
