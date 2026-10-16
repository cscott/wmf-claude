# From upstream: what to adopt

Upstream wmf-claude `main` 70712f6..21bc986 (2026-10-16) adds the Lima VM
work, `claude-vm` with its two modes, and a few general changes.
`lima-port` is rebased on it. `upstream-review.md` is the full review,
with file and line references (written at 835242c, before the rebase;
its "breaks us" items are done in the rebase and in 9339a31).

These items are not done. Each one is a to-do (`NOTES.md`, "To do").
Do them in this order, and ask the engineer first where an item says so.

## Terms: upstream's modes, and ours

Upstream's `claude-vm create` makes one of two kinds of VM
(`docs/lima-vm.md`):

- **Isolated**: nono in a VM. The VM has the internet (but not the host,
  the LAN or private ranges), and the model token is in the VM. It stops
  a nono escape from landing on the engineer's machine. It does not limit
  egress.
- **Contained**: nono in a VM whose network is locked. The only way out
  is the model API, through a proxy on the host that holds the token and
  refuses requests that make the API fetch for the agent (server tools,
  the MCP connector, URL content sources). No MCP servers: the lock
  drops their traffic to Phabricator, Gerrit and GitLab.

wmf-sbx is neither. On QEMU (the default), the VM has no route; its one
way out is the host's egress proxy, an allowlist that root in the guest
cannot change (the profile's hosts, the package registries, and what
`wmf-sbx allow` adds). The token stays on the host (credproxy, authproxy):
the VM gets a phantom. The MCP servers work. So: the credential is
handled as in upstream's contained mode, and the egress is an allowlist,
narrower than isolated and wider than contained. On `vz` or with
`--no-egress-proxy`, the guest's own nftables and nono enforce the
allowlist, as in an isolated VM with a narrower network.

**Name clash:** `sbx/README.md` ("Two modes"), `status.py`, `session.py`,
`resume.py` and `vm.py` call our non-sudo mode "contained". That is not
upstream's contained mode. Rename ours (for example "nono mode", against
"sudo mode"), and say in `sbx/README.md` how wmf-sbx compares with
upstream's two modes.

## 1. Small fixes

- **Project directory names over 200 characters.** `resume.py:121-124`
  encodes the project directory without Claude Code's rule for long
  paths: cut to 200 characters, then `-` and Java's string hash of the
  path in base 36. `lima/claude-context.py enc` has it, checked against a
  real directory. Use it.
- **`WMF_CLAUDE_PHAB_USER`.** Upstream's `bin/wmf-claude-setup` takes the
  Phabricator username from it, with no prompt. `session.wmf_claude_setup`
  can pass it, and stop writing `config.json` into the VM and piping
  `"\n"`. Then withdraw !131, or keep it only for a host where
  `claude mcp get` fails (`create.py:495-508` reads that output).
- **Terminal variables (3982f3a).** The profile now passes
  `TERM_PROGRAM`, `TERM_PROGRAM_VERSION` and `COLORTERM`. Copy them from
  the host's environment into the session's `env` (`session.py`).
- **`/etc/php` (4f36b8a).** Upstream's profile now grants a read of
  `/etc/php`. When the image's nono profile has it, remove it from our
  `READ_GRANTS`.

## 2. Report uncommitted work

Upstream's `claude-vm claude` lists, at the end of a session, what is not
pulled yet, including uncommitted files (61dd761, ea18f80). We report
unfetched branch tips (`remotes.unfetched_tips`, in `rm` and `ls`) but not
uncommitted files. Add one read-only `git status --porcelain` pass, as the
agent, to `rm` and to the end of a session. File names come from the
guest: strip control characters before printing them, as upstream does.

## 3. Carry Claude Code memory and sessions

`lima/claude-context.py` (a59ed4b) copies Claude Code's memory and
sessions between host and VM, with name checks, a three-way merge for
memory, and a prefix rule for sessions. wmf-sbx has nothing like it:
`resume.py` resumes inside the VM only. Our repos are at the same paths
as the engineer's, so only the home prefix differs and no path map is
necessary. Call it from `create`/`resume` (push) and from `sbx-pull`/`rm`
(pull: stage only, as upstream does; never write host memory).

## 4. Firewall: `ct direction reply`

Upstream's lock accepts `ct state established,related ct direction reply`
(c907cbd). Our host block uses the plain form
(`sandbox-provision.sh:103,117`). The change stops a connection that
started before the rules loaded from surviving them. Our rules load at
boot, so the gain is small. Test it in a sandbox (RAN) before adopting it.

## 5. Ask the engineer first

- **Headless Chromium (d7dfe34, `lima/guest-chrome.sh`).** Upstream runs
  Chromium as its own user, with a per-uid nftables rule, for the
  chrome-devtools MCP. It would give us Tier 2 manual testing, which
  `WMF_CLAUDE_SKIP_CHROME=1` turns off now. Needs an image user and a
  rule in `sandbox-provision.sh`. Medium effort.
- **Request-body checks (c907cbd, `bin/wmf-claude-model-proxy`).**
  `authproxy.py` blocks only the `/v1/mcp_servers` paths. A
  `/v1/messages` body can still name MCP servers or server tools, and
  through them Anthropic's servers fetch URLs that our egress allowlist
  does not see. Refusing server tools also stops Claude Code's
  WebSearch. A first step: refuse the `mcp_servers` field only, or make
  the full check a setting.

## Keep ours (overlaps)

- Upstream's contained-mode proxy and our credproxy/authproxy: both keep
  the token on the host. Keep ours; take the policy ideas in item 5.
- Upstream's `push`/`pull --nested` (bundles) and our `wmfsbx://`
  remotes: in both, the host chooses the names. Do not converge.
- Upstream's PHP, Composer and MariaDB in the guest, and our golden
  image (php-ast as CI builds it, a SQLite wiki). Keep ours.
- Upstream's `list` and `--vm`, and our `ls`, `status` and the other
  verbs: parallel CLIs.
