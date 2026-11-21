import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("switch_release", Path(__file__).resolve().parents[1] / "scripts/switch_release.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


HOME_LINKS = ('<a href="/pobierz/mealspire-1.5.apk" download>Mealspire — aplikacja na Androida (APK 1.5)</a>'
              '<a href="/pobierz/parlo-%s.apk" download>Parlo — aplikacja na Androida (APK %s)</a>')


def resign(root):
    data = json.loads((root / "build-manifest.json").read_text())
    data["artifacts"] = [{"path": p.relative_to(root).as_posix(), "sha256": guard.digest(p)}
                         for p in sorted(root.rglob("*")) if p.is_file() and p.name != "build-manifest.json"]
    (root / "build-manifest.json").write_text(json.dumps(data))


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
    parlo = {"versionName": "0.2.0.7", "size": len(b"parlo apk"), "sha256": hashlib.sha256(b"parlo apk").hexdigest()}
    downloads |= {"pobierz/parlo-0.2.0.6.apk": b"older parlo", "pobierz/parlo-0.2.0.7.apk": b"parlo apk",
                  "pobierz/parlo.apk": b"parlo apk", "pobierz/parlo-wersja.json": json.dumps(parlo).encode()}
    manifest(current, downloads | {"index.html": b"old", "de/opowiadania/index.html": b"retired translation"})
    # Downloads may have been added after the previous homepage manifest.
    previous = json.loads((current / "build-manifest.json").read_text())
    previous["artifacts"] = [e for e in previous["artifacts"] if not e["path"].startswith("pobierz/")]
    (current / "build-manifest.json").write_text(json.dumps(previous))
    (current / "v2").mkdir()
    (current / "v2/index.html").write_text("preview")
    manifest(staged, downloads | {"index.html": (HOME_LINKS % ("0.2.0.7", "0.2.0.7")).encode()})
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


@pytest.mark.parametrize("drift", ["parlo_missing_apk", "parlo_wrong_version_link", "parlo_hash_mismatch", "parlo_two_links"])
def test_refuses_parlo_link_or_artifact_drift(trees, drift):
    current, staged, backup = trees
    home = staged / "index.html"
    if drift == "parlo_missing_apk":
        (staged / "pobierz/parlo-0.2.0.7.apk").unlink()
    elif drift == "parlo_wrong_version_link":
        home.write_text(HOME_LINKS % ("0.2.0.6", "0.2.0.6"))
    elif drift == "parlo_hash_mismatch":
        (staged / "pobierz/parlo-0.2.0.7.apk").write_bytes(b"tampered")
    else:
        home.write_text(HOME_LINKS % ("0.2.0.7", "0.2.0.7") + '<a href="/pobierz/parlo-0.2.0.6.apk">x</a>')
    resign(staged)  # the manifest agrees with the files, so only the Parlo guard can refuse
    # Current docroot must not count as drift for removed/changed staged files.
    if drift in {"parlo_missing_apk", "parlo_hash_mismatch"}:
        (current / "pobierz/parlo-0.2.0.7.apk").unlink()
    with pytest.raises(ValueError, match="Parlo"):
        guard.switch(*trees)
    assert (current / "index.html").read_text() == "old"
    assert staged.is_dir() and not backup.exists()


MANUAL = b"manually uploaded parlo"
MANUAL_SHA = hashlib.sha256(MANUAL).hexdigest()


@pytest.fixture
def manual_alias(trees, tmp_path):
    """Production today: /pobierz/parlo.apk was uploaded by hand and differs from the release."""
    current, staged, _ = trees
    (current / "pobierz/parlo.apk").write_bytes(MANUAL)
    return trees, tmp_path / "retired"


def test_stable_alias_must_equal_the_versioned_release(trees):
    current, staged, backup = trees
    (staged / "pobierz/parlo.apk").write_bytes(b"older parlo")
    (current / "pobierz/parlo.apk").write_bytes(b"older parlo")  # so only the Parlo guard can refuse
    resign(staged)
    with pytest.raises(ValueError, match="stable alias"):
        guard.switch(*trees)
    (staged / "pobierz/parlo.apk").unlink()
    resign(staged)
    with pytest.raises(ValueError):
        guard.switch(*trees)
    assert (current / "index.html").read_text() == "old" and not backup.exists()


def test_unacknowledged_manual_alias_still_blocks(manual_alias):
    trees, retired = manual_alias
    current, staged, backup = trees
    with pytest.raises(ValueError, match="live downloads drift"):
        guard.switch(*trees)
    assert (current / "pobierz/parlo.apk").read_bytes() == MANUAL and not backup.exists() and not retired.exists()


@pytest.mark.parametrize("acknowledged", ["0" * 64, hashlib.sha256(b"parlo apk").hexdigest()])
def test_retire_refuses_wrong_hash_without_touching_anything(manual_alias, acknowledged):
    trees, retired = manual_alias
    current, staged, backup = trees
    with pytest.raises(ValueError, match="retired download"):
        guard.switch(*trees, retire={"pobierz/parlo.apk": acknowledged}, retired=retired)
    assert (current / "pobierz/parlo.apk").read_bytes() == MANUAL
    assert staged.is_dir() and not backup.exists() and not retired.exists()


def test_retire_requires_directory_outside_docroot(manual_alias):
    trees, _ = manual_alias
    with pytest.raises(ValueError, match="outside the docroot"):
        guard.switch(*trees, retire={"pobierz/parlo.apk": MANUAL_SHA})
    assert (trees[0] / "pobierz/parlo.apk").read_bytes() == MANUAL and not trees[2].exists()


def test_retire_migrates_manual_file_out_of_docroot_and_is_restorable(manual_alias):
    trees, retired = manual_alias
    current, staged, backup = trees
    before = guard.fingerprint(current)
    guard.switch(*trees, retire={"pobierz/parlo.apk": MANUAL_SHA}, retired=retired)
    # Live: both addresses serve the same verified release asset.
    assert (current / "pobierz/parlo.apk").read_bytes() == (current / "pobierz/parlo-0.2.0.7.apk").read_bytes() == b"parlo apk"
    assert (current / "pobierz/parlo-0.2.0.6.apk").read_bytes() == b"older parlo"
    # Nothing deleted: exact copy with checksum outside the docroot, plus the full backup.
    assert (retired / "pobierz/parlo.apk").read_bytes() == MANUAL
    assert (retired / "SHA256SUMS").read_text() == f"{MANUAL_SHA}  pobierz/parlo.apk\n"
    assert guard.fingerprint(backup) == before
    assert not retired.with_name(retired.name + ".partial").exists()
    # Restore path 1: roll back the whole tree (what deploy.sh does on failure).
    current.rename(current.with_name("failed")); backup.rename(current)
    assert guard.fingerprint(current) == before
    # Restore path 2: put the retired copy back from SHA256SUMS.
    (current / "pobierz/parlo.apk").write_bytes(b"x")
    expected, rel = (retired / "SHA256SUMS").read_text().split()
    (current / rel).write_bytes((retired / rel).read_bytes())
    assert guard.digest(current / rel) == expected


def test_retire_refuses_existing_retired_directory(manual_alias):
    trees, retired = manual_alias
    retired.mkdir()
    with pytest.raises(ValueError, match="retired directory already exists"):
        guard.switch(*trees, retire={"pobierz/parlo.apk": MANUAL_SHA}, retired=retired)
    assert (trees[0] / "pobierz/parlo.apk").read_bytes() == MANUAL and not trees[2].exists()


@pytest.mark.parametrize("value", ["pobierz/parlo.apk=" + "A" * 64, "../index.html=" + "0" * 64,
                                   "pobierz/../index.html=" + "0" * 64, "index.html=" + "0" * 64, "pobierz/parlo.apk"])
def test_retire_values_are_strictly_validated(value):
    with pytest.raises(ValueError, match="invalid --retire"):
        guard.parse_retire([value])


def test_switch_cli_with_retire_names_retired_dir_after_release(manual_alias, tmp_path):
    import subprocess, sys
    trees, _ = manual_alias
    current, staged, _ = trees
    backup = tmp_path / "backup-20261121T120000Z-0123456789ab"
    script = Path(guard.__file__)
    result = subprocess.run([sys.executable, "-", str(current), str(staged), str(backup), "--retire", f"pobierz/parlo.apk={MANUAL_SHA}"],
                            stdin=script.open(), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "retired-20261121T120000Z-0123456789ab/pobierz/parlo.apk").read_bytes() == MANUAL
