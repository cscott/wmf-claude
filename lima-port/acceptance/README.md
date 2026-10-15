# The blind acceptance run (phase 7)

`TESTING.md` is the task list for the blind acceptance run of a Lima
sandbox: a new sandbox, a fresh Claude with only its own instructions and
this task list, and its report. Every step it had to invent is a
documentation bug. On the host:

```bash
wmf-sbx create --reset-all --name sbx-testverify --memory 8GiB --disk 60GiB \
  gerrit:mediawiki/extensions/Translate \
  gerrit:mediawiki/extensions/Cite \
  gerrit:mediawiki/services/parsoid
limactl shell --workdir / wmf-sbx-sbx-testverify -- \
  sudo -u agent sh -c 'cat > /home/agent/TESTING.md' < lima-port/acceptance/TESTING.md
wmf-sbx resume sbx-testverify -- --read-file=/home/agent/TESTING.md -- \
  -p "Read ~/TESTING.md and do what it says. This session is not interactive: \
it ends when you end a turn and no Monitor is running. While you wait for \
background work, keep a Monitor running that reports when that work ends." \
  --allowedTools "Bash Read Write Edit Glob Grep TodoWrite Monitor TaskOutput TaskStop" \
  --max-turns 400
git -C ~/<Translate checkout> fetch sbx-testverify
git -C ~/<Translate checkout> show sbx-testverify/sandbox-report:REPORT.md
```

`--memory 8GiB --disk 60GiB`: the run needs this much, whatever the
defaults are. Translate's phan peaks at about 2.7 GB and was OOM-killed
in a 4 GiB VM (RAN 2026-10-14). Keep the sizes explicit, so a smaller
default does not break the run.

**Nested**: the agent in a `--sudo --nested` sandbox of
wmf-claude changes its own clone, then runs the commands above with that
clone's `sbx/bin/wmf-sbx` in place of `wmf-sbx`, against the changed
copy. Make the outer sandbox big enough for the session and the inner
8 GiB VM, also with explicit sizes:

```bash
wmf-sbx create --sudo --nested --name egsn --memory 14GiB --disk 120GiB \
  ~/Projects/Wikimedia/wmf-claude
```

`--read-file`: the agent's home is not readable under the profile, and
TESTING.md is not one of the session's files. In `-p` mode the session
ends when the agent ends a turn with no Monitor running. A background
Bash job (`run_in_background`) does not keep the session open. RAN
2026-10-14: the agent stopped its Monitors, waited on two background
jobs, and the run ended after 9 minutes with no report. So the prompt
says this, and `--allowedTools` includes `Monitor`, `TaskOutput` and
`TaskStop`. Under QEMU without KVM a run takes 5 to 8 hours and,
with `-p`, costs about $20 (sbx/NOTES.md). Remove the sandbox after
the run: a second run needs a new one.
