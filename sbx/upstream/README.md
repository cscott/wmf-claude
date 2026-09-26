# Upstream tasks

Bugs this work found in code outside `sbx/`, written as Phabricator tasks
for the engineer to file. Each `PHAB-TASK-<n>.md` follows the
`/wmf-claude:write-phab-task` template; `PHAB-ATTACHMENT-<n>.patch` is its
fix, as a `git diff` against the named repo. Every patch was applied and
tested as the task says.

| n | Project | Bug | Patch against |
|---|---|---|---|
| 1 | MediaWiki-REST-API | header parameters looked up by exact case | mediawiki/core |
| 2 | wdio-mediawiki | empty `MW_SCRIPT_PATH` rejected | mediawiki/core `tests/selenium/wdio-mediawiki/` |
| 3 | Popups | node-qunit tests do not run on Node 21+, exit 0 | mediawiki/extensions/Popups `package.json` |
| 4 | wmf-claude | `find` rules never apply; dead `Write(...)` denies | wmf-claude `main` |
| 5 | wmf-claude | run-tests skill teaches `vendor/bin/phpunit` | wmf-claude `main` |

When a task is filed, add its T-number to the table. When upstream takes
a fix, delete the pair; for 5, also delete
`sbx/patches/plugin/01-run-tests-composer-entrypoint.patch`.
