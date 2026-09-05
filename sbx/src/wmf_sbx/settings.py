"""Host `sbx settings` preflight checks for wmf-sbx-create.

Several sbx daemon settings are preconditions this repo's own security
analysis depends on (see sbx/SECURITY.md §2, §7.5/§7.5.1 and
sbx/NOTES.md §78) -- `wmf-sbx-create` never checked them itself, so a
host reconfigured away from the expected values either silently widens
the SSH attack surface or fails deep inside `sbx create` with an error
this tool never explains. `read_settings`/`preflight` make that check
automatic instead of a manual "check them before trusting this
section" reminder.
"""

import json
import subprocess

_KIT_DOC = "sbx/NOTES.md §78.3"
_PROXY_DOC = "sbx/SECURITY.md §7.5.1"


def read_settings(wmf_sbx, run=subprocess.run):
    """{key: value} from `wmf-sbx settings list --json`, or None if it
    could not be read (old sbx without the command, daemon unreachable,
    unparseable output, ...). Best-effort, same shape as
    create.existing_sandbox_names: a failure here must not block sandbox
    creation on our own ability to introspect the host.

    `stdin=DEVNULL` for the same reason as every other non-attach `sbx`
    call in create.py (see existing_sandbox_names): a call none of us
    expect to be interactive must not hang on a stray prompt sent into a
    buffer nobody reads."""
    try:
        result = run(
            [wmf_sbx, "--upstream", "settings", "list", "--json"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        rows = json.loads(result.stdout or "")
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    return {row["key"]: row.get("value") for row in rows
            if isinstance(row, dict) and isinstance(row.get("key"), str)}


def preflight(values, *, check_kit=True):
    """(warnings, errors) from a {key: value} settings dict -- plain
    sentences, no `warning:`/`error:` prefix (create.py's main() adds
    that once, at the print site, matching every other warning/error in
    that file).

    check_kit=False when the caller passed an explicit --kit: the two
    kit settings gate whether *our generated* kit (a local, unsigned
    temp-dir kit) will be accepted, which says nothing about a kit the
    user supplied themselves -- same carve-out already used in main()
    for --reset-all/--no-mcp/--kit-out ('no effect with an explicit
    --kit')."""
    warnings = []
    errors = []

    if values.get("ssh.agentForwardingEnabled"):
        warnings.append(
            "sbx forwards this host's real SSH agent into every sandbox "
            "(ssh.agentForwardingEnabled=true). The wmf-sbx wrapper's "
            "`unset SSH_AUTH_SOCK` protects sandboxes created through it, "
            "but not a daemon that already has an agent forwarded from "
            "somewhere else -- see sbx/SECURITY.md §2. "
            "`wmf-sbx settings set ssh.agentForwardingEnabled false` "
            "closes this daemon-wide, but needs `sbx daemon restart` to "
            "take effect on an already-tainted daemon, and that restart "
            "drops every running sandbox's session state -- schedule it, "
            "don't run it reflexively."
        )

    if check_kit and values.get("kit.allowLocalKits") is False:
        errors.append(
            "kit.allowLocalKits is false, so sbx will refuse the "
            "MediaWiki kit this tool generates in a temp directory on "
            f"every create. Set it to true (`wmf-sbx settings set "
            "kit.allowLocalKits true`), or always pass an explicit --kit "
            f"from a source this host allows -- see {_KIT_DOC}."
        )

    if check_kit and values.get("kit.requireSignature"):
        errors.append(
            "kit.requireSignature is true, so sbx will refuse to install "
            "any kit without a trusted signature -- the kit this tool "
            "generates is never signed. Set it to false (`wmf-sbx "
            "settings set kit.requireSignature false`), or always pass "
            f"an explicit, signed --kit -- see {_KIT_DOC}."
        )

    no_proxy_sandbox = values.get("no_proxy.sandbox")
    if no_proxy_sandbox:
        errors.append(
            f"no_proxy.sandbox is {no_proxy_sandbox!r}, not empty. "
            "sbx/SECURITY.md §7.5's MCP credential-injection argument "
            "depends on it being empty -- an entry here is an exception "
            "to that interception for those hosts. Clear it (`wmf-sbx "
            f"settings set no_proxy.sandbox ''`) -- see {_PROXY_DOC}."
        )

    proxy_sandbox = values.get("proxy.sandbox")
    if proxy_sandbox:
        errors.append(
            f"proxy.sandbox is {proxy_sandbox!r}, not empty. "
            f"{_PROXY_DOC} treats this and no_proxy.sandbox as the "
            "preconditions of the whole MCP credential-injection "
            "argument in sbx/SECURITY.md §7.5 -- a non-default sandbox "
            "proxy has not been checked against that section's "
            "reasoning. Clear it (`wmf-sbx settings set proxy.sandbox "
            f"''`) -- see {_PROXY_DOC}."
        )

    return warnings, errors
