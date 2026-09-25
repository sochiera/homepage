# Homepage release runbook

This procedure replaces only `/var/www/sochiera`. It does not edit nginx and does not deploy or remove the paint-by-number application.

1. Start from a clean tracked worktree. Create `.venv`, install `.[test]`, run pytest, and build with the command in the README (include `--protected-file content/protected.toml` and the operator's local `--password-file`).
2. Inspect `.build/site`, `build-manifest.json`, its hashes, and the archive listing. Confirm `git status --short` and `git ls-files` contain no generated story HTML and no password material. Protected entries must appear only as `data-protected` ciphertext supplied by `static/js/privacy.js` decryption.
3. Run `scripts/deploy.sh --host ubuntu@51.83.199.206`. The helper connects with `/home/jan/.ssh/pbn_vps` (or `$HOME/.ssh/pbn_vps`), so no SSH host alias or `~/.ssh/config` entry is required. For non-interactive operator automation, add `--yes`.
4. Record the printed release ID. Verify these resources on both public hostnames: `/`, `/mikroblog/`, `/opowiadania/`, `/opowiadania/dobry-ojciec/`, `/opowiadania/kartka/`, `/de/opowiadania/`, `/de/opowiadania/der-gute-vater/`, `/styles.css`, `/js/privacy.js`, and `/favicon.svg`. Confirm there is no German Kartka route and `/malowanie-po-numerach/` remains reachable. On `/mikroblog/` verify protected entries expose only the blurred lockbox (no plaintext markers) and that unlocking with the password shows the entry locally.

The helper refuses DNS, target, nginx docroot, manifest, or worktree drift. It stages on the docroot filesystem, retains the previous complete tree below `/var/www/sochiera-releases` as `backup-<release-id>`, switches with renames under a lock, verifies, and automatically restores that backup if verification fails.

To restore a retained exact release, preview the identifier from prior operator output and run:

```bash
scripts/deploy.sh --host ubuntu@51.83.199.206 --rollback 20260907T120000Z-012345abcdef
```

Rollback accepts only the constrained release-ID form, preserves the replaced tree, and repeats the verification matrix. The connection target and private-key filename are intentionally fixed in the helper; never put key contents, credentials, passwords, or other secrets in this repository.
