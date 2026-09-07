#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import markdown
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://sochiera.pl"
EXPECTED_SOURCES = {"Dobry_Ojciec/opowiadanie.md", "Kartka/kartka.md", "Dobry_Ojciec/der_gute_vater.md"}
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
        source = writing_root / story["source"]
        if source.is_symlink() or not source.is_file(): fail(f"missing or linked source: {story['source']}")
        relative = Path(story["source"])
        current = writing_root
        for part in relative.parts:
            current /= part
            if current.is_symlink(): fail(f"linked source component: {story['source']}")
        resolved = source.resolve()
        if not resolved.is_relative_to(writing_root): fail("source escapes writing root")
        story["path"] = resolved
    return stories

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
        if "<" in line or ">" in line:
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

def render_site(writing_root: Path, staging: Path) -> None:
    stories = load_stories(writing_root)
    env = Environment(loader=FileSystemLoader(ROOT / "layouts"), autoescape=True, undefined=StrictUndefined, keep_trailing_newline=True)
    common = dict(alternates=[])
    def write(rel: str, template: str, **context) -> None:
        target = staging / rel; target.parent.mkdir(parents=True, exist_ok=True)
        values = common | context
        target.write_text(env.get_template(template).render(**values), encoding="utf-8")
    write("index.html", "home.html", lang="pl", title="Jan Sochiera", og_title="Jan Sochiera", description="Jan Sochiera — inżynier oprogramowania. Kariera, ostatnie projekty i twórczość literacka.", canonical=BASE_URL + "/")
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
    validate_output(staging)
    artifacts = [{"path": p.relative_to(staging).as_posix(), "sha256": sha(p)} for p in sorted(staging.rglob("*")) if p.is_file()]
    sources = [{"path": s["source"], "sha256": sha(s["path"])} for s in sorted(stories, key=lambda x: x["source"])]
    manifest = {"schema_version": 1, "tool_version": "1.0.0", "artifacts": artifacts, "sources": sources}
    (staging / "build-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

def validate_output(staging: Path) -> None:
    expected = {"index.html", "opowiadania/index.html", "opowiadania/dobry-ojciec/index.html", "opowiadania/kartka/index.html", "de/opowiadania/index.html", "de/opowiadania/der-gute-vater/index.html", "styles.css", "favicon.svg"}
    actual = {p.relative_to(staging).as_posix() for p in staging.rglob("*") if p.is_file()}
    if actual != expected or any(p.is_symlink() for p in staging.rglob("*")): fail("unexpected publish tree")
    for page in staging.rglob("*.html"):
        text = page.read_text(encoding="utf-8")
        if "<script" in text.lower(): fail("scripts are forbidden")
        for link in re.findall(r'(?:href|src)="([^"]+)"', text):
            parsed = urlsplit(link)
            if parsed.scheme in {"http", "https"}: continue
            if not link.startswith("/"): fail(f"non-root-relative link: {link}")
            if link.startswith("/malowanie-po-numerach/"): continue
            target = staging / (link.lstrip("/") + ("index.html" if link.endswith("/") else ""))
            if not target.is_file(): fail(f"broken internal link: {link}")

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--writing-root", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / ".build/site")
    args = parser.parse_args()
    writing_root = args.writing_root.resolve()
    output = args.output.absolute()
    if not writing_root.is_dir(): fail("writing root does not exist")
    if output.is_relative_to(ROOT) and not output.is_relative_to(ROOT / ".build"): fail("output inside tracked source tree is forbidden")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        render_site(writing_root, staging)
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
