# The blind acceptance run (phase 7)

`TESTING.md` is the task list for the blind acceptance run of a Lima
sandbox: a new sandbox, a fresh Claude with only its own instructions and
this task list, and its report. Every step it had to invent is a
documentation bug. On the host:

```bash
wmf-sbx create --reset-all --name sbx-testverify \
  gerrit:mediawiki/extensions/Translate \
  gerrit:mediawiki/extensions/Cite \
  gerrit:mediawiki/services/parsoid
limactl shell --workdir / wmf-sbx-sbx-testverify -- \
  sudo -u agent sh -c 'cat > /home/agent/TESTING.md' < lima-port/acceptance/TESTING.md
wmf-sbx resume sbx-testverify --read-file=/home/agent/TESTING.md -- \
  -p "Read ~/TESTING.md and do what it says." \
  --allowedTools "Bash Read Write Edit Glob Grep TodoWrite Monitor TaskOutput TaskStop" \
  --max-turns 400
git -C ~/<Translate checkout> fetch sbx-testverify
git -C ~/<Translate checkout> show sbx-testverify/sandbox-report:REPORT.md
```

`--read-file`: the agent's home is not readable under the profile, and
TESTING.md is not one of the session's files. Without `Monitor`,
`TaskOutput` and `TaskStop`, the run ends when the agent waits on a
background job. Under QEMU without KVM a run takes 5 to 8 hours and,
with `-p`, costs about $20 (sbx/NOTES.md). Remove the sandbox after
the run: a second run needs a new one.
