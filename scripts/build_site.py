#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import html as html_mod
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import markdown
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://sochiera.pl"
PROTECTED_AAD = b"sochiera/blog-v1"
PROTECTED_ITERATIONS = 600_000
PROTECTED_SALT_BYTES = 16
PROTECTED_NONCE_BYTES = 12
PROTECTED_SCHEMA = 1
EXPECTED_SOURCES = {"Dobry_Ojciec/opowiadanie.md", "Kartka/kartka.md", "Dobry_Ojciec/der_gute_vater.md"}
EXPECTED_MICROBLOG_SOURCE = "Mikroblog_2026/mikroblog_2026.md"
ALLOWED_HTML = [
    re.compile(r'^<h3 align="center">([IVX]+)</h3>$'),
    re.compile(r'^<h4 align="center">(Natan|Joram)</h4>$'),
    re.compile(r'^<p align="center">\*  \*  \*</p>$'),
]

def fail(message: str) -> None:
    raise ValueError(message)

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def output_path(url: str) -> str:
    if not url.startswith("/") or not url.endswith("/") or ".." in url:
        fail(f"unsafe output URL: {url}")
    return (url.strip("/") + "/index.html") if url != "/" else "index.html"

def resolve_source(writing_root: Path, relative_source: str) -> Path:
    source = writing_root / relative_source
    if source.is_symlink() or not source.is_file():
        fail(f"missing or linked source: {relative_source}")
    relative = Path(relative_source)
    current = writing_root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            fail(f"linked source component: {relative_source}")
    resolved = source.resolve()
    if not resolved.is_relative_to(writing_root):
        fail("source escapes writing root")
    return resolved


def load_stories(writing_root: Path) -> list[dict]:
    data = tomllib.loads((ROOT / "content/stories.toml").read_text(encoding="utf-8"))
    stories = data.get("stories", [])
    if {s.get("source") for s in stories} != EXPECTED_SOURCES or len(stories) != 3:
        fail("story allowlist must contain exactly the three approved sources")
    urls, slugs = set(), set()
    for story in stories:
        if story["language"] not in {"pl", "de"} or story["profile"] not in {"chaptered", "standard"}:
            fail("unexpected language or render profile")
        if story["url"] in urls or (story["language"], story["slug"]) in slugs:
            fail("duplicate output URL or slug")
        urls.add(story["url"]); slugs.add((story["language"], story["slug"]))
        story["path"] = resolve_source(writing_root, story["source"])
    return stories


def load_microblog(writing_root: Path) -> dict:
    data = tomllib.loads((ROOT / "content/microblog.toml").read_text(encoding="utf-8"))
    microblog = data.get("microblog", {})
    if microblog.get("source") != EXPECTED_MICROBLOG_SOURCE:
        fail("unexpected microblog source")
    if microblog.get("url") != "/mikroblog/" or microblog.get("profile") != "standard" or microblog.get("language") != "pl":
        fail("unexpected microblog URL, language, or render profile")
    microblog["path"] = resolve_source(writing_root, microblog["source"])
    return microblog

def load_protected(path: Path | None) -> dict:
    if path is None or not path.is_file(): return {"entries": []}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != PROTECTED_SCHEMA or data.get("kdf") != "pbkdf2-sha256" or data.get("iterations") != PROTECTED_ITERATIONS:
        fail(f"unsupported protected-file schema in {path}")
    entries = data.get("entries", [])
    if not isinstance(entries, list): fail("protected entries must be a list")
    headings = set()
    for entry in entries:
        if entry.get("source") != EXPECTED_MICROBLOG_SOURCE: fail("protected entries support only the microblog source")
        heading = entry.get("heading", "").strip()
        if not heading: fail("protected entry needs a heading")
        if heading in headings: fail(f"duplicate protected heading: {heading}")
        headings.add(heading)
    return {"entries": entries}

def read_password(path: Path) -> bytes:
    try:
        if not path.is_file(): fail(f"password file not found: {path}")
        if path.is_symlink(): fail("password file must not be a symlink")
        if stat.S_IMODE(path.stat().st_mode) & 0o077: fail("password file permissions are too open (need 0600)")
        password = path.read_bytes().strip()
        if not password: fail("password file is empty")
        return password
    except ValueError as exc: raise ValueError(str(exc)) from exc
    except (OSError, PermissionError) as exc: fail(f"unreadable password file: {exc}")

def decrypt_proof(query: bytes, salt: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PROTECTED_ITERATIONS)
    return AESGCM(kdf.derive(query)).decrypt(nonce, ciphertext, aad)

def protect_fragment(fragment: str, password: bytes) -> str:
    salt = os.urandom(PROTECTED_SALT_BYTES)
    nonce = os.urandom(PROTECTED_NONCE_BYTES)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PROTECTED_ITERATIONS)
    ciphertext = AESGCM(kdf.derive(password)).encrypt(nonce, fragment.encode("utf-8"), PROTECTED_AAD)
    payload = {"v": PROTECTED_SCHEMA, "i": PROTECTED_ITERATIONS,
               "s": base64.b64encode(salt).decode("ascii"),
               "n": base64.b64encode(nonce).decode("ascii"),
               "c": base64.b64encode(ciphertext).decode("ascii")}
    return json.dumps(payload, separators=(",", ":"))

def split_microblog(text: str) -> list[dict]:
    if not text or not text.startswith("# Mikroblog"): fail("microblog must start with the Mikroblog H1")
    entries, current, preamble = [], None, []
    for line in text.splitlines()[1:]:
        match = re.match(r"## (.+)$", line)
        if match:
            current = {"heading": match.group(1).strip(), "lines": []}
            entries.append(current)
        elif current is not None:
            current["lines"].append(line)
        elif line.strip(): preamble.append(line)
    if preamble: fail("unexpected preamble between microblog H1 and first entry")
    return entries

def render_manuscript(story: dict) -> str:
    try: text = story["path"].read_text(encoding="utf-8")
    except UnicodeDecodeError as exc: raise ValueError("source is not UTF-8") from exc
    if story["language"] == "de": text = re.sub(r"\A> Deutsche Übersetzung[^\n]*\n+", "", text)
    expected = f"# {story['expected_h1']}"
    lines = text.splitlines()
    if not lines or lines[0] != expected: fail(f"wrong H1 for {story['source']}")
    lines = lines[1:]
    normalized = []
    for line in lines:
        if "<" in line:
            match = next((pattern.fullmatch(line) for pattern in ALLOWED_HTML if pattern.fullmatch(line)), None)
            if not match: fail(f"unapproved raw HTML in {story['source']}")
            if line.startswith("<h3"): normalized.append(f'<h2 class="chapter">{match.group(1)}</h2>')
            elif line.startswith("<h4"): normalized.append(f'<h3 class="chapter-name">{match.group(1)}</h3>')
            else: normalized.append('<p class="sep">* &nbsp; * &nbsp; *</p>')
        elif story["profile"] == "chaptered" and line.strip() == "---":
            continue
        else: normalized.append(line)
    fragment = markdown.markdown("\n".join(normalized), extensions=["sane_lists"], output_format="html")
    return fragment.replace("<hr />", "<hr>")

def render_microblog_entry(entry: dict) -> str:
    if any("<" in line for line in entry["lines"]): fail("unapproved raw HTML in microblog")
    fragment = markdown.markdown("\n".join(entry["lines"]), extensions=["sane_lists"], output_format="html")
    return fragment.replace("<hr />", "<hr>")

def render_site(writing_root: Path, staging: Path, protected: dict, protected_password: bytes | None) -> None:
    stories = load_stories(writing_root)
    microblog = load_microblog(writing_root)
    env = Environment(loader=FileSystemLoader(ROOT / "layouts"), autoescape=True, undefined=StrictUndefined, keep_trailing_newline=True)
    common = dict(alternates=[])
    def write(rel: str, template: str, **context) -> None:
        target = staging / rel; target.parent.mkdir(parents=True, exist_ok=True)
        values = common | context
        target.write_text(env.get_template(template).render(**values), encoding="utf-8")
    write("index.html", "home.html", lang="pl", title="Jan Sochiera — strona główna", og_title="Jan Sochiera — strona główna", description="Strona główna Jana Sochiery.", canonical=BASE_URL + "/")
    write(output_path("/o-mnie/"), "about.html", lang="pl", title="O mnie — Jan Sochiera", og_title="O mnie — Jan Sochiera", description="Jan Sochiera — inżynier oprogramowania, kariera, projekty i twórczość literacka.", canonical=BASE_URL + "/o-mnie/")
    write(output_path("/biblioteka/"), "library.html", lang="pl", title="Biblioteka — Jan Sochiera", og_title="Biblioteka — Jan Sochiera", description="Ukryta wyszukiwarka książek.", canonical=BASE_URL + "/biblioteka/", robots="noindex,nofollow")
    protected_headings = {entry["heading"] for entry in protected["entries"]}
    entries = []
    for entry in split_microblog(microblog["path"].read_text(encoding="utf-8")):
        slug = re.sub(r"[^a-z0-9]+", "-", entry["heading"].lower()).strip("-")
        if entry["heading"] in protected_headings:
            if protected_password is None: fail("password file required for protected microblog entries")
            entries.append({"heading": entry["heading"], "slug": slug, "protected": True, "payload": protect_fragment(render_microblog_entry(entry), protected_password)})
        else:
            entries.append({"heading": entry["heading"], "slug": slug, "protected": False, "body": Markup(render_microblog_entry(entry))})
    missing = protected_headings - {entry["heading"] for entry in entries}
    if missing: fail(f"protected entries missing from {microblog['source']}: {sorted(missing)}")
    microblog["entries"] = entries
    write(output_path(microblog["url"]), "microblog.html", lang="pl", microblog=microblog, title=f'{microblog["title"]} — Jan Sochiera', og_title=microblog["title"], description=microblog["description"], canonical=BASE_URL + microblog["url"])
    for lang, url, heading in (("pl", "/opowiadania/", "Opowiadania"), ("de", "/de/opowiadania/", "Erzählungen")):
        listed = [s for s in stories if s["language"] == lang]
        write(output_path(url), "stories-index.html", lang=lang, stories=listed, title=f"{heading} — Jan Sochiera", og_title=heading, description=("Opowiadania Jana Sochiery." if lang == "pl" else "Erzählungen von Jan Sochiera."), canonical=BASE_URL + url)
    by_slug = {s["slug"]: s for s in stories}
    for story in stories:
        translation = by_slug.get(story.get("translation"))
        alternates = []
        if translation:
            pair = sorted((story, translation), key=lambda s: s["language"])
            alternates = [{"lang": s["language"], "url": BASE_URL + s["url"]} for s in pair]
        write(output_path(story["url"]), "story.html", lang=story["language"], story=story, translation=translation, body=Markup(render_manuscript(story)), title=f"{story['title']} — Jan Sochiera", og_title=story["title"], description=story["description"], canonical=BASE_URL + story["url"], alternates=alternates)
    shutil.copy2(ROOT / "static/styles.css", staging / "styles.css")
    shutil.copy2(ROOT / "static/favicon.svg", staging / "favicon.svg")
    (staging / "js").mkdir()
    shutil.copy2(ROOT / "static/js/privacy.js", staging / "js/privacy.js")
    validate_output(staging)
    artifacts = [{"path": p.relative_to(staging).as_posix(), "sha256": sha(p)} for p in sorted(staging.rglob("*")) if p.is_file()]
    sources = [{"path": s["source"], "sha256": sha(s["path"])} for s in sorted([*stories, microblog], key=lambda x: x["source"])]
    manifest = {"schema_version": 1, "tool_version": "1.0.0", "artifacts": artifacts, "sources": sources}
    (staging / "build-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

def validate_output(staging: Path) -> None:
    expected = {"index.html", "o-mnie/index.html", "biblioteka/index.html", "mikroblog/index.html", "opowiadania/index.html", "opowiadania/dobry-ojciec/index.html", "opowiadania/kartka/index.html", "de/opowiadania/index.html", "de/opowiadania/der-gute-vater/index.html", "styles.css", "favicon.svg", "js/privacy.js"}
    actual = {p.relative_to(staging).as_posix() for p in staging.rglob("*") if p.is_file()}
    if actual != expected or any(p.is_symlink() for p in staging.rglob("*")): fail("unexpected publish tree")
    for page in staging.rglob("*.html"):
        rel = page.relative_to(staging).as_posix()
        text = page.read_text(encoding="utf-8")
        has_script = "<script" in text.lower()
        if rel == "mikroblog/index.html":
            if has_script and text.count('<script defer src="/js/privacy.js"></script>') != 1:
                fail("microblog must load exactly the site decryptor script")
            for match in re.finditer(r'section class="locked-entry[^"]*" data-protected="([^"]*)"', text):
                payload = json.loads(html_mod.unescape(match.group(1)))
                if set(payload) != {"v", "i", "s", "n", "c"} or payload["v"] != PROTECTED_SCHEMA or payload["i"] != PROTECTED_ITERATIONS:
                    fail("unsafe protected payload")
                for key in ("s", "n", "c"):
                    base64.b64decode(payload[key], validate=True)
        elif has_script:
            fail("scripts are forbidden")
        for link in re.findall(r'(?:href|src)="([^"]+)"', text):
            parsed = urlsplit(link)
            if parsed.scheme in {"http", "https"}: continue
            if not link.startswith("/"): fail(f"non-root-relative link: {link}")
            if link.startswith(("/malowanie-po-numerach/", "/poker/")): continue
            target = staging / (link.lstrip("/") + ("index.html" if link.endswith("/") else ""))
            if not target.is_file(): fail(f"broken internal link: {link}")

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--writing-root", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / ".build/site")
    parser.add_argument("--protected-file", type=Path, help="TOML declaring protected microblog entries (default: none)")
    parser.add_argument("--password-file", type=Path)
    args = parser.parse_args()
    writing_root = args.writing_root.resolve()
    output = args.output.absolute()
    if not writing_root.is_dir(): fail("writing root does not exist")
    if output.is_relative_to(ROOT) and not output.is_relative_to(ROOT / ".build"): fail("output inside tracked source tree is forbidden")
    output.parent.mkdir(parents=True, exist_ok=True)
    protected = load_protected(args.protected_file.resolve() if args.protected_file else None)
    protected_password = read_password(args.password_file.resolve()) if args.password_file else None
    if protected["entries"] and protected_password is None:
        fail("password file is required when protected entries exist (use --password-file)")
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        render_site(writing_root, staging, protected, protected_password)
        old = output.with_name(output.name + ".previous")
        if old.exists(): shutil.rmtree(old)
        if output.exists(): os.replace(output, old)
        try:
            os.replace(staging, output)
        except Exception:
            if old.exists() and not output.exists(): os.replace(old, output)
            raise
        if old.exists(): shutil.rmtree(old)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(f"Built {output}")
    return 0

if __name__ == "__main__":
    try: raise SystemExit(main())
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"build failed: {exc}", file=sys.stderr); raise SystemExit(1)
