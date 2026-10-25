#!/usr/bin/env python3
"""Private setup and encrypted database snapshot helpers for the topic API."""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import pwd
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from grupa_tematy_api import DEFAULT_DB_PATH, SCRYPT_N, SCRYPT_P, SCRYPT_R, initialize_database


DEFAULT_CREDENTIAL = Path("/etc/credstore.encrypted/grupa-tematy")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_ROOTS = (
    REPOSITORY_ROOT,
    Path("/var/www/sochiera"),
    Path("/var/www/sochiera-releases"),
    Path("/opt/sochiera/grupa-tematy"),
)


def _require_private_path(path: Path, label: str) -> None:
    resolved = path.resolve(strict=False)
    if any(resolved == root or resolved.is_relative_to(root) for root in FORBIDDEN_ROOTS):
        raise RuntimeError(f"{label} must remain outside the repository and public release trees")


def _credential_document(password: str) -> bytes:
    salt = secrets.token_bytes(16)
    verifier = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        dklen=32, maxmem=64 * 1024 * 1024,
    )
    document = {
        "version": 1,
        "data_key": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
        "password_salt": base64.b64encode(salt).decode("ascii"),
        "password_verifier": base64.b64encode(verifier).decode("ascii"),
        "scrypt_n": SCRYPT_N,
        "scrypt_r": SCRYPT_R,
        "scrypt_p": SCRYPT_P,
    }
    return (json.dumps(document, separators=(",", ":")) + "\n").encode("utf-8")


def initialize(database: Path, encrypted_credential: Path) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("initial setup must run as root so the credential stays outside the release")
    try:
        service_user = pwd.getpwnam("grupa-tematy")
    except KeyError:
        raise RuntimeError("create the grupa-tematy service user and group before initialization") from None
    _require_private_path(database, "database")
    _require_private_path(encrypted_credential, "credential")
    if database.exists() or encrypted_credential.exists():
        raise RuntimeError("database or encrypted credential already exists; refusing to replace either")
    first = getpass.getpass("Ustaw wspólne hasło: ")
    second = getpass.getpass("Powtórz wspólne hasło: ")
    if not secrets.compare_digest(first, second):
        raise RuntimeError("hasła nie są zgodne")
    if len(first) < 15:
        raise RuntimeError("hasło musi mieć co najmniej 15 znaków")

    encrypted_credential.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_fd, source_name = tempfile.mkstemp(prefix=".grupa-tematy-credential-", dir=encrypted_credential.parent)
    encrypted_fd, encrypted_name = tempfile.mkstemp(prefix=".grupa-tematy-encrypted-", dir=encrypted_credential.parent)
    os.close(encrypted_fd)
    source = Path(source_name)
    encrypted_tmp = Path(encrypted_name)
    encrypted_tmp.unlink()
    try:
        os.fchmod(source_fd, 0o600)
        with os.fdopen(source_fd, "wb") as handle:
            handle.write(_credential_document(first))
            handle.flush()
            os.fsync(handle.fileno())
        result = subprocess.run(
            ["systemd-creds", "encrypt", "--name=grupa-tematy", "--with-key=host", str(source), str(encrypted_tmp)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode:
            raise RuntimeError("systemd-creds encryption failed; check host credential support")
        os.chmod(encrypted_tmp, 0o600)
        initialize_database(database)
        os.chown(database.parent, service_user.pw_uid, service_user.pw_gid)
        os.chmod(database.parent, 0o700)
        os.chown(database, service_user.pw_uid, service_user.pw_gid)
        os.chmod(database, 0o600)
        os.replace(encrypted_tmp, encrypted_credential)
        os.chown(encrypted_credential, 0, 0)
        os.chmod(encrypted_credential, 0o600)
        dir_fd = os.open(encrypted_credential.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        source.unlink(missing_ok=True)
        encrypted_tmp.unlink(missing_ok=True)
    print("Baza zainicjalizowana; poświadczenie zaszyfrowano kluczem hosta systemd.")


def backup(database: Path, encrypted_credential: Path, destination: Path) -> None:
    _require_private_path(destination, "backup")
    if not database.is_file() or not encrypted_credential.is_file():
        raise RuntimeError("baza lub zaszyfrowane poświadczenie nie istnieje")
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(destination, 0o700)
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=10) as source:
            with sqlite3.connect(destination / "data.sqlite3") as target:
                source.backup(target)
        shutil.copyfile(encrypted_credential, destination / "credential.encrypted")
        os.chmod(destination / "data.sqlite3", 0o600)
        os.chmod(destination / "credential.encrypted", 0o600)
        for path in (destination / "data.sqlite3", destination / "credential.encrypted"):
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        dir_fd = os.open(destination, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    print("Zaszyfrowana kopia bazy i poświadczenia została utworzona.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Administracja trwałymi danymi Grupy tematy")
    commands = parser.add_subparsers(dest="command", required=True)
    init_parser = commands.add_parser("init", help="utwórz bazę i zaszyfrowane poświadczenie systemd")
    init_parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    init_parser.add_argument("--credential", type=Path, default=DEFAULT_CREDENTIAL)
    backup_parser = commands.add_parser("backup", help="utwórz spójny snapshot bazy i poświadczenia")
    backup_parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    backup_parser.add_argument("--credential", type=Path, default=DEFAULT_CREDENTIAL)
    backup_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "init":
            initialize(args.database, args.credential)
        else:
            backup(args.database, args.credential, args.output)
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        parser.exit(1, f"błąd administracyjny: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
