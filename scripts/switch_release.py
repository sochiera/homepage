#!/usr/bin/env python3
"""Run under the remote deployment lock; refuse shared-docroot data loss."""
import hashlib
import json
import re
import sys
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(root):
    entries = [(p.relative_to(root).as_posix(), digest(p))
               for p in sorted(root.rglob("*")) if p.is_file()]
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()


def artifacts(root):
    manifest = json.loads((root / "build-manifest.json").read_text())
    result = {}
    for item in manifest["artifacts"]:
        path = Path(item["path"])
        if path.is_absolute() or ".." in path.parts or path.as_posix() in result:
            raise ValueError("unsafe manifest path")
        result[path.as_posix()] = item["sha256"]
    return result


def check_trees(current, staged):
    previous, incoming = artifacts(current), artifacts(staged)
    current_files = {p.relative_to(current).as_posix(): p for p in current.rglob("*") if p.is_file()}
    staged_files = {p.relative_to(staged).as_posix(): p for p in staged.rglob("*") if p.is_file()}
    if any(p.is_symlink() for root in (current, staged) for p in root.rglob("*")):
        raise ValueError("linked docroot asset")
    if set(staged_files) != set(incoming) | {"build-manifest.json"}:
        raise ValueError("staged file set differs from manifest")
    if any(digest(staged_files[path]) != expected for path, expected in incoming.items()):
        raise ValueError("staged artifact differs from manifest")
    # /v2 is an explicitly retired preview. Old manifest-owned pages (including
    # unpublished translations) may be retired. Unknown shared assets may not.
    unknown = {p for p in current_files if p not in previous and p not in incoming
               and p != "build-manifest.json" and not p.startswith("v2/")}
    if unknown:
        raise ValueError("unmanaged docroot assets; preserve them before deploying")
    for path, file in current_files.items():
        if path.startswith("pobierz/") and (path not in incoming or digest(file) != incoming[path]):
            raise ValueError("live downloads drift; refresh download snapshot before deploying")
    metadata = json.loads((staged / "pobierz/mealspire-wersja.json").read_text())
    version = metadata["versionName"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+", version):
        raise ValueError("unexpected APK version")
    apk = f"pobierz/mealspire-{version}.apk"
    if incoming.get(apk) != metadata["sha256"]:
        raise ValueError("APK checksum disagrees with version metadata")
    anchor = f'<a href="/{apk}" download>Mealspire — aplikacja na Androida (APK {version})</a>'
    if (staged / "index.html").read_text().count(anchor) != 1:
        raise ValueError("homepage APK link disagrees with version metadata")


def switch(current, staged, backup):
    if backup.exists():
        raise ValueError("backup already exists")
    check_trees(current, staged)
    current.rename(backup)
    try:
        staged.rename(current)
    except OSError:
        backup.rename(current)
        raise


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--fingerprint":
            print(fingerprint(Path(sys.argv[2])))
        else:
            paths = tuple(Path(arg) for arg in sys.argv[1:])
            switch(*paths)
            print(fingerprint(paths[2]), digest(paths[2] / "index.html"))
    except (OSError, ValueError, KeyError) as error:
        # Never print content, metadata or payloads on validation failures.
        print(f"deploy switch refused: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
