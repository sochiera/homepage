from __future__ import annotations

import hashlib
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


@pytest.fixture
def writing(tmp_path: Path) -> Path:
    root = tmp_path / "writing"
    (root / "Dobry_Ojciec").mkdir(parents=True)
    (root / "Kartka").mkdir()
    (root / "Dobry_Ojciec/opowiadanie.md").write_text(manuscript("Dobry Ojciec", True), encoding="utf-8")
    (root / "Dobry_Ojciec/der_gute_vater.md").write_text("> Deutsche Übersetzung.\n\n" + manuscript("Der gute Vater", True), encoding="utf-8")
    (root / "Kartka/kartka.md").write_text(manuscript("Kartka"), encoding="utf-8")
    return root


def build(writing: Path, output: Path, *, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(BUILDER), "--writing-root", str(writing), "--output", str(output)], cwd=cwd, text=True, capture_output=True)


def test_build_outputs_exact_routes_and_metadata(writing: Path, tmp_path: Path):
    out = tmp_path / "site"
    result = build(writing, out)
    assert result.returncode == 0, result.stderr
    expected = {
        "index.html", "opowiadania/index.html", "opowiadania/dobry-ojciec/index.html",
        "opowiadania/kartka/index.html", "de/opowiadania/index.html",
        "de/opowiadania/der-gute-vater/index.html", "styles.css", "favicon.svg",
        "build-manifest.json",
    }
    assert {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()} == expected
    home = (out / "index.html").read_text()
    assert home.count('href="/malowanie-po-numerach/"') == 1
    assert "Sii Poland" in home and "Nokia" in home and "Agent Loop" in home
    de_index = (out / "de/opowiadania/index.html").read_text()
    assert "Der gute Vater" in de_index and "Kartka" not in de_index
    for rel in expected:
        if rel.endswith(".html"):
            html = (out / rel).read_text()
            assert 'name="viewport"' in html and "<script" not in html
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
    assert {item["path"] for item in manifest["sources"]} == {"Dobry_Ojciec/opowiadanie.md", "Kartka/kartka.md", "Dobry_Ojciec/der_gute_vater.md"}
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
        for relative in ("Dobry_Ojciec/opowiadanie.md", "Kartka/kartka.md", "Dobry_Ojciec/der_gute_vater.md"):
            for paragraph in (writing_root / relative).read_text(encoding="utf-8").split("\n\n"):
                if len(paragraph) >= 80 and not paragraph.startswith(("#", "<")):
                    assert paragraph not in tracked_text
