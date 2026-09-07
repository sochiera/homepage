#!/usr/bin/env bash
set -euo pipefail

readonly EXPECTED_HOST="old-vps"
readonly DOCROOT="/var/www/sochiera"
readonly RELEASE_DIR="/var/www/sochiera-releases"
readonly RELEASE_RE='^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$'
readonly BUILD_DIR=".build/site"
HOST=""; YES=0; ROLLBACK=""

die() { echo "deploy: $*" >&2; exit 1; }
usage() { echo "usage: scripts/deploy.sh --host old-vps [--yes] [--rollback RELEASE_ID]"; }

while (($#)); do
  case "$1" in
    --host) (($# >= 2)) || die "--host needs a value"; HOST=$2; shift 2 ;;
    --yes) YES=1; shift ;;
    --rollback) (($# >= 2)) || die "--rollback needs a value"; ROLLBACK=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ "$HOST" == "$EXPECTED_HOST" ]] || die "--host must be literal old-vps"
if (( ! YES )); then
  read -r -p "Type deploy to confirm: " answer
  [[ "$answer" == deploy ]] || die "confirmation refused"
fi
if [[ -n "$ROLLBACK" && ! "$ROLLBACK" =~ $RELEASE_RE ]]; then die "invalid rollback release identifier"; fi

manifest_hash() { sha256sum "$BUILD_DIR/build-manifest.json" | awk '{print $1}'; }

check_clean_manifest() {
  [[ -f "$BUILD_DIR/build-manifest.json" ]] || die "missing build manifest"
  .venv/bin/python - "$BUILD_DIR" <<'PY'
import hashlib, json, pathlib, sys
root=pathlib.Path(sys.argv[1]); data=json.loads((root/'build-manifest.json').read_text())
for item in data['artifacts']:
    path=root/item['path']
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:
        raise SystemExit('dirty or stale build manifest')
PY
  if find content layouts static scripts/build_site.py -type f -newer "$BUILD_DIR/build-manifest.json" | grep -q .; then
    die "stale build manifest"
  fi
  [[ -z "$(git status --porcelain --untracked-files=no)" ]] || die "tracked worktree is dirty"
}

check_dns() {
  local apex www alias_ip
  mapfile -t apex < <(getent ahostsv4 sochiera.pl | awk '{print $1}' | sort -u)
  mapfile -t www < <(getent ahostsv4 www.sochiera.pl | awk '{print $1}' | sort -u)
  mapfile -t alias_ip < <(ssh -G "$EXPECTED_HOST" | awk '$1=="hostname"{print $2}' | xargs getent ahostsv4 | awk '{print $1}' | sort -u)
  ((${#apex[@]}==1 && ${#www[@]}==1 && ${#alias_ip[@]}==1)) || die "DNS cardinality check failed"
  [[ "${apex[0]}" == "${www[0]}" && "${apex[0]}" == "${alias_ip[0]}" ]] || die "DNS/alias address disagreement"
  curl --fail --silent --show-error --head https://sochiera.pl/ >/dev/null
  curl --fail --silent --show-error --head https://www.sochiera.pl/ >/dev/null
}

check_remote_config() {
  ssh "$EXPECTED_HOST" "set -eu; nginx -T 2>&1 | grep -Eq 'server_name[[:space:]]+([^;]*[[:space:]])?sochiera\\.pl([[:space:];]|$)' && nginx -T 2>&1 | grep -Eq 'root[[:space:]]+$DOCROOT;' && test -f '$DOCROOT/index.html' && df -P '$DOCROOT' >/dev/null"
}

package_release() {
  RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)-$(manifest_hash | cut -c1-12)"
  PACKAGE=".build/packages/homepage-$RELEASE_ID.tar.gz"
  mkdir -p .build/packages
  tar --format=posix --owner=0 --group=0 -C "$BUILD_DIR" -czf "$PACKAGE" .
  tar -tzf "$PACKAGE" | awk '$0 ~ /^\// || $0 ~ /(^|\/)\.\.($|\/)/ {exit 1}' || die "unsafe archive"
}

upload_release() {
  scp "$PACKAGE" "$EXPECTED_HOST:/tmp/homepage-$RELEASE_ID.tar.gz"
  ssh "$EXPECTED_HOST" "set -eu; umask 022; mkdir -p '$RELEASE_DIR/.stage-$RELEASE_ID'; tar -xzf '/tmp/homepage-$RELEASE_ID.tar.gz' -C '$RELEASE_DIR/.stage-$RELEASE_ID'; find '$RELEASE_DIR/.stage-$RELEASE_ID' -type l -o -type b -o -type c | grep . && exit 1 || true; python3 - '$RELEASE_DIR/.stage-$RELEASE_ID' <<'PY'
import hashlib,json,pathlib,sys
r=pathlib.Path(sys.argv[1]); m=json.loads((r/'build-manifest.json').read_text())
assert all((r/i['path']).is_file() and hashlib.sha256((r/i['path']).read_bytes()).hexdigest()==i['sha256'] for i in m['artifacts'])
PY
rm -f '/tmp/homepage-$RELEASE_ID.tar.gz'"
}

backup_current() {
  ssh "$EXPECTED_HOST" "set -eu; test -d '$DOCROOT'; test -d '$RELEASE_DIR/.stage-$RELEASE_ID'; test ! -e '$RELEASE_DIR/backup-$RELEASE_ID'"
}

switch_release() {
  ssh "$EXPECTED_HOST" "set -eu; mkdir -p '$RELEASE_DIR'; flock '$RELEASE_DIR/.lock' sh -c 'mv "$DOCROOT" "$RELEASE_DIR/backup-$RELEASE_ID"; if ! mv "$RELEASE_DIR/.stage-$RELEASE_ID" "$DOCROOT"; then mv "$RELEASE_DIR/backup-$RELEASE_ID" "$DOCROOT"; exit 1; fi'"
}

verify_release() {
  local rel expected public_path host actual
  ssh "$EXPECTED_HOST" "python3 - '$DOCROOT' <<'PY'
import hashlib,json,pathlib,sys
r=pathlib.Path(sys.argv[1]); m=json.loads((r/'build-manifest.json').read_text())
assert all((r/i['path']).is_file() and hashlib.sha256((r/i['path']).read_bytes()).hexdigest()==i['sha256'] for i in m['artifacts'])
PY"
  while IFS=$'\t' read -r rel expected; do
    [[ "$rel" == */index.html ]] && public_path="/${rel%index.html}" || public_path="/$rel"
    [[ "$rel" == index.html ]] && public_path="/"
    for host in sochiera.pl www.sochiera.pl; do
      actual="$(curl --fail --silent --show-error "https://$host$public_path" | sha256sum | awk '{print $1}')" || return 1
      [[ "$actual" == "$expected" ]] || return 1
    done
  done < <(.venv/bin/python - "$BUILD_DIR/build-manifest.json" <<'PY'
import json,sys
for item in json.load(open(sys.argv[1]))['artifacts']:
    print(item['path'], item['sha256'], sep='\t')
PY
  )
  for host in sochiera.pl www.sochiera.pl; do
    [[ "$(curl --silent --output /dev/null --write-out '%{http_code}' "https://$host/de/opowiadania/kartka/")" != 200 ]] || return 1
  done
  curl --fail --silent --show-error --location --head https://sochiera.pl/malowanie-po-numerach/ >/dev/null
  ssh "$EXPECTED_HOST" "test -d '$DOCROOT' && nginx -T 2>&1 | grep -q '/api/pbn-'"
}

restore_backup() {
  ssh "$EXPECTED_HOST" "set -eu; flock '$RELEASE_DIR/.lock' sh -c 'test -d "$RELEASE_DIR/backup-$RELEASE_ID"; mv "$DOCROOT" "$RELEASE_DIR/failed-$RELEASE_ID"; mv "$RELEASE_DIR/backup-$RELEASE_ID" "$DOCROOT"'"
}

manual_rollback() {
  local target="$RELEASE_DIR/backup-$ROLLBACK" failed_id="$(date -u +%Y%m%dT%H%M%SZ)-rollback"
  ssh "$EXPECTED_HOST" "set -eu; test -d '$target'; flock '$RELEASE_DIR/.lock' sh -c 'mv "$DOCROOT" "$RELEASE_DIR/failed-$failed_id"; mv "$target" "$DOCROOT"'"
  verify_release || die "rollback verification failed"
}

check_clean_manifest
check_dns
check_remote_config
if [[ -n "$ROLLBACK" ]]; then manual_rollback; exit 0; fi
package_release
upload_release
backup_current
switch_release
if ! verify_release; then restore_backup; verify_release || die "automatic rollback verification failed"; die "release verification failed; previous site restored"; fi
echo "Deployed $RELEASE_ID"
