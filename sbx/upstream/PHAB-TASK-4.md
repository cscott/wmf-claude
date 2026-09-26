Title: wmf-claude: The `find -exec` deny rules in settings-merge.json never apply, and the `Write(...)` denies are dead

## Summary

`wiring/settings-merge.json` spells its `find` rules `Bash(find:* -exec*)`. Claude Code rejects that form ("the `:*` pattern must be at the end") and skips a rejected rule, so `find -exec`, `-execdir`, `-delete`, `-ok`, `-okdir` and `-fprint` are denied in no backend, and the eight `find:* -…` allow rules do not match either. Separately, the eight `Write(...)` deny rules are never consulted, because `Edit(path)` is the rule that covers every file-editing tool; Claude Code says so on stderr at every startup.

## Technical notes

`:*` is a prefix match and may only end a pattern. The spelling that matches is `Bash(find *-exec*)`: it catches `find . -exec …` and the path-less `find -exec …`, and leaves `find . -name …` allowed. Measured against Claude Code 2.1.269. The `Edit(...)` denies on the same paths stay, and they already cover the Write tool, so removing the `Write(...)` lines removes no protection.

The attached patch (PHAB-ATTACHMENT-4.patch, against `main`) fixes the 14 `find` rules, removes the 8 `Write(...)` rules, and updates `SECURITY.md` to match. It also rewrites the "Linux caveat" paragraph: under 2.1.269 on Linux, `Read(**/*.pem)` and `Read(**/*.env)` do deny `deep/nested/secret.pem` and `sample.env`, so the glob rules now fire there. `bin/wmf-claude-setup` still prints the Linux warning; whether to keep it is a separate decision.

## Acceptance criteria

- [ ] With the shipped settings, `find . -exec true \;` is denied and `find . -name x` is allowed, and Claude Code prints no "pattern must be at the end" or "`Write(...)` is not matched" warnings at startup.
- [ ] `SECURITY.md` describes the rules as they are.
