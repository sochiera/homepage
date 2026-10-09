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
