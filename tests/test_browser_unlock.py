from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BROWSER_SUITE = ROOT / "tests/browser/privacy.test.cjs"
CHROME_CANDIDATES = (
    Path("/home/jan/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome"),
    Path("/usr/bin/chromium-browser"),
    Path("/usr/bin/google-chrome"),
    Path("/usr/bin/chromium"),
)


def chrome_available() -> bool:
    return any(path.is_file() or shutil.which(str(path)) for path in CHROME_CANDIDATES)


def node_available() -> str | None:
    node = shutil.which("node")
    if node is None:
        return None
    probe = subprocess.run([node, "-e", "require('crypto').webcrypto; WebSocket"], capture_output=True)
    return node if probe.returncode == 0 else None


@pytest.mark.skipif(not BROWSER_SUITE.is_file(), reason="browser suite missing")
def test_browser_unlock_flow():
    node = node_available()
    if node is None:
        pytest.skip("node with built-in WebSocket/WebCrypto unavailable")
    if not chrome_available():
        pytest.skip("headless chrome unavailable")

    result = subprocess.run(
        [node, str(BROWSER_SUITE)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, f"browser unlock flow failed:\n{result.stdout}\n{result.stderr}"
