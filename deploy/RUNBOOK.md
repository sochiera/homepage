# Homepage release runbook

This procedure replaces only `/var/www/sochiera`. It does not edit nginx and does not deploy or remove the paint-by-number application.

1. Start from a clean tracked worktree. Create `.venv`, install `.[test]`, run pytest, and build with the command in the README.
2. Inspect `.build/site`, `build-manifest.json`, its hashes, and the archive listing. Confirm `git status --short` and `git ls-files` contain no generated story HTML.
3. Run `scripts/deploy.sh --host old-vps`. For non-interactive operator automation, add `--yes`.
4. Record the printed release ID. Verify these resources on both public hostnames: `/`, `/opowiadania/`, `/opowiadania/dobry-ojciec/`, `/opowiadania/kartka/`, `/de/opowiadania/`, `/de/opowiadania/der-gute-vater/`, `/styles.css`, and `/favicon.svg`. Confirm there is no German Kartka route and `/malowanie-po-numerach/` remains reachable.

The helper refuses DNS, alias, nginx docroot, manifest, or worktree drift. It stages on the docroot filesystem, retains the previous complete tree below `/var/www/sochiera-releases` as `backup-<release-id>`, switches with renames under a lock, verifies, and automatically restores that backup if verification fails.

To restore a retained exact release, preview the identifier from prior operator output and run:

```bash
scripts/deploy.sh --host old-vps --rollback 20260907T120000Z-012345abcdef
```

Rollback accepts only the constrained release-ID form, preserves the replaced tree, and repeats the verification matrix. Never put addresses, usernames, key names or paths, credentials, or other connection configuration in this repository.
