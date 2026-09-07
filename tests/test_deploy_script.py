from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy.sh"


def test_deploy_script_is_valid_and_alias_is_literal():
    assert subprocess.run(["/bin/bash", "-n", SCRIPT]).returncode == 0
    text = SCRIPT.read_text()
    assert "old-vps" in text and "/var/www/sochiera" in text
    assert "rsync --delete" not in text


def test_rejects_arbitrary_host_before_external_commands(tmp_path: Path):
    env = os.environ | {"PATH": str(tmp_path)}
    result = subprocess.run(["/bin/bash", SCRIPT, "--host", "user@example.invalid", "--yes"], cwd=ROOT, env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert "old-vps" in result.stderr


def test_requires_confirmation_before_preflight():
    result = subprocess.run(["/bin/bash", SCRIPT, "--host", "old-vps"], cwd=ROOT, input="no\n", text=True, capture_output=True)
    assert result.returncode != 0
    assert "confirmation" in result.stderr.lower()


def test_script_contains_ordered_safety_gates_and_rollback():
    text = SCRIPT.read_text()
    ordered = ["check_clean_manifest", "check_dns", "check_remote_config", "upload_release", "backup_current", "switch_release", "verify_release"]
    positions = [text.rfind(name) for name in ordered]
    assert positions == sorted(positions) and min(positions) >= 0
    assert "restore_backup" in text and "flock" in text
    assert "malowanie-po-numerach" in text and "/api/pbn-" in text
    assert "--rollback" in text and "RELEASE_RE=" in text
