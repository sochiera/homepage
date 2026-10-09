import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("switch_release", Path(__file__).resolve().parents[1] / "scripts/switch_release.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def manifest(root, files):
    root.mkdir()
    entries = []
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        entries.append({"path": name, "sha256": hashlib.sha256(content).hexdigest()})
    (root / "build-manifest.json").write_text(json.dumps({"artifacts": entries}))


@pytest.fixture
def trees(tmp_path):
    current, staged, backup = (tmp_path / name for name in ("current", "staged", "backup"))
    downloads = {"pobierz/mealspire-1.5.apk": b"apk", "pobierz/mealspire-1.2.apk": b"older apk"}
    downloads["pobierz/mealspire-wersja.json"] = json.dumps({"versionName": "1.5", "sha256": hashlib.sha256(b"apk").hexdigest()}).encode()
    manifest(current, downloads | {"index.html": b"old", "de/opowiadania/index.html": b"retired translation"})
    # Downloads may have been added after the previous homepage manifest.
    previous = json.loads((current / "build-manifest.json").read_text())
    previous["artifacts"] = [e for e in previous["artifacts"] if not e["path"].startswith("pobierz/")]
    (current / "build-manifest.json").write_text(json.dumps(previous))
    (current / "v2").mkdir()
    (current / "v2/index.html").write_text("preview")
    manifest(staged, downloads | {"index.html": '<a href="/pobierz/mealspire-1.5.apk" download>Mealspire — aplikacja na Androida (APK 1.5)</a>'.encode()})
    return current, staged, backup


def test_switch_preserves_complete_backup_and_downloads(trees):
    current, staged, backup = trees
    before = guard.fingerprint(current)
    guard.switch(*trees)
    assert guard.fingerprint(backup) == before
    assert (backup / "v2/index.html").read_text() == "preview"
    assert (backup / "de/opowiadania/index.html").is_file()
    assert not (current / "v2").exists() and not (current / "de").exists()
    for path in (backup / "pobierz").iterdir():
        assert path.read_bytes() == (current / "pobierz" / path.name).read_bytes()


@pytest.mark.parametrize("drift", ["new_download", "changed_download", "unknown_asset", "stale_anchor", "staged_tamper"])
def test_refuses_drift_before_touching_docroot(trees, drift):
    current, staged, backup = trees
    if drift == "new_download":
        (current / "pobierz/mealspire-1.6.apk").write_bytes(b"new")
    elif drift == "changed_download":
        (current / "pobierz/mealspire-1.5.apk").write_bytes(b"changed")
    elif drift == "unknown_asset":
        (current / "other-service.json").write_text("retain me")
    else:
        (staged / "index.html").write_text("old APK link")
        if drift == "stale_anchor":
            data = json.loads((staged / "build-manifest.json").read_text())
            for item in data["artifacts"]:
                if item["path"] == "index.html":
                    item["sha256"] = guard.digest(staged / "index.html")
            (staged / "build-manifest.json").write_text(json.dumps(data))
    with pytest.raises(ValueError):
        guard.switch(*trees)
    assert (current / "index.html").read_text() == "old"
    assert staged.is_dir() and not backup.exists()
