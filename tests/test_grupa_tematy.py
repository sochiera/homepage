from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
API_PATH = ROOT / "scripts/grupa_tematy_api.py"
ADMIN_PATH = ROOT / "scripts/grupa_tematy_admin.py"
ORIGIN = "https://sochiera.pl"
PASSWORD = "test-only-grupa-tematy-9472"
TOPIC = "Chcę porozmawiać o dostępności cyfrowej. Podczas spotkania sprawdzimy kilka konkretnych przykładów."
ASSIGNEE = "Osoba testowa"
SESSION_COOKIE = "grupa-tematy-session"
CSRF_COOKIE = "grupa-tematy-csrf"
SALT = os.urandom(16)
N, R, P = 2**15, 8, 1


def load_api():
    spec = importlib.util.spec_from_file_location("grupa_tematy_api", API_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def credential_file(path: Path) -> Path:
    document = {
        "version": 1,
        "data_key": base64.b64encode(os.urandom(32)).decode("ascii"),
        "password_salt": base64.b64encode(SALT).decode("ascii"),
        "password_verifier": base64.b64encode(
            hashlib.scrypt(PASSWORD.encode(), salt=SALT, n=N, r=R, p=P, dklen=32, maxmem=64 * 1024 * 1024)
        ).decode("ascii"),
        "scrypt_n": N,
        "scrypt_r": R,
        "scrypt_p": P,
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture
def system(tmp_path: Path):
    module = load_api()
    database = tmp_path / "data.sqlite3"
    credential = credential_file(tmp_path / "credential.json")
    app = module.create_app(db_path=database, credential_path=credential, allowed_origin=ORIGIN)
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as client:
        yield module, database, credential, app, client


def csrf(client: TestClient) -> str:
    response = client.get("/api/grupa-tematy/csrf")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    token = response.json()["csrfToken"]
    assert client.cookies.get(CSRF_COOKIE)
    return token


def mutation_headers(token: str, origin: str = ORIGIN) -> dict[str, str]:
    return {"Origin": origin, "X-CSRF-Token": token}


def submit(client: TestClient, topic: str = TOPIC, token: str | None = None):
    token = token or csrf(client)
    return client.post(
        "/api/grupa-tematy/proposals",
        json={"topic": topic},
        headers=mutation_headers(token),
    )


def login(client: TestClient, password: str = PASSWORD, token: str | None = None):
    token = token or csrf(client)
    response = client.post(
        "/api/grupa-tematy/session",
        json={"password": password},
        headers=mutation_headers(token),
    )
    return response, token


def test_public_submission_is_encrypted_and_private_routes_are_gated(system, tmp_path: Path):
    module, database, _, _, client = system
    token = csrf(client)
    wal_keeper = sqlite3.connect(database, timeout=10)
    assert wal_keeper.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    created = submit(client, token=token)
    assert created.status_code == 201
    assert created.json().keys() == {"id", "message"}
    proposal_id = created.json()["id"]
    assert TOPIC not in created.text

    assert client.get("/api/grupa-tematy/proposals").status_code == 401
    missing_password = client.post(
        "/api/grupa-tematy/session", json={}, headers=mutation_headers(token)
    )
    assert missing_password.status_code == 400
    assert client.get("/api/grupa-tematy/proposals").status_code == 401
    assert client.put(
        f"/api/grupa-tematy/proposals/{proposal_id}/assignment",
        json={"name": ASSIGNEE},
        headers=mutation_headers("untrusted"),
    ).status_code == 401
    wrong, token = login(client, "wrong synthetic password")
    assert wrong.status_code == 401
    assert TOPIC not in wrong.text and ASSIGNEE not in wrong.text
    assert client.get("/api/grupa-tematy/proposals").status_code == 401

    accepted, token = login(client, token=token)
    assert accepted.status_code == 200
    cookie = accepted.headers["set-cookie"].lower()
    assert all(fragment in cookie for fragment in ("secure", "httponly", "samesite=strict", "path=/api/grupa-tematy/"))
    assert "max-age=" in cookie
    csrf_token = accepted.json()["csrfToken"]
    refreshed_csrf = client.get("/api/grupa-tematy/csrf").json()
    assert 0 < refreshed_csrf["expiresIn"] <= module.SESSION_TTL_SECONDS
    assert refreshed_csrf["csrfToken"] == csrf_token
    old_session_token = client.cookies.get(SESSION_COOKIE)
    assert old_session_token
    with sqlite3.connect(database) as connection:
        stored_session_hash = connection.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert stored_session_hash == hashlib.sha256(old_session_token.encode("ascii")).digest()
    assert old_session_token.encode("ascii") not in database.read_bytes()

    with TestClient(system[3], base_url=ORIGIN, raise_server_exceptions=False) as stale_client:
        stale_client.cookies.set(SESSION_COOKIE, old_session_token, path="/api/grupa-tematy/")
        stale_client.cookies.set(CSRF_COOKIE, csrf_token, path="/api/grupa-tematy/")
        assert stale_client.get("/api/grupa-tematy/proposals").status_code == 200
        rotated, _ = login(client, token=csrf_token)
        assert rotated.status_code == 200
        assert client.cookies.get(SESSION_COOKIE) != old_session_token
        assert rotated.json()["csrfToken"] != csrf_token
        assert stale_client.get("/api/grupa-tematy/proposals").status_code == 401
        csrf_token = rotated.json()["csrfToken"]

    listed = client.get("/api/grupa-tematy/proposals")
    assert listed.status_code == 200
    assert listed.json()["proposals"] == [
        {"id": proposal_id, "topic": TOPIC, "createdAt": listed.json()["proposals"][0]["createdAt"], "assignee": None}
    ]

    with sqlite3.connect(database) as connection:
        nonce_before, ciphertext_before = connection.execute(
            "SELECT nonce, payload FROM proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
    invalid_assignee = json.dumps({"name": "\ud800"}, ensure_ascii=True).encode("ascii")
    rejected_assignee = client.put(
        f"/api/grupa-tematy/proposals/{proposal_id}/assignment", content=invalid_assignee,
        headers=mutation_headers(csrf_token) | {"Content-Type": "application/json"},
    )
    assert rejected_assignee.status_code == 422
    response = client.put(
        f"/api/grupa-tematy/proposals/{proposal_id}/assignment",
        json={"name": ASSIGNEE},
        headers=mutation_headers(csrf_token),
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assigned = client.get("/api/grupa-tematy/proposals").json()["proposals"][0]
    assert assigned["topic"] == TOPIC and assigned["assignee"] == ASSIGNEE

    with sqlite3.connect(database) as connection:
        nonce_after, ciphertext_after = connection.execute(
            "SELECT nonce, payload FROM proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        backup = tmp_path / "backup.sqlite3"
        with sqlite3.connect(backup) as target:
            connection.backup(target)
    assert nonce_before != nonce_after
    assert ciphertext_before != ciphertext_after
    assert len(nonce_after) == 12
    assert TOPIC.encode() not in database.read_bytes()
    assert ASSIGNEE.encode() not in database.read_bytes()
    assert TOPIC.encode() not in backup.read_bytes()
    assert ASSIGNEE.encode() not in backup.read_bytes()
    wal = Path(str(database) + "-wal")
    assert wal.is_file()
    assert TOPIC.encode() not in wal.read_bytes()
    assert ASSIGNEE.encode() not in wal.read_bytes()
    wal_keeper.close()

    key = base64.b64decode(json.loads((tmp_path / "credential.json").read_text())["data_key"])
    aad = module.record_aad(proposal_id)
    plaintext = AESGCM(key).decrypt(nonce_after, ciphertext_after, aad)
    decoded = json.loads(plaintext)
    assert decoded == {"topic": TOPIC, "assignee": ASSIGNEE}
    with pytest.raises(InvalidTag):
        AESGCM(key).decrypt(nonce_after, ciphertext_after, module.record_aad(proposal_id + "x"))
    with pytest.raises(InvalidTag):
        AESGCM(key).decrypt(bytes([nonce_after[0] ^ 1]) + nonce_after[1:], ciphertext_after, aad)
    with pytest.raises(InvalidTag):
        AESGCM(key).decrypt(nonce_after, ciphertext_after[:-1] + bytes([ciphertext_after[-1] ^ 1]), aad)

    removed_assignment = client.put(
        f"/api/grupa-tematy/proposals/{proposal_id}/assignment",
        json={"name": None},
        headers=mutation_headers(csrf_token),
    )
    assert removed_assignment.status_code == 200
    assert client.get("/api/grupa-tematy/proposals").json()["proposals"][0]["assignee"] is None

    removed = client.delete("/api/grupa-tematy/session", headers=mutation_headers(csrf_token))
    assert removed.status_code == 204
    assert client.get("/api/grupa-tematy/proposals").status_code == 401


def test_origin_csrf_validation_sentence_limits_and_safe_plain_text(system):
    _, _, _, _, client = system
    token = csrf(client)
    assert submit(client, token=token).status_code == 201
    assert client.post(
        "/api/grupa-tematy/proposals", json={"topic": TOPIC}, headers={"X-CSRF-Token": token}
    ).status_code == 403
    assert client.post(
        "/api/grupa-tematy/proposals", json={"topic": TOPIC}, headers=mutation_headers(token, "https://evil.example")
    ).status_code == 403
    assert client.post(
        "/api/grupa-tematy/proposals", json={"topic": TOPIC}, headers=mutation_headers("wrong")
    ).status_code == 403
    assert client.post(
        "/api/grupa-tematy/proposals", content=b'{"topic":"invalid-utf8-\xff"}',
        headers=mutation_headers(token) | {"Content-Type": "application/json"},
    ).status_code == 400
    assert client.post(
        "/api/grupa-tematy/proposals", content=json.dumps({"topic": TOPIC}, ensure_ascii=False).encode("utf-16"),
        headers=mutation_headers(token) | {"Content-Type": "application/json"},
    ).status_code == 400
    surrogate_topic = json.dumps({"topic": "\ud800 dwa zdania. Drugie zdanie."}, ensure_ascii=True).encode("ascii")
    assert client.post(
        "/api/grupa-tematy/proposals", content=surrogate_topic,
        headers=mutation_headers(token) | {"Content-Type": "application/json"},
    ).status_code == 422
    assert client.post(
        "/api/grupa-tematy/session", content=json.dumps({"password": "\ud800"}, ensure_ascii=True).encode("ascii"),
        headers=mutation_headers(token) | {"Content-Type": "application/json"},
    ).status_code == 401
    assert submit(client, "Jedno zdanie tylko.", token).status_code == 422
    assert submit(client, "Jedno. Drugie. Trzecie. Czwarte.", token).status_code == 422


def test_session_ttl_revocation_and_request_limits(system):
    module, database, _, _, client = system
    token = csrf(client)
    for _ in range(5):
        response, token = login(client, "wrong synthetic password", token)
        assert response.status_code == 401
    blocked, _ = login(client, "wrong synthetic password", token)
    assert blocked.status_code == 429

    oversized = client.post(
        "/api/grupa-tematy/proposals",
        content=b"x" * (module.MAX_BODY_BYTES + 1),
        headers=mutation_headers(token) | {"Content-Type": "application/json"},
    )
    assert oversized.status_code == 413

    # A new app instance shares the same persistent store but an expired token is rejected.
    other = module.create_app(db_path=database, credential_path=system[2], allowed_origin=ORIGIN)
    with TestClient(other, base_url=ORIGIN, raise_server_exceptions=False) as second:
        fresh = csrf(second)
        accepted, _ = login(second, token=fresh)
        assert accepted.status_code == 200
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE sessions SET expires_at = 1")
        assert second.get("/api/grupa-tematy/proposals").status_code == 401


def test_public_submission_rate_limit_is_enforced(system):
    _, _, _, _, client = system
    token = csrf(client)
    for _ in range(10):
        assert submit(client, token=token).status_code == 201
    assert submit(client, token=token).status_code == 429


def test_admin_backup_copies_database_and_opaque_credential_without_plaintext(system, tmp_path: Path):
    _, database, _, _, client = system
    submit(client)
    opaque_credential = tmp_path / "credential.encrypted"
    opaque_credential.write_bytes(b"synthetic encrypted credential fixture")
    with tempfile.TemporaryDirectory(prefix="grupa-tematy-admin-backup-") as outside_repo:
        destination = Path(outside_repo) / "snapshot"
        result = subprocess.run(
            [sys.executable, str(ADMIN_PATH), "backup", "--database", str(database),
             "--credential", str(opaque_credential), "--output", str(destination)],
            cwd=ROOT, text=True, capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        assert (destination / "credential.encrypted").read_bytes() == opaque_credential.read_bytes()
        backup = destination / "data.sqlite3"
        assert TOPIC.encode() not in backup.read_bytes()
        assert destination.stat().st_mode & 0o777 == 0o700
        assert backup.stat().st_mode & 0o777 == 0o600
        assert result.stdout.strip() == "Zaszyfrowana kopia bazy i poświadczenia została utworzona."
    forbidden = ROOT / ".build/evidence/grupa-tematy/forbidden-test-backup"
    rejected = subprocess.run(
        [sys.executable, str(ADMIN_PATH), "backup", "--database", str(database),
         "--credential", str(opaque_credential), "--output", str(forbidden)],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert rejected.returncode != 0
    assert not forbidden.exists()


def test_concurrent_assignment_updates_do_not_lose_or_corrupt_records(system):
    _, _, _, app, client = system
    created = submit(client)
    token = login(client)[0].json()["csrfToken"]
    proposal_id = created.json()["id"]
    session_token = client.cookies.get(SESSION_COOKIE)
    csrf_cookie = client.cookies.get(CSRF_COOKIE)
    names = [f"Osoba testowa {i}" for i in range(8)]

    def assign(name: str) -> int:
        with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as parallel:
            parallel.cookies.set(SESSION_COOKIE, session_token, path="/api/grupa-tematy/")
            parallel.cookies.set(CSRF_COOKIE, csrf_cookie, path="/api/grupa-tematy/")
            return parallel.put(
                f"/api/grupa-tematy/proposals/{proposal_id}/assignment",
                json={"name": name},
                headers=mutation_headers(token),
            ).status_code

    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        statuses = list(pool.map(assign, names))
    assert statuses == [200] * len(names)
    final = client.get("/api/grupa-tematy/proposals").json()["proposals"][0]
    assert final["assignee"] in names
    assert final["topic"] == TOPIC


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class UvicornApi:
    def __init__(self, tmp_path: Path, database: Path, credential: Path):
        self.tmp_path = tmp_path
        self.database = database
        self.credential = credential
        self.port = _free_port()
        self.log_path = tmp_path / "uvicorn.log"
        self.log = None
        self.process = None

    def start(self):
        cert, key = self.tmp_path / "localhost.crt", self.tmp_path / "localhost.key"
        if not cert.exists():
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                 "-out", str(cert), "-days", "1", "-subj", "/CN=localhost",
                 "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                check=True, capture_output=True,
            )
        self.log = self.log_path.open("ab")
        env = os.environ | {
            "GRUPA_TEMATY_DB_PATH": str(self.database),
            "GRUPA_TEMATY_CREDENTIAL_FILE": str(self.credential),
            "GRUPA_TEMATY_ALLOWED_ORIGIN": ORIGIN,
            "PYTHONPATH": str(ROOT),
        }
        self.process = subprocess.Popen(
            [str(ROOT / ".venv/bin/uvicorn"), "scripts.grupa_tematy_api:app", "--host", "127.0.0.1",
             "--port", str(self.port), "--ssl-certfile", str(cert), "--ssl-keyfile", str(key),
             "--no-access-log", "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT,
        )
        context = ssl._create_unverified_context()
        url = f"https://127.0.0.1:{self.port}/api/grupa-tematy/csrf"
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError("uvicorn exited before readiness")
            try:
                with urllib.request.urlopen(url, context=context, timeout=1) as response:
                    if response.status == 200:
                        return self
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        raise AssertionError("uvicorn did not become ready")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.log:
            self.log.close()


def _client(port: int, jar: CookieJar):
    context = ssl._create_unverified_context()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), urllib.request.HTTPSHandler(context=context))

    def call(method: str, route: str, body: dict | None = None, headers: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"https://127.0.0.1:{port}{route}", data=data, method=method,
            headers={"Origin": ORIGIN, "Content-Type": "application/json"} | (headers or {}),
        )
        try:
            response = opener.open(request, timeout=4)
        except urllib.error.HTTPError as error:
            response = error
        return response.status, response.headers, response.read()

    return call


def test_real_process_restart_and_backup_restore_preserve_assignments(tmp_path: Path):
    module = load_api()
    database = tmp_path / "persistent.sqlite3"
    credential = credential_file(tmp_path / "credential.json")
    jar = CookieJar()
    server = UvicornApi(tmp_path, database, credential).start()
    try:
        call = _client(server.port, jar)
        _, _, csrf_body = call("GET", "/api/grupa-tematy/csrf")
        csrf_token = json.loads(csrf_body)["csrfToken"]
        code, _, body = call("POST", "/api/grupa-tematy/proposals", {"topic": TOPIC}, {"X-CSRF-Token": csrf_token})
        assert code == 201
        proposal_id = json.loads(body)["id"]
        code, _, body = call("POST", "/api/grupa-tematy/session", {"password": PASSWORD}, {"X-CSRF-Token": csrf_token})
        assert code == 200
        csrf_token = json.loads(body)["csrfToken"]
        code, _, _ = call(
            "PUT", f"/api/grupa-tematy/proposals/{proposal_id}/assignment", {"name": ASSIGNEE},
            {"X-CSRF-Token": csrf_token},
        )
        assert code == 200
        with sqlite3.connect(database) as connection, sqlite3.connect(tmp_path / "backup.sqlite3") as backup:
            connection.backup(backup)
    finally:
        server.stop()

    server = UvicornApi(tmp_path, database, credential).start()
    try:
        call = _client(server.port, jar)
        code, _, body = call("GET", "/api/grupa-tematy/proposals")
        assert code == 200
        assert json.loads(body)["proposals"][0]["assignee"] == ASSIGNEE
    finally:
        server.stop()

    restored = tmp_path / "restored.sqlite3"
    shutil.copy2(tmp_path / "backup.sqlite3", restored)
    restored_server = UvicornApi(tmp_path, restored, credential).start()
    try:
        call = _client(restored_server.port, CookieJar())
        _, _, csrf_body = call("GET", "/api/grupa-tematy/csrf")
        csrf_token = json.loads(csrf_body)["csrfToken"]
        code, _, login_body = call("POST", "/api/grupa-tematy/session", {"password": PASSWORD}, {"X-CSRF-Token": csrf_token})
        assert code == 200
        csrf_token = json.loads(login_body)["csrfToken"]
        code, _, body = call("GET", "/api/grupa-tematy/proposals")
        assert code == 200
        assert json.loads(body)["proposals"][0]["assignee"] == ASSIGNEE
    finally:
        restored_server.stop()

    log = (tmp_path / "uvicorn.log").read_bytes()
    assert PASSWORD.encode() not in log
    assert TOPIC.encode() not in log
    assert ASSIGNEE.encode() not in log
    assert ADMIN_PATH.is_file()


def test_deployment_templates_pin_loopback_and_encrypted_credentials():
    service = (ROOT / "deploy/grupa-tematy.service").read_text(encoding="utf-8")
    nginx = (ROOT / "deploy/grupa-tematy.nginx.conf").read_text(encoding="utf-8")
    assert "--host 127.0.0.1 --port 8793" in service
    assert "LoadCredentialEncrypted=grupa-tematy:/etc/credstore.encrypted/grupa-tematy" in service
    assert "\nLoadCredential=" not in service
    assert "ReadWritePaths=/var/lib/sochiera/grupa-tematy" in service
    assert "ProtectSystem=strict" in service and "NoNewPrivileges=yes" in service and "UMask=0077" in service
    assert "location ^~ /grupa-tematy/" in nginx
    assert "location ^~ /api/grupa-tematy/" in nginx
    assert "client_max_body_size 4k;" in nginx
    assert "proxy_pass http://127.0.0.1:8793;" in nginx
    assert "proxy_set_header X-Real-IP $remote_addr;" in nginx
