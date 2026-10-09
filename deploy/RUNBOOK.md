# Homepage release runbook

This procedure replaces only `/var/www/sochiera`. It does not edit nginx and does not deploy or remove the paint-by-number application.

1. Start from a clean tracked worktree. Create `.venv`, install `.[test]`, run pytest, and build with the command in the README (include `--protected-file content/protected.toml` and the operator's local `--password-file`).
2. Inspect `.build/site`, `build-manifest.json`, its hashes, and the archive listing. Confirm `git status --short` and `git ls-files` contain no generated story HTML and no password material. Protected entries must appear only as `data-protected` ciphertext supplied by `static/js/privacy.js` decryption. Compare every live `/pobierz/` file with the bundled snapshot (APK 1.2–1.5 and version metadata); preserve all downloads and link the version named by `mealspire-wersja.json`. Refresh the public snapshot if Mealspire has published a newer version. The switch checks download hashes, metadata and the homepage anchor under the deployment lock and refuses drift or unknown docroot files before moving the current tree.
3. Run `scripts/deploy.sh --host ubuntu@51.83.199.206`. The helper connects with `/home/jan/.ssh/pbn_vps` (or `$HOME/.ssh/pbn_vps`), so no SSH host alias or `~/.ssh/config` entry is required. For non-interactive operator automation, add `--yes`.
4. Record the printed release ID. Verify these resources on both public hostnames: `/`, `/mikroblog/`, `/opowiadania/`, `/opowiadania/dobry-ojciec/`, `/opowiadania/kartka/`, `/styles.css`, `/js/privacy.js`, and `/favicon.svg`. Confirm unpublished German routes and the retired `/v2/` preview return 404; check `/poker/`, `/biblioteka/` and `/malowanie-po-numerach/` remain reachable and all download hashes match. On `/mikroblog/` verify protected entries expose only the blurred lockbox (no plaintext markers) and that unlocking with the password shows the entry locally. Never log the password, plaintext, or browser input parameters.

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
