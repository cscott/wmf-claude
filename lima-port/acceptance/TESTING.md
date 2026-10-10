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
