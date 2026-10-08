Title: wmf-claude: The "Linux glob caveat" may be stale: `Read` glob denies fired on Linux under Claude Code 2.1.269

## Summary

`docs/security-rationale.md` ("Linux glob caveat") says Claude Code ignores glob patterns in `Read` and `Edit` rules on Linux, and `bin/wmf-claude-setup` warns about it on every Linux install. On Claude Code 2.1.269 on Linux, `Read(**/*.pem)` and `Read(**/*.env)` did deny a Read of `deep/nested/secret.pem` and of `sample.env`. If the caveat no longer holds, the document and the warning understate what the tool layer does on Linux.

## Technical notes

Measured in an sbx sandbox (`sbx/NOTES.md` §70.4) with the deny list of the time, which used the relative `**/` form. Upstream `main` now uses the absolute `//**/` form (`Read(//**/*.pem)`, `Edit(//**/.env*)`, …), which was not measured. Upstream's text quotes a startup warning ("On Linux, glob patterns in Edit/Read rules will be ignored"), so some Claude Code version prints it. Re-measure on the current Claude Code, with the shipped `wiring/settings-merge.json`, before filing: check whether the warning still prints, and whether a Read and an Edit of a nested `.pem` and `.env` are denied.

The `find -exec` and `Write(...)` parts of the original task are fixed upstream and are not part of this one.

## Acceptance criteria

- [ ] `docs/security-rationale.md` states which Claude Code versions ignore `Read`/`Edit` globs on Linux, as measured.
- [ ] `bin/wmf-claude-setup` prints its Linux warning only if the measurement still supports it.
