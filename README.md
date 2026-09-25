# sochiera.pl homepage

Static homepage and story publisher. Story bodies remain outside this repository and are injected only into ignored build output.

## Local development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest
.venv/bin/python scripts/build_site.py --writing-root /home/jan/Sources/writing --output .build/site \
  --protected-file content/protected.toml --password-file /home/jan/.config/sochiera/blog-password
```

`content/protected.toml` declares microblog entries that are published only as a blurred preview with an encrypted body (AES-256-GCM, PBKDF2-SHA256, 600 000 iterations). The browser decrypts them with the shared password; the plaintext fragment never appears in the repository or the built site. The password lives only in Jan's local secret file with `0600` permissions and is passed to the builder via `--password-file`; never commit or copy it.

Only the approved paths declared in `content/stories.toml` and `content/microblog.toml` are read. Never add `.build/`, generated HTML, manuscripts, operator configuration, or secrets to Git.

Production operators use the preconfigured SSH alias `old-vps`; connection details do not belong in this repository. See [deploy/RUNBOOK.md](deploy/RUNBOOK.md).
