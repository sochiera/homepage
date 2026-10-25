#!/usr/bin/env python3
"""Private topic proposal API. Payloads are encrypted before SQLite writes."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
import unicodedata
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response


MAX_BODY_BYTES = 4096
MAX_TOPIC_CHARS = 1200
MAX_ASSIGNEE_CHARS = 100
SESSION_TTL_SECONDS = 30 * 60
CSRF_TTL_SECONDS = 60 * 60
SESSION_COOKIE = "grupa-tematy-session"
CSRF_COOKIE = "grupa-tematy-csrf"
DEFAULT_DB_PATH = Path("/var/lib/sochiera/grupa-tematy/data.sqlite3")
DEFAULT_ORIGIN = "https://sochiera.pl"
AAD_PREFIX = b"sochiera/grupa-tematy/v1\x00"
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1


def record_aad(record_id: str) -> bytes:
    return AAD_PREFIX + record_id.encode("ascii")


def _b64(value: str, expected: int) -> bytes:
    decoded = base64.b64decode(value, validate=True)
    if len(decoded) != expected:
        raise ValueError("invalid credential material")
    return decoded


def load_credential(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("unsupported credential format")
    if (document.get("scrypt_n"), document.get("scrypt_r"), document.get("scrypt_p")) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
        raise ValueError("unsupported password verifier parameters")
    return {
        "data_key": _b64(document["data_key"], 32),
        "password_salt": _b64(document["password_salt"], 16),
        "password_verifier": _b64(document["password_verifier"], 32),
    }


def _credential_path(override: Path | None = None) -> Path:
    if override is not None:
        return override
    explicit = os.environ.get("GRUPA_TEMATY_CREDENTIAL_FILE")
    if explicit:
        return Path(explicit)
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if directory:
        return Path(directory) / "grupa-tematy"
    return Path("/run/credentials/grupa-tematy.service/grupa-tematy")


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(db_path: Path) -> None:
    db_path = Path(db_path)
    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(db_path.parent, 0o700)
    connection = _connect(db_path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                nonce BLOB NOT NULL CHECK(length(nonce) = 12),
                payload BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash BLOB PRIMARY KEY,
                csrf_hash BLOB NOT NULL,
                csrf_token TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
            """
        )
    finally:
        connection.close()
    os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _encrypt(key: bytes, record_id: str, payload: dict[str, Any]) -> tuple[bytes, bytes]:
    nonce = secrets.token_bytes(12)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return nonce, AESGCM(key).encrypt(nonce, encoded, record_aad(record_id))


def _decrypt(key: bytes, record_id: str, nonce: bytes, ciphertext: bytes) -> dict[str, Any]:
    decoded = AESGCM(key).decrypt(nonce, ciphertext, record_aad(record_id))
    payload = json.loads(decoded.decode("utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"topic", "assignee"}:
        raise ValueError("invalid encrypted record")
    return payload


class RateLimiter:
    def __init__(self):
        self.events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, bucket: str, identity: str, count: int, window: int) -> bool:
        now = time.monotonic()
        with self.lock:
            events = self.events[(bucket, identity)]
            while events and events[0] <= now - window:
                events.popleft()
            if len(events) >= count:
                return False
            events.append(now)
            return True


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH", "DELETE"}:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                if int(raw_length) > self.max_bytes:
                    response = JSONResponse({"detail": "Żądanie jest za duże."}, status_code=413, headers={"Cache-Control": "no-store"})
                    await response(scope, receive, send)
                    return
            except ValueError:
                response = JSONResponse({"detail": "Nieprawidłowe żądanie."}, status_code=400, headers={"Cache-Control": "no-store"})
                await response(scope, receive, send)
                return
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                response = JSONResponse({"detail": "Żądanie jest za duże."}, status_code=413, headers={"Cache-Control": "no-store"})
                await response(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay, send)


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {name.lower() for name, _ in headers}
                for name, value in (
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                ):
                    if name not in existing:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)
        await self.app(scope, receive, secure_send)


def _client_identity(request: Request) -> str:
    forwarded = request.headers.get("x-real-ip", "")
    try:
        return str(ipaddress.ip_address(forwarded))
    except ValueError:
        return request.client.host if request.client else "unknown"


async def _json_object(request: Request, expected: set[str]) -> dict[str, Any]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="Nieprawidłowe żądanie.")
    try:
        raw = await request.body()
        body = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Nieprawidłowe żądanie.") from None
    if not isinstance(body, dict) or set(body) != expected:
        raise HTTPException(status_code=400, detail="Nieprawidłowe żądanie.")
    return body


def _require_origin(request: Request, allowed_origin: str) -> None:
    if request.headers.get("origin") != allowed_origin:
        raise HTTPException(status_code=403, detail="Żądanie odrzucone.")


def _set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/api/grupa-tematy/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def _current_session(request: Request) -> sqlite3.Row | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token or not token.isascii() or len(token) > 128:
        return None
    digest = _digest(token)
    connection = _connect(request.app.state.db_path)
    try:
        row = connection.execute("SELECT token_hash, csrf_hash, csrf_token, expires_at FROM sessions WHERE token_hash = ?", (digest,)).fetchone()
        if not row:
            return None
        if row["expires_at"] <= time.time():
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (digest,))
            return None
        return row
    finally:
        connection.close()


def _require_session(request: Request) -> sqlite3.Row:
    session = _current_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Zaloguj się, aby kontynuować.")
    return session


def _require_csrf(request: Request, *, session: sqlite3.Row | None = None) -> None:
    header = request.headers.get("x-csrf-token", "")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if not header or not header.isascii() or len(header) > 128 or not cookie or not cookie.isascii() or not hmac.compare_digest(header, cookie):
        raise HTTPException(status_code=403, detail="Żądanie odrzucone.")
    digest = _digest(header)
    if session is not None and (
        session["expires_at"] <= time.time()
        or not hmac.compare_digest(session["csrf_hash"], digest)
        or not hmac.compare_digest(session["csrf_token"], header)
    ):
        raise HTTPException(status_code=403, detail="Żądanie odrzucone.")


def create_app(
    *,
    db_path: Path | None = None,
    credential_path: Path | None = None,
    allowed_origin: str | None = None,
) -> FastAPI:
    configured_db = Path(db_path or os.environ.get("GRUPA_TEMATY_DB_PATH", DEFAULT_DB_PATH))
    configured_credential = _credential_path(credential_path)
    origin = allowed_origin or os.environ.get("GRUPA_TEMATY_ALLOWED_ORIGIN", DEFAULT_ORIGIN)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db_path = configured_db
        app.state.credential = load_credential(configured_credential)
        app.state.allowed_origin = origin
        app.state.rate_limiter = RateLimiter()
        initialize_database(configured_db)
        yield

    app = FastAPI(title="Grupa tematy", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    @app.exception_handler(Exception)
    async def safe_internal_error(_: Request, __: Exception):
        return JSONResponse({"detail": "Wystąpił błąd serwera."}, status_code=500, headers={"Cache-Control": "no-store"})

    @app.get("/api/grupa-tematy/csrf")
    def issue_csrf(request: Request):
        now = time.time()
        session = _current_session(request)
        token = session["csrf_token"] if session else _new_token()
        max_age = max(1, int(session["expires_at"] - now)) if session else CSRF_TTL_SECONDS
        response = JSONResponse({"csrfToken": token} | ({"expiresIn": max_age} if session else {}))
        _set_cookie(response, CSRF_COOKIE, token, max_age)
        return response

    @app.post("/api/grupa-tematy/proposals", status_code=201)
    async def create_proposal(request: Request):
        _require_origin(request, request.app.state.allowed_origin)
        _require_csrf(request)
        identity = _client_identity(request)
        if not request.app.state.rate_limiter.allow("proposal", identity, 10, 3600):
            raise HTTPException(status_code=429, detail="Spróbuj ponownie później.")
        body = await _json_object(request, {"topic"})
        topic = body["topic"]
        if not isinstance(topic, str):
            raise HTTPException(status_code=422, detail="Wpisz propozycję w 2–3 zdaniach.")
        topic = topic.strip()
        try:
            topic.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise HTTPException(status_code=422, detail="Wpisz propozycję w 2–3 zdaniach.") from None
        if not topic or len(topic) > MAX_TOPIC_CHARS or any(
            unicodedata.category(char) == "Cc" and char not in "\n\r\t" for char in topic
        ):
            raise HTTPException(status_code=422, detail="Wpisz propozycję w 2–3 zdaniach.")
        sentence_count = len(re.findall(r"[.!?]+(?=\s|$)", topic))
        if sentence_count not in (2, 3):
            raise HTTPException(status_code=422, detail="Wpisz propozycję w 2–3 zdaniach.")
        record_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        nonce, ciphertext = _encrypt(request.app.state.credential["data_key"], record_id, {"topic": topic, "assignee": None})
        connection = _connect(request.app.state.db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO proposals(id, created_at, nonce, payload) VALUES (?, ?, ?, ?)",
                (record_id, created_at, nonce, ciphertext),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {"id": record_id, "message": "Dziękujemy za propozycję."}

    @app.post("/api/grupa-tematy/session")
    async def create_session(request: Request):
        _require_origin(request, request.app.state.allowed_origin)
        _require_csrf(request)
        identity = _client_identity(request)
        if not request.app.state.rate_limiter.allow("login", identity, 5, 900):
            raise HTTPException(status_code=429, detail="Spróbuj ponownie później.")
        body = await _json_object(request, {"password"})
        password = body["password"]
        if not isinstance(password, str):
            raise HTTPException(status_code=401, detail="Nieprawidłowe hasło.")
        try:
            encoded_password = password.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise HTTPException(status_code=401, detail="Nieprawidłowe hasło.") from None
        if len(encoded_password) > 1024:
            raise HTTPException(status_code=401, detail="Nieprawidłowe hasło.")
        credential = request.app.state.credential
        candidate = hashlib.scrypt(
            encoded_password, salt=credential["password_salt"], n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
            dklen=32, maxmem=64 * 1024 * 1024,
        )
        if not hmac.compare_digest(candidate, credential["password_verifier"]):
            raise HTTPException(status_code=401, detail="Nieprawidłowe hasło.")
        old_token = request.cookies.get(SESSION_COOKIE)
        session_token, csrf_token = _new_token(), _new_token()
        now = time.time()
        expires = now + SESSION_TTL_SECONDS
        connection = _connect(request.app.state.db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            if old_token:
                connection.execute("DELETE FROM sessions WHERE token_hash = ?", (_digest(old_token),))
            connection.execute(
                "INSERT INTO sessions(token_hash, csrf_hash, csrf_token, expires_at) VALUES (?, ?, ?, ?)",
                (_digest(session_token), _digest(csrf_token), csrf_token, expires),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        response = JSONResponse({"csrfToken": csrf_token, "expiresIn": SESSION_TTL_SECONDS})
        _set_cookie(response, SESSION_COOKIE, session_token, SESSION_TTL_SECONDS)
        _set_cookie(response, CSRF_COOKIE, csrf_token, SESSION_TTL_SECONDS)
        return response

    @app.get("/api/grupa-tematy/proposals")
    def list_proposals(request: Request):
        _require_session(request)
        connection = _connect(request.app.state.db_path)
        try:
            rows = connection.execute("SELECT id, created_at, nonce, payload FROM proposals ORDER BY created_at DESC, id DESC").fetchall()
        finally:
            connection.close()
        proposals = []
        for row in rows:
            payload = _decrypt(request.app.state.credential["data_key"], row["id"], row["nonce"], row["payload"])
            proposals.append({
                "id": row["id"],
                "createdAt": row["created_at"],
                "topic": payload["topic"],
                "assignee": payload["assignee"],
            })
        return {"proposals": proposals}

    @app.put("/api/grupa-tematy/proposals/{proposal_id}/assignment")
    async def update_assignment(proposal_id: str, request: Request):
        _require_origin(request, request.app.state.allowed_origin)
        session = _require_session(request)
        _require_csrf(request, session=session)
        try:
            uuid.UUID(proposal_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Nie znaleziono propozycji.") from None
        body = await _json_object(request, {"name"})
        name = body["name"]
        if name is not None:
            if not isinstance(name, str):
                raise HTTPException(status_code=422, detail="Wpisz imię i nazwisko lub usuń przypisanie.")
            name = name.strip()
            try:
                name.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                raise HTTPException(status_code=422, detail="Wpisz imię i nazwisko lub usuń przypisanie.") from None
            if not name or len(name) > MAX_ASSIGNEE_CHARS or any(unicodedata.category(char) == "Cc" for char in name):
                raise HTTPException(status_code=422, detail="Wpisz imię i nazwisko lub usuń przypisanie.")
        connection = _connect(request.app.state.db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT nonce, payload FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
            if not row:
                connection.rollback()
                raise HTTPException(status_code=404, detail="Nie znaleziono propozycji.")
            payload = _decrypt(request.app.state.credential["data_key"], proposal_id, row["nonce"], row["payload"])
            payload["assignee"] = name
            nonce, ciphertext = _encrypt(request.app.state.credential["data_key"], proposal_id, payload)
            connection.execute("UPDATE proposals SET nonce = ?, payload = ? WHERE id = ?", (nonce, ciphertext, proposal_id))
            connection.commit()
        except HTTPException:
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {"ok": True}

    @app.delete("/api/grupa-tematy/session", status_code=204)
    def delete_session(request: Request):
        _require_origin(request, request.app.state.allowed_origin)
        session = _require_session(request)
        _require_csrf(request, session=session)
        connection = _connect(request.app.state.db_path)
        try:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (session["token_hash"],))
        finally:
            connection.close()
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/api/grupa-tematy/", secure=True, httponly=True, samesite="strict")
        response.delete_cookie(CSRF_COOKIE, path="/api/grupa-tematy/", secure=True, httponly=True, samesite="strict")
        return response

    return app


app = create_app()
