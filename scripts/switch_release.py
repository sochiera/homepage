#!/usr/bin/env python3
"""Run under the remote deployment lock; refuse shared-docroot data loss."""
import hashlib
import json
import re
import shutil
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


RETIRE_RE = re.compile(r"^(pobierz/[A-Za-z0-9][A-Za-z0-9._-]*)=([0-9a-f]{64})$")


def parse_retire(values):
    """Parse explicit PATH=SHA256 acknowledgements of live downloads to retire."""
    retire = {}
    for value in values:
        match = RETIRE_RE.fullmatch(value)
        if not match or match.group(1) in retire:
            raise ValueError("invalid --retire value; expected pobierz/NAME=SHA256")
        retire[match.group(1)] = match.group(2)
    return retire


def check_trees(current, staged, retire=None):
    retire = retire or {}
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
    for path, expected in retire.items():
        # Retiring is an explicit acknowledgement of one exact live file: if it is
        # gone or its bytes changed since it was inspected, nothing is replaced.
        if path not in current_files or digest(current_files[path]) != expected:
            raise ValueError("retired download differs from the acknowledged SHA-256")
        if path in incoming and incoming[path] == expected:
            raise ValueError("retired download is identical to the incoming file")
    for path, file in current_files.items():
        if path in retire:
            continue
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
    check_parlo(staged, incoming)


def check_parlo(staged, incoming):
    """The linked Parlo APK must be exactly the one named by parlo-wersja.json."""
    metadata = json.loads((staged / "pobierz/parlo-wersja.json").read_text())
    version = metadata["versionName"]
    if not re.fullmatch(r"[0-9]+(\.[0-9]+){1,3}", version):
        raise ValueError("unexpected Parlo APK version")
    apk = f"pobierz/parlo-{version}.apk"
    if apk not in incoming:
        raise ValueError("Parlo APK named by version metadata is missing")
    if incoming[apk] != metadata["sha256"] or (staged / apk).stat().st_size != metadata["size"]:
        raise ValueError("Parlo APK checksum disagrees with version metadata")
    home = (staged / "index.html").read_text()
    anchor = f'<a href="/{apk}" download>Parlo — aplikacja na Androida (APK {version})</a>'
    if home.count(anchor) != 1 or len(re.findall(r'href="/pobierz/parlo-', home)) != 1:
        raise ValueError("homepage Parlo link disagrees with version metadata")
    # The stable address must serve exactly the same verified release asset.
    alias = "pobierz/parlo.apk"
    if incoming.get(alias) != metadata["sha256"] or (staged / alias).stat().st_size != metadata["size"]:
        raise ValueError("Parlo stable alias disagrees with version metadata")


def preserve_retired(current, retire, retired):
    """Copy acknowledged live downloads outside the docroot before the switch.

    Nothing is deleted: the switch also keeps the whole previous tree as the
    backup, and this copy (with SHA256SUMS) survives backup pruning. Restore with
    `cp -p RETIRED/PATH DOCROOT/PATH` after rolling back, or just roll back.
    """
    if not retire:
        return
    if retired.exists():
        raise ValueError("retired directory already exists")
    staging = retired.with_name(retired.name + ".partial")
    if staging.exists():
        raise ValueError("retired directory already exists")
    sums = []
    for path, expected in sorted(retire.items()):
        target = staging / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(current / path, target)
        if digest(target) != expected:
            raise ValueError("retired copy differs from the acknowledged SHA-256")
        sums.append(f"{expected}  {path}\n")
    (staging / "SHA256SUMS").write_text("".join(sums))
    staging.rename(retired)


def switch(current, staged, backup, retire=None, retired=None):
    if backup.exists():
        raise ValueError("backup already exists")
    retire = retire or {}
    check_trees(current, staged, retire)
    if retire:
        if retired is None:
            raise ValueError("retired downloads need a directory outside the docroot")
        preserve_retired(current, retire, retired)
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
            args = sys.argv[1:]
            retire_values = []
            while len(args) > 3 and args[-2] == "--retire":
                retire_values.append(args[-1])
                args = args[:-2]
            if len(args) != 3:
                raise ValueError("usage: CURRENT STAGED BACKUP [--retire pobierz/NAME=SHA256]...")
            paths = tuple(Path(arg) for arg in args)
            retire = parse_retire(retire_values)
            if retire and not paths[2].name.startswith("backup-"):
                raise ValueError("backup directory must be named backup-RELEASE_ID")
            retired = paths[2].with_name(paths[2].name.replace("backup-", "retired-", 1)) if retire else None
            switch(*paths, retire=retire, retired=retired)
            print(fingerprint(paths[2]), digest(paths[2] / "index.html"))
    except (OSError, ValueError, KeyError) as error:
        # Never print content, metadata or payloads on validation failures.
        print(f"deploy switch refused: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
