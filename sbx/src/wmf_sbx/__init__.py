"""wmf-sbx: host-side tooling for Docker Sandboxes ("sbx") MediaWiki dev.

Not installed as a package (no pyproject.toml/setup.py) -- sbx/bin's thin
wrapper scripts and sbx/tests both add sbx/src to sys.path by hand before
importing from here, so this stays a plain src-layout directory rather
than a pip-installable one. See sbx/NOTES.md for why (§49).
"""
