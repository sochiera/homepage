from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts/build_site.py"


def manuscript(title: str, chaptered: bool = False) -> str:
    if not chaptered:
        return f"# {title}\n\n**ważne**\n\n---\n\n*finał*\n"
    chunks = [f"# {title}"]
    romans = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII", "XIV", "XV", "XVI")
    for index, roman in enumerate(romans):
        chunks += [f'<h3 align="center">{roman}</h3>', f'<h4 align="center">{"Natan" if index % 2 == 0 else "Joram"}</h4>', "Tekst *kursywą*.", '<p align="center">*  *  *</p>', "---"]
    return "\n\n".join(chunks) + "\n"


PROTECTED_MARKING = "Poufny-pies-99"

@pytest.fixture
def writing(tmp_path: Path) -> Path:
    root = tmp_path / "writing"
    (root / "Dobry_Ojciec").mkdir(parents=True)
    (root / "Kartka").mkdir()
    (root / "Mikroblog_2026").mkdir()
    (root / "Dobry_Ojciec/opowiadanie.md").write_text(manuscript("Dobry Ojciec", True), encoding="utf-8")
    (root / "Dobry_Ojciec/der_gute_vater.md").write_text("> Deutsche Übersetzung.\n\n" + manuscript("Der gute Vater", True), encoding="utf-8")
    (root / "Kartka/kartka.md").write_text(manuscript("Kartka"), encoding="utf-8")
    (root / "Mikroblog_2026/mikroblog_2026.md").write_text(
        "# Mikroblog 2026\n\n## 23 września 2026\n\n> Cytat.\n\nPierwszy wpis.\n",
        encoding="utf-8",
    )
    return root

@pytest.fixture
def locked(writing: Path, tmp_path: Path) -> dict:
    source = writing / "Mikroblog_2026/mikroblog_2026.md"
    source.write_text(source.read_text(encoding="utf-8") + f"\n## 24 września 2026\n\nTajny akapit {PROTECTED_MARKING}.\n\n> Blok *poufny*.\n", encoding="utf-8")
    protected_file = tmp_path / "protected.toml"
    protected_file.write_text(
        'schema_version = 1\nkdf = "pbkdf2-sha256"\niterations = 600000\n\n[[entries]]\nsource = "Mikroblog_2026/mikroblog_2026.md"\nheading = "24 września 2026"\n',
        encoding="utf-8",
    )
    password_file = tmp_path / "secrets.password"
    password_file.write_text("bardzo-dobre-haslo-", encoding="utf-8")
    password_file.chmod(0o600)
    return {"writing": writing, "protected": protected_file, "password": password_file}


def build(writing: Path, output: Path, *, cwd: Path = ROOT, protected: Path | None = None, password: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(BUILDER), "--writing-root", str(writing), "--output", str(output)]
    if protected: command += ["--protected-file", str(protected)]
    if password: command += ["--password-file", str(password)]
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True)


class Anchors(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []
        self.href = None
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.text = []

    def handle_data(self, data):
        if self.href is not None:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href is not None:
            self.items.append((self.href, "".join(self.text).strip()))
            self.href = None


def test_build_outputs_exact_routes_and_metadata(writing: Path, tmp_path: Path):
    out = tmp_path / "site"
    result = build(writing, out)
    assert result.returncode == 0, result.stderr
    expected = {
        "index.html", "opowiadania/index.html", "opowiadania/dobry-ojciec/index.html",
        "opowiadania/kartka/index.html", "mikroblog/index.html", "de/opowiadania/index.html",
        "de/opowiadania/der-gute-vater/index.html", "styles.css", "favicon.svg",
        "o-mnie/index.html", "biblioteka/index.html",
        "build-manifest.json", "js/privacy.js",
    }
    assert {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()} == expected
    home = (out / "index.html").read_text()
    anchors = Anchors(); anchors.feed(home)
    assert anchors.items == [
        ("/o-mnie/", "O mnie"),
        ("/opowiadania/", "Opowiadania"),
        ("/mikroblog/", "Mikroblog"),
        ("https://malowanie.sochiera.pl/", "Generator malowania po numerach"),
        ("/poker/", "Poker"),
    ]
    assert 'href="/malowanie-po-numerach/"' not in home
    assert "/biblioteka/" not in home
    library = (out / "biblioteka/index.html").read_text()
    assert 'name="robots" content="noindex,nofollow"' in library
    assert 'action="/biblioteka/"' in library and "<script" not in library.lower()
    assert 'name="robots"' not in home
    about = (out / "o-mnie/index.html").read_text()
    assert 'name="robots"' not in about
    assert all(value in about for value in (
        "Sii Poland", "Nokia", "Agent Loop", "github.com/sochiera", "linkedin.com",
        "/opowiadania/", "/malowanie-po-numerach/",
    ))
    stories_index = (out / "opowiadania/index.html").read_text()
    assert "Dobry Ojciec" in stories_index and "Kartka" in stories_index
    assert "Mikroblog" not in stories_index
    microblog = (out / "mikroblog/index.html").read_text()
    assert all(value in microblog for value in (
        "Mikroblog 2026", "23 września 2026", "<blockquote>", "Cytat.", "Pierwszy wpis.",
    ))
    assert 'href="/opowiadania/"' not in microblog
    assert 'href="/"' in microblog
    assert '<script defer src="/js/privacy.js"></script>' in microblog
    assert microblog.count("<script") == 1
    de_index = (out / "de/opowiadania/index.html").read_text()
    assert "Der gute Vater" in de_index and "Kartka" not in de_index
    for rel in expected:
        if rel.endswith(".html"):
            html = (out / rel).read_text()
            assert 'name="viewport"' in html and (rel == "mikroblog/index.html") == ("<script" in html)
            assert not re.search(r'<(?:script|img)[^>]+src="https?://', html)
            assert not re.search(r'<link[^>]+rel="(?:stylesheet|icon)"[^>]+href="https?://', html)


def test_chapters_translation_links_and_standard_hr(writing: Path, tmp_path: Path):
    out = tmp_path / "site"
    assert build(writing, out).returncode == 0
    pl = (out / "opowiadania/dobry-ojciec/index.html").read_text()
    de = (out / "de/opowiadania/der-gute-vater/index.html").read_text()
    assert pl.count('<h2 class="chapter">') == de.count('<h2 class="chapter">') == 16
    for html in (pl, de):
        assert html.count('<h3 class="chapter-name">Natan</h3>') == 8
        assert html.count('<h3 class="chapter-name">Joram</h3>') == 8
        assert "align=" not in html and "\n---\n" not in html and 'class="sep"' in html
        assert 'hreflang="pl"' in html and 'hreflang="de"' in html
    assert "Wersja niemiecka" in pl and "Polnische Version" in de
    assert "Deutsche Übersetzung" not in de
    assert "<hr>" in (out / "opowiadania/kartka/index.html").read_text()


def test_manifest_is_complete_deterministic_and_private(writing: Path, tmp_path: Path):
    first, second = tmp_path / "one", tmp_path / "two"
    assert build(writing, first).returncode == build(writing, second).returncode == 0
    assert (first / "build-manifest.json").read_bytes() == (second / "build-manifest.json").read_bytes()
    manifest = json.loads((first / "build-manifest.json").read_text())
    assert str(writing) not in json.dumps(manifest)
    assert {item["path"] for item in manifest["sources"]} == {
        "Dobry_Ojciec/opowiadanie.md",
        "Kartka/kartka.md",
        "Dobry_Ojciec/der_gute_vater.md",
        "Mikroblog_2026/mikroblog_2026.md",
    }
    for item in manifest["artifacts"]:
        assert hashlib.sha256((first / item["path"]).read_bytes()).hexdigest() == item["sha256"]


@pytest.mark.parametrize("mutation", ["missing", "wrong_h1", "raw_html", "symlink"])
def test_bad_sources_fail_without_replacing_previous_output(writing: Path, tmp_path: Path, mutation: str):
    out = tmp_path / "site"
    assert build(writing, out).returncode == 0
    marker = (out / "index.html").read_bytes()
    target = writing / "Kartka/kartka.md"
    if mutation == "missing": target.unlink()
    elif mutation == "wrong_h1": target.write_text("# Nie Kartka\n")
    elif mutation == "raw_html": target.write_text("# Kartka\n<script>x</script>\n")
    else:
        outside = tmp_path / "outside.md"; outside.write_text("# Kartka\n")
        target.unlink(); target.symlink_to(outside)
    assert build(writing, out).returncode != 0
    assert (out / "index.html").read_bytes() == marker


def test_output_inside_tracked_source_is_rejected(writing: Path):
    assert build(writing, ROOT / "site").returncode != 0


def test_repository_does_not_track_generated_or_manuscript_text():
    tracked = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
    assert not any(p == "site" or p.startswith(("site/", ".build/")) for p in tracked)
    writing_root = Path("/home/jan/Sources/writing")
    if writing_root.is_dir():
        tracked_text = "\n".join((ROOT / path).read_text(encoding="utf-8") for path in tracked)
        for relative in (
            "Dobry_Ojciec/opowiadanie.md",
            "Kartka/kartka.md",
            "Dobry_Ojciec/der_gute_vater.md",
            "Mikroblog_2026/mikroblog_2026.md",
        ):
            for paragraph in (writing_root / relative).read_text(encoding="utf-8").split("\n\n"):
                if len(paragraph) >= 80 and not paragraph.startswith(("#", "<")):
                    assert paragraph not in tracked_text


def html_unescape(payload: str) -> str:
    import html
    return html.unescape(payload)


def module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_site", BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repository_without_protected_file_builds_unchanged(writing: Path, tmp_path: Path):
    out = tmp_path / "site"
    assert build(writing, out).returncode == 0
    microblog = (out / "mikroblog/index.html").read_text()
    assert 'section class="locked-entry"' not in microblog
    assert html_unescape("") == ""


def test_protected_entry_is_locked_and_not_leaked(locked: dict, tmp_path: Path):
    out = tmp_path / "site"
    result = build(locked["writing"], out, protected=locked["protected"], password=locked["password"])
    assert result.returncode == 0, result.stderr
    microblog = (out / "mikroblog/index.html").read_text()
    assert "24 września 2026" in microblog  # heading stays public
    assert PROTECTED_MARKING not in microblog and "Tajny akapit" not in microblog
    assert 'section class="locked-entry" data-protected="' in microblog
    js = (out / "js/privacy.js").read_text()
    assert "PBKDF2" in js and "AES-GCM" in js and "sochiera/blog-v1" in js
    # decrypt roundtrip mirrors the browser parameter set
    import base64, json as jsonmod
    site = module()
    match = re.search(r'data-protected="([^"]*)"', microblog)
    payload = jsonmod.loads(html_unescape(match.group(1)))
    assert set(payload) == {"v", "i", "s", "n", "c"}
    plaintext = site.decrypt_proof(
        locked["password"].read_bytes().strip(),
        base64.b64decode(payload["s"]), base64.b64decode(payload["n"]), base64.b64decode(payload["c"]),
        site.PROTECTED_AAD,
    ).decode("utf-8")
    assert PROTECTED_MARKING in plaintext and plaintext.startswith("<") and "&lt;" not in plaintext
    # wrong password must fail
    from cryptography.exceptions import InvalidTag
    with pytest.raises(InvalidTag):
        site.decrypt_proof(b"wrong-password", base64.b64decode(payload["s"]), base64.b64decode(payload["n"]), base64.b64decode(payload["c"]), site.PROTECTED_AAD)
    # salt and nonce are random per build
    second = tmp_path / "site2"
    assert build(locked["writing"], second, protected=locked["protected"], password=locked["password"]).returncode == 0
    assert re.search(r'data-protected="([^"]*)"', (second / "mikroblog/index.html").read_text()).group(1) != match.group(1)


def test_protected_entries_require_password_file(locked: dict, tmp_path: Path):
    out = tmp_path / "site"
    result = build(locked["writing"], out, protected=locked["protected"])
    assert result.returncode != 0 and "password file" in result.stderr


@pytest.mark.parametrize("mode, expected_ok", [(0o600, True), (0o644, False), (0o666, False)])
def test_password_file_permissions_are_enforced(locked: dict, tmp_path: Path, mode: int, expected_ok: bool):
    locked["password"].chmod(mode)
    out = tmp_path / "site"
    result = build(locked["writing"], out, protected=locked["protected"], password=locked["password"])
    assert (result.returncode == 0) == expected_ok


def test_unknown_protected_heading_fails(locked: dict, tmp_path: Path):
    (locked["writing"] / "Mikroblog_2026/mikroblog_2026.md").write_text(
        "# Mikroblog 2026\n\n## 23 września 2026\n\nPierwszy wpis.\n", encoding="utf-8",
    )
    result = build(locked["writing"], tmp_path / "site", protected=locked["protected"], password=locked["password"])
    assert result.returncode != 0 and "missing" in result.stderr


def test_empty_protected_file_builds_all_public(writing: Path, tmp_path: Path):
    protected = tmp_path / "protected-empty.toml"
    protected.write_text('schema_version = 1\nkdf = "pbkdf2-sha256"\niterations = 600000\nentries = []\n', encoding="utf-8")
    out = tmp_path / "site"
    assert build(writing, out, protected=protected).returncode == 0
    assert 'section class="locked-entry"' not in (out / "mikroblog/index.html").read_text()
