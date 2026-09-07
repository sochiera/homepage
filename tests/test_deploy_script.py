from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy.sh"


def test_deploy_script_is_valid_and_uses_verified_explicit_target():
    assert subprocess.run(["/bin/bash", "-n", SCRIPT]).returncode == 0
    text = SCRIPT.read_text()
    assert 'EXPECTED_HOST="ubuntu@51.83.199.206"' in text
    assert 'IDENTITY_FILE="${HOME}/.ssh/pbn_vps"' in text
    assert 'ssh -i "$IDENTITY_FILE" -o IdentitiesOnly=yes "$EXPECTED_HOST"' in text
    assert 'scp -i "$IDENTITY_FILE" -o IdentitiesOnly=yes' in text
    assert "sudo -n nginx -T" in text
    assert "old-vps" not in text and "/var/www/sochiera" in text
    assert "rsync --delete" not in text


def test_rejects_arbitrary_user_or_host_before_external_commands(tmp_path: Path):
    env = os.environ | {"PATH": str(tmp_path)}
    for target in ("user@example.invalid", "root@51.83.199.206", "ubuntu@203.0.113.1", "old-vps"):
        result = subprocess.run(["/bin/bash", SCRIPT, "--host", target, "--yes"], cwd=ROOT, env=env, text=True, capture_output=True)
        assert result.returncode != 0
        assert "ubuntu@51.83.199.206" in result.stderr


def test_requires_confirmation_before_preflight():
    result = subprocess.run(["/bin/bash", SCRIPT, "--host", "ubuntu@51.83.199.206"], cwd=ROOT, input="no\n", text=True, capture_output=True)
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
