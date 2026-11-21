# Homepage release runbook

This procedure replaces only `/var/www/sochiera`. It does not edit nginx and does not deploy or remove the paint-by-number application.

1. Start from a clean tracked worktree. Create `.venv`, install `.[test]`, run pytest, and build with the command in the README (include `--protected-file content/protected.toml` and the operator's local `--password-file`).
2. Inspect `.build/site`, `build-manifest.json`, its hashes, and the archive listing. Confirm `git status --short` and `git ls-files` contain no generated story HTML and no password material. Protected entries must appear only as `data-protected` ciphertext supplied by `static/js/privacy.js` decryption. Compare every live `/pobierz/` file with the bundled snapshot (APK 1.2–1.5 and version metadata); preserve all downloads and link the version named by `mealspire-wersja.json`. Refresh the public snapshot if Mealspire has published a newer version. The switch checks download hashes, metadata and the homepage anchor under the deployment lock and refuses drift or unknown docroot files before moving the current tree.
   **Parlo** has a single source of truth: `static/pobierz/parlo-wersja.json` (`versionName`, `releaseTag`, `assetName`, `size`, `sha256`). The homepage link is rendered from it, the build refuses a missing APK or a size/SHA-256 mismatch, and the switch refuses a homepage link that names any other file. To publish a new Parlo release: download the asset of the latest **stable** GitHub release (`gh release download android-native-vX -R sochiera/italiano -p parlo.apk`), confirm its SHA-256 equals the release asset `digest` and the hash in the release notes, save it as `static/pobierz/parlo-X.apk` (keep every older `parlo-*.apk`), add it to `STATIC_FILES`, update the metadata file, then run `.venv/bin/python3 scripts/verify_parlo_release.py official`.
   **Stable Parlo address:** `/pobierz/parlo.apk` always serves the latest official APK. It is not a source file: the build writes a byte copy of the versioned `parlo-X.apk` named by the metadata (no symlink), the switch refuses an alias whose SHA-256/size differs from the metadata, and `verify_parlo_release.py live` checks both addresses. Older versioned `parlo-*.apk` stay published.
   **One-time migration of the hand-uploaded `/pobierz/parlo.apk`** (2026-11-19, not an official asset; the switch refuses it as "live downloads drift"): inspect the live file's SHA-256 under SSH (`sha256sum /var/www/sochiera/pobierz/parlo.apk`) and pass exactly that value: `scripts/deploy.sh --host ubuntu@51.83.199.206 --retire-download pobierz/parlo.apk=<sha256>`. Under the deployment lock the switch refuses if the live bytes differ from the acknowledged hash, copies the file to `/var/www/sochiera-releases/retired-<release>/pobierz/parlo.apk` with `SHA256SUMS`, and only then swaps the tree (the full previous tree is also kept as `backup-<release>`). Nothing is deleted. To restore: `scripts/deploy.sh --rollback <release>` (or a failed verification does it automatically), or copy the retired file back and check it with `sha256sum -c SHA256SUMS`. Never use `--retire-download` without first inspecting the file it names.
3. Run `scripts/deploy.sh --host ubuntu@51.83.199.206`. The helper connects with `/home/jan/.ssh/pbn_vps` (or `$HOME/.ssh/pbn_vps`), so no SSH host alias or `~/.ssh/config` entry is required. For non-interactive operator automation, add `--yes`. Before uploading it runs `verify_parlo_release.py official` (needs `gh auth login` or `GITHUB_TOKEN`; the Parlo repository is private) and refuses to ship while the metadata lags the latest stable release. After the switch it runs `verify_parlo_release.py live` on both hostnames; a failure restores the previous tree.
4. A Parlo release counts as **deployed** only after a fresh read passes: `.venv/bin/python3 scripts/verify_parlo_release.py live https://sochiera.pl https://www.sochiera.pl --official` (exactly one Parlo link on `/`, version equal to the metadata, both the versioned APK and `/pobierz/parlo.apk` HTTP 200 with SHA-256 equal to the metadata and to the GitHub release asset). Do not mark the deployment done on the helper's exit code alone if this check was skipped.
5. Record the printed release ID. Verify these resources on both public hostnames: `/`, `/mikroblog/`, `/opowiadania/`, `/opowiadania/dobry-ojciec/`, `/opowiadania/kartka/`, `/styles.css`, `/js/privacy.js`, and `/favicon.svg`. Confirm unpublished German routes and the retired `/v2/` preview return 404; check `/poker/`, `/biblioteka/` and `/malowanie-po-numerach/` remain reachable and all download hashes match. On `/mikroblog/` verify protected entries expose only the blurred lockbox (no plaintext markers) and that unlocking with the password shows the entry locally. Never log the password, plaintext, or browser input parameters.

The helper refuses DNS, target, nginx docroot, manifest, or worktree drift. It stages on the docroot filesystem, retains the previous complete tree below `/var/www/sochiera-releases` as `backup-<release-id>`, switches with renames under a lock, verifies, and automatically restores that backup if verification fails.

To restore a retained exact release, preview the identifier from prior operator output and run:

```bash
scripts/deploy.sh --host ubuntu@51.83.199.206 --rollback 20260907T120000Z-012345abcdef
```

Rollback accepts only the constrained release-ID form, preserves the replaced tree, and repeats the verification matrix. The connection target and private-key filename are intentionally fixed in the helper; never put key contents, credentials, passwords, or other secrets in this repository.

## Isolated preview (`/v2/`)

A redesign can be reviewed live without touching production. Build the whole site below a prefix (every page gets `noindex,nofollow` and a preview banner; links to `/poker/`, `/malowanie-po-numerach/` and `/pobierz/` stay at the root):

```bash
.venv/bin/python3 scripts/build_site.py --writing-root /home/jan/Sources/writing --output .build/preview-v2 --base-path /v2 \
  --protected-file content/protected.toml --password-file /home/jan/.config/sochiera/blog-password
scripts/deploy-preview.sh   # add --yes for operator automation
```

The helper replaces only `/var/www/sochiera/v2` (the previous preview is kept as `/var/www/sochiera-releases/preview-backup-<id>`), checks the preview routes and confirms the production homepage bytes did not change. nginx already serves `/v2/` through the static `location /`. The next production release (`scripts/deploy.sh`) swaps the whole docroot and therefore removes the preview; to remove it earlier, move `/var/www/sochiera/v2` into the release directory.
