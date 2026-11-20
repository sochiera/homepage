#!/usr/bin/env python3
"""Check that the Parlo APK named by parlo-wersja.json is the official release and is what the site serves.

  official            metadata == latest stable GitHub release (tag, asset size, SHA-256)
  live BASE_URL...    fresh read of BASE_URL/: exactly one Parlo link, matching the metadata,
                      its APK answers HTTP 200 and hashes to the metadata SHA-256
                      (add --official to also compare with the GitHub release)

Exit 0 only when everything agrees; a Parlo release counts as deployed only after `live` passes.
Read-only: never uploads, switches or deploys anything.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
METADATA = ROOT / "static/pobierz/parlo-wersja.json"
RELEASE_API = "https://api.github.com/repos/sochiera/italiano/releases/latest"
LINK_RE = re.compile(r'<a href="(/pobierz/parlo-[^"]+)" download>Parlo — aplikacja na Androida \(APK ([^)]+)\)</a>')


class Mismatch(Exception):
    pass


def fetch(url, headers=None):
    request = urllib.request.Request(url, headers={"User-Agent": "sochiera-homepage-verify", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, b""


def load_metadata(path=METADATA):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("versionName", "releaseTag", "assetName", "size", "sha256"):
        if key not in data:
            raise Mismatch(f"metadata lacks {key}")
    return data


def github_token():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token or not shutil.which("gh"):
        return token
    result = subprocess.run(["gh", "auth", "token"], text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else None


def check_official(metadata, fetch=fetch):
    headers = {"Accept": "application/vnd.github+json"}
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, body = fetch(RELEASE_API, headers)
    if status != 200:
        # sochiera/italiano is private: anonymous requests get 404.
        raise Mismatch(f"GitHub latest release: HTTP {status}" + ("" if token else " (no GitHub token; run `gh auth login` or set GITHUB_TOKEN)"))
    release = json.loads(body)
    if release.get("draft") or release.get("prerelease"):
        raise Mismatch("GitHub latest release is not a stable release")
    if release.get("tag_name") != metadata["releaseTag"]:
        raise Mismatch(f"homepage names {metadata['releaseTag']}, latest stable release is {release.get('tag_name')}")
    assets = [a for a in release.get("assets", []) if a.get("name") == metadata["assetName"]]
    if len(assets) != 1:
        raise Mismatch(f"release has no single {metadata['assetName']} asset")
    asset = assets[0]
    if asset.get("size") != metadata["size"] or asset.get("digest") != f"sha256:{metadata['sha256']}":
        raise Mismatch("release asset size or SHA-256 differs from metadata")
    return f"official {metadata['releaseTag']} sha256={metadata['sha256']}"


def check_live(base_url, metadata, fetch=fetch):
    base_url = base_url.rstrip("/")
    status, body = fetch(base_url + "/", {"Cache-Control": "no-cache"})
    if status != 200:
        raise Mismatch(f"{base_url}/: HTTP {status}")
    links = LINK_RE.findall(body.decode("utf-8"))
    if len(links) != 1:
        raise Mismatch(f"{base_url}/: expected one Parlo download link, found {len(links)}")
    href, version = links[0]
    expected = f"/pobierz/parlo-{metadata['versionName']}.apk"
    if href != expected or version != metadata["versionName"]:
        raise Mismatch(f"{base_url}/ links {href} (APK {version}), metadata names {expected}")
    status, apk = fetch(base_url + href, {"Cache-Control": "no-cache"})
    if status != 200:
        raise Mismatch(f"{base_url}{href}: HTTP {status}")
    digest = hashlib.sha256(apk).hexdigest()
    if digest != metadata["sha256"] or len(apk) != metadata["size"]:
        raise Mismatch(f"{base_url}{href}: sha256={digest} size={len(apk)} differs from metadata")
    return f"live {base_url}{href} HTTP 200 sha256={digest}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--metadata", type=Path, default=METADATA)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("official")
    live = sub.add_parser("live")
    live.add_argument("base_urls", nargs="+")
    live.add_argument("--official", action="store_true", help="also compare metadata with the GitHub release")
    args = parser.parse_args(argv)
    try:
        metadata = load_metadata(args.metadata)
        results = []
        if args.mode == "official" or args.official:
            results.append(check_official(metadata))
        if args.mode == "live":
            results += [check_live(url, metadata) for url in args.base_urls]
    except (Mismatch, OSError, ValueError) as error:
        print(f"parlo release NOT consistent: {error}", file=sys.stderr)
        return 1
    print("\n".join(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
