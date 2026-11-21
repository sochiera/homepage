from __future__ import annotations

import functools
import importlib.util
import json
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from test_build_site import build, writing  # noqa: F401  (fixture)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/verify_parlo_release.py"
spec = importlib.util.spec_from_file_location("verify_parlo_release", SCRIPT)
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)
METADATA = verify.load_metadata()


def release(**overrides):
    data = {"tag_name": METADATA["releaseTag"], "draft": False, "prerelease": False,
            "assets": [{"name": "parlo.apk", "size": METADATA["size"], "digest": f"sha256:{METADATA['sha256']}"}]}
    return data | overrides


def github(payload, status=200):
    return lambda url, headers=None: (status, json.dumps(payload).encode())


def test_official_release_matches_metadata():
    assert "official" in verify.check_official(METADATA, github(release()))


@pytest.mark.parametrize("payload, reason", [
    (release(tag_name="android-native-v0.2.0.8"), "latest stable release is android-native-v0.2.0.8"),
    (release(prerelease=True), "not a stable"),
    (release(assets=[{"name": "parlo.apk", "size": METADATA["size"], "digest": "sha256:" + "0" * 64}]), "SHA-256"),
    (release(assets=[]), "no single"),
])
def test_official_release_drift_is_reported(payload, reason):
    with pytest.raises(verify.Mismatch, match=reason):
        verify.check_official(METADATA, github(payload))


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def site(writing, tmp_path):  # noqa: F811
    out = tmp_path / "site"
    result = build(writing, out)
    assert result.returncode == 0, result.stderr
    handler = functools.partial(QuietHandler, directory=str(out))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield out, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_live_build_served_over_http_is_consistent(site):
    out, url = site
    result = subprocess.run([sys.executable, str(SCRIPT), "live", url], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert f"/pobierz/parlo-{METADATA['versionName']}.apk HTTP 200 sha256={METADATA['sha256']}" in result.stdout
    assert f"/pobierz/parlo.apk HTTP 200 sha256={METADATA['sha256']}" in result.stdout


@pytest.mark.parametrize("drift, reason", [
    ("missing_apk", "HTTP 404"),
    ("wrong_version", "metadata names"),
    ("bad_hash", "differs from metadata"),
    ("no_link", "found 0"),
    ("missing_alias", "parlo.apk: HTTP 404"),
    ("stale_alias", "parlo.apk: sha256="),
])
def test_live_drift_blocks_deployed(site, drift, reason):
    out, url = site
    apk = out / f"pobierz/parlo-{METADATA['versionName']}.apk"
    home = out / "index.html"
    if drift == "missing_apk":
        apk.unlink()
    elif drift == "wrong_version":
        home.write_text(home.read_text().replace(METADATA["versionName"], "0.2.0.6"))
    elif drift == "bad_hash":
        data = apk.read_bytes()
        apk.write_bytes(data[:-1] + bytes([data[-1] ^ 0xFF]))
    elif drift == "missing_alias":
        (out / "pobierz/parlo.apk").unlink()
    elif drift == "stale_alias":
        (out / "pobierz/parlo.apk").write_bytes((out / "pobierz/parlo-0.2.0.6.apk").read_bytes())
    else:
        home.write_text(home.read_text().replace("Parlo — aplikacja", "Parlo – aplikacja"))
    result = subprocess.run([sys.executable, str(SCRIPT), "live", url], text=True, capture_output=True)
    assert result.returncode == 1
    assert "NOT consistent" in result.stderr and reason in result.stderr
