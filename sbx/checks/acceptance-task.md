<!--
The blind acceptance run: a regression test of a sandbox and of the
agent's instructions, not of the code in it. A new sandbox, a fresh
Claude with only its own instructions and this task list, and its
report. Every step that the agent had to invent is a documentation bug.
Run it ON THE HOST, from the wmf-claude checkout, after a change to the
image, the session or the agent's guides:

  wmf-sbx create --no-start --reset-all --name sbx-acceptance --memory 8GiB --disk 60GiB \
    gerrit:mediawiki/extensions/Translate \
    gerrit:mediawiki/extensions/Cite \
    gerrit:mediawiki/services/parsoid
  limactl shell --workdir / wmf-sbx-sbx-acceptance -- \
    sudo -u agent sh -c 'cat > /home/agent/TESTING.md' < sbx/checks/acceptance-task.md
  wmf-sbx resume sbx-acceptance -- --read-file=/home/agent/TESTING.md -- \
    -p "Read ~/TESTING.md and do what it says. This session is not interactive: \
  it ends when you end a turn and no Monitor is running. While you wait for \
  background work, keep a Monitor running that reports when that work ends." \
    --allowedTools "Bash Read Write Edit Glob Grep TodoWrite Monitor TaskOutput TaskStop" \
    --max-turns 400
  git -C <your Translate checkout> fetch sbx-acceptance
  git -C <your Translate checkout> show sbx-acceptance/sandbox-report:REPORT.md
  wmf-sbx rm sbx-acceptance

- Keep the sizes explicit: Translate's phan peaks at about 2.7 GB and is
  killed for lack of memory in a 4 GiB VM.
- `--read-file`: the profile does not let the session read the agent's
  home, and TESTING.md is not one of the session's files.
- In `-p` mode the session ends when the agent ends a turn with no
  Monitor running; a background Bash job does not keep it open. So the
  prompt says this, and `--allowedTools` has Monitor, TaskOutput and
  TaskStop.
- Under QEMU without KVM a run takes 5 to 8 hours. A second run needs a
  new sandbox.

To test a change to wmf-sbx itself, run the same commands in a `--sudo
--nested` sandbox of wmf-claude (README.md, "Developing wmf-claude"),
with that clone's `sbx/bin/wmf-sbx` in place of `wmf-sbx`. Make the
outer sandbox big enough for the session and the inner 8 GiB VM:

  wmf-sbx create --no-start --sudo --nested --name wmf-acceptance \
    --memory 14GiB --disk 120GiB /path/to/wmf-claude
-->
# Testing this sandbox

This is a test of the sandbox and of its instructions, not of the code in
it. Do each item below and report what happened. Do not fix the code in
the repos.

1. **Report the workspace.** Which repos are here, which branch and which
   commit each one is on, and whether any has uncommitted changes.
2. **Read your own instructions first.** `~/.claude/CLAUDE.md` and whatever
   it points you at. Say what you read and whether the pointers resolved.
3. **Run the tests, following the instructions you already have.** For each
   repo in the workspace, run every test suite the instructions say applies
   to it: PHP tests, PHP lint and static analysis, JS lint, JS unit tests,
   browser tests and API tests. Report, per suite, the exact command, the
   pass and fail counts, the run time, and the first failure in full if
   there is one.
4. **Say which suites do not apply**, and why, for each repo.
5. **Start the wiki and check it answers**, over HTTP, both a page and the
   action API.
6. **Do the browser suites too.** If a browser is not installed, follow the
   instructions to install one, then run them.
7. **Do not work around anything silently.** If a command in your
   instructions fails, or you had to do something the instructions do not
   mention, that is the most valuable thing in your report. Record the
   command, the output and what you did instead.
8. **Write `REPORT.md`** at the top of your primary repo (the directory
   you start in), on a new branch `sandbox-report`, and commit it there,
   so the engineer can fetch it.

`REPORT.md` must contain, in this order:

- a table of every suite you ran: repo, command, result (pass and fail
  counts), time;
- every command that failed, with its output;
- every step you had to invent: anything you did that your instructions
  did not tell you to do;
- anything in `~/MEDIAWIKI-TESTING.md`, `~/.claude/CLAUDE.md` or your
  session-start context that was wrong, missing or misleading, quoted;
- every request that the network or the sandbox refused, with the host or
  the path;
- the time from the start of the session to the first green test run;
- a plain answer to "could you have done this without inventing any step?"
