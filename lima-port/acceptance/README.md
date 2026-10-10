# The blind acceptance run (phase 7)

`TESTING.md` is the task list for the blind run of
`sbx/DESIGN-testing-instructions.md` §9, for a Lima sandbox. On the host:

```bash
wmf-sbx create --reset-all --name sbx-testverify \
  gerrit:mediawiki/extensions/Translate \
  gerrit:mediawiki/extensions/Cite \
  gerrit:mediawiki/services/parsoid
limactl shell --workdir / wmf-sbx-sbx-testverify -- \
  sudo -u agent sh -c 'cat > /home/agent/TESTING.md' < lima-port/acceptance/TESTING.md
wmf-sbx resume sbx-testverify --read-file=/home/agent/TESTING.md -- \
  -p "Read ~/TESTING.md and do what it says." \
  --allowedTools "Bash Read Write Edit Glob Grep TodoWrite" --max-turns 400
git -C ~/<Translate checkout> fetch sbx-testverify
git -C ~/<Translate checkout> show sbx-testverify/sandbox-report:REPORT.md
```

`--read-file`: the agent's home is not readable under the profile, and
TESTING.md is not one of the session's files. Remove the sandbox after
the run: a second run needs a new one.
