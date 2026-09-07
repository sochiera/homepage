# sochiera.pl homepage

Static homepage and story publisher. Story bodies remain outside this repository and are injected only into ignored build output.

## Local development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest
.venv/bin/python scripts/build_site.py --writing-root /home/jan/Sources/writing --output .build/site
```

Only the three paths declared in `content/stories.toml` are read. Never add `.build/`, generated HTML, manuscripts, operator configuration, or secrets to Git.

Production operators use the preconfigured SSH alias `old-vps`; connection details do not belong in this repository. See [deploy/RUNBOOK.md](deploy/RUNBOOK.md).
