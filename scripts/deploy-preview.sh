#!/usr/bin/env bash
# Publish an isolated, noindex preview build to https://sochiera.pl/v2/.
# Touches only /var/www/sochiera/v2 (plus a retained backup under the release
# directory); never the production tree, nginx, or other applications.
set -euo pipefail

readonly EXPECTED_HOST="ubuntu@51.83.199.206"
readonly IDENTITY_FILE="${HOME}/.ssh/pbn_vps"
readonly DOCROOT="/var/www/sochiera"
readonly RELEASE_DIR="/var/www/sochiera-releases"
readonly PREVIEW="v2"
readonly BUILD_DIR=".build/preview-v2"
SSH=(ssh -i "$IDENTITY_FILE" -o IdentitiesOnly=yes "$EXPECTED_HOST")

die() { echo "deploy-preview: $*" >&2; exit 1; }
[[ "${1:-}" == "--yes" ]] || { read -r -p "Type preview to publish /$PREVIEW/: " answer; [[ "$answer" == preview ]] || die "confirmation refused"; }

[[ -f "$BUILD_DIR/index.html" && -f "$BUILD_DIR/build-manifest.json" ]] || die "missing $BUILD_DIR (build with --base-path /$PREVIEW --output $BUILD_DIR)"
grep -q 'name="robots" content="noindex,nofollow"' "$BUILD_DIR/index.html" || die "build is not a noindex preview"
grep -q "href=\"/$PREVIEW/styles.css\"" "$BUILD_DIR/index.html" || die "build was not made with --base-path /$PREVIEW"

# Verify the whole package, not just the entry page: manifest hashes, exact file
# set, noindex + preview banner + prefixed links on every page, encrypted entries.
python3 - "$BUILD_DIR" "/$PREVIEW" <<'PY' || die "preview package failed verification"
import hashlib, json, pathlib, re, sys, tomllib
root, base = pathlib.Path(sys.argv[1]), sys.argv[2]
manifest = json.loads((root / "build-manifest.json").read_text())
listed = {item["path"]: item["sha256"] for item in manifest["artifacts"]}
actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink()} - {"build-manifest.json"}
if actual != set(listed): sys.exit(f"file set differs from manifest: {sorted(actual ^ set(listed))}")
for rel, digest in listed.items():
    path = root / rel
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest: sys.exit(f"stale or modified artifact: {rel}")
for page in root.rglob("*.html"):
    html = page.read_text(encoding="utf-8")
    if 'name="robots" content="noindex,nofollow"' not in html or 'class="preview-flag"' not in html: sys.exit(f"not a noindex preview page: {page}")
    for link in re.findall(r'(?:href|src|action)="(/[^"]*)"', html):
        if not link.startswith((base + "/", "/poker/", "/malowanie-po-numerach/", "/pobierz/")): sys.exit(f"link escapes {base}: {link} in {page}")
protected = pathlib.Path("content/protected.toml")
if protected.is_file():
    expected = len(tomllib.loads(protected.read_text(encoding="utf-8")).get("entries", []))
    found = (root / "mikroblog/index.html").read_text(encoding="utf-8").count('data-protected="')
    if found != expected: sys.exit(f"expected {expected} encrypted microblog entries, found {found}")
PY

ID="$(date -u +%Y%m%dT%H%M%SZ)-$(sha256sum "$BUILD_DIR/build-manifest.json" | cut -c1-12)"
PACKAGE=".build/packages/preview-$ID.tar.gz"
mkdir -p .build/packages
tar --format=posix --owner=0 --group=0 -C "$BUILD_DIR" -czf "$PACKAGE" .
tar -tzf "$PACKAGE" | awk '$0 ~ /^\// || $0 ~ /(^|\/)\.\.($|\/)/ {exit 1}' || die "unsafe archive"

before="$(curl --fail --silent --show-error https://sochiera.pl/ | sha256sum | cut -c1-64)"
scp -i "$IDENTITY_FILE" -o IdentitiesOnly=yes "$PACKAGE" "$EXPECTED_HOST:/tmp/preview-$ID.tar.gz"
"${SSH[@]}" "set -eu; umask 022; stage='$RELEASE_DIR/.preview-$ID'; sudo -n mkdir -p \"\$stage\"; sudo -n tar -xzf '/tmp/preview-$ID.tar.gz' -C \"\$stage\"; rm -f '/tmp/preview-$ID.tar.gz'; sudo -n chmod -R a+rX \"\$stage\"; if find \"\$stage\" -type l | grep -q .; then exit 1; fi; sudo -n flock '$RELEASE_DIR/.lock' sh -c 'if [ -e \"$DOCROOT/$PREVIEW\" ]; then mv \"$DOCROOT/$PREVIEW\" \"$RELEASE_DIR/preview-backup-$ID\"; fi; mv \"$RELEASE_DIR/.preview-$ID\" \"$DOCROOT/$PREVIEW\"'"

for path in / /o-mnie/ /mikroblog/ /opowiadania/ /styles.css /js/privacy.js; do
  [[ "$(curl --silent --output /dev/null --write-out '%{http_code}' "https://sochiera.pl/$PREVIEW$path")" == 200 ]] || die "preview path /$PREVIEW$path not reachable"
done
after="$(curl --fail --silent --show-error https://sochiera.pl/ | sha256sum | cut -c1-64)"
[[ "$before" == "$after" ]] || die "production homepage changed during preview publish — investigate"
echo "Preview $ID published at https://sochiera.pl/$PREVIEW/"
