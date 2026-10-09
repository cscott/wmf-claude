#!/usr/bin/env python3
"""Unit tests for wmf_sbx_create.py -- run with:
  python3 -m unittest discover -s sbx/tests -v
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.create as c  # noqa: E402
import wmf_sbx.kit as kit_mod  # noqa: E402
import wmf_sbx.resolve as r  # noqa: E402


def patch_offline(testcase):
    """No unit test may reach the network. main() runs the dependency walk,
    which fetches manifests from gitiles for any repo that isn't cloned --
    and Wikimedia's edge answers a burst of those with 429 + retry-after,
    so an unpatched test suite is both non-hermetic and slow."""
    patcher = mock.patch.object(
        c.deps_mod, "fetch_manifest",
        lambda canonical, warn=None, **kw: None,
    )
    patcher.start()
    testcase.addCleanup(patcher.stop)


def patch_host_probes(testcase, settings=True, skills=True):
    """No main() test may read the state of the host it runs on. main()
    runs `wmf-sbx settings list`, `sbx create --help` and the host MCP
    preflight, and all three call real programs. In a sandbox there is no
    `sbx`, so they get no answer. On a host they read the real daemon,
    the built submodules and the `node` on PATH, and a node below the
    floor stops the create. Stub the MCP preflight to "no servers". Stub
    the settings read to "could not read", and the skills probe to the
    flag that current sbx takes. Set `settings` or `skills` to False when
    the test gives main() a `run` that answers that call."""
    patchers = [mock.patch.object(
        c, "ensure_host_mcp_servers", lambda **kw: [])]
    if settings:
        patchers.append(mock.patch.object(
            c.settings_mod, "read_settings", lambda *a, **kw: None))
    if skills:
        patchers.append(mock.patch.object(
            c, "supported_skills_flag", lambda **kw: "--skills=off"))
    for patcher in patchers:
        patcher.start()
        testcase.addCleanup(patcher.stop)


class CloneUrlTests(unittest.TestCase):
    def test_gerrit(self):
        self.assertEqual(
            c.clone_url("gerrit:mediawiki/extensions/Cite"),
            "https://gerrit.wikimedia.org/r/mediawiki/extensions/Cite",
        )

    def test_gitlab(self):
        self.assertEqual(
            c.clone_url("gitlab:daniel/claudebox"),
            "https://gitlab.wikimedia.org/daniel/claudebox.git",
        )

    def test_unknown_scheme(self):
        with self.assertRaises(c.LaunchError):
            c.clone_url("svn:foo/bar")


class UpstreamPlanTests(unittest.TestCase):
    def test_maps_what_it_can_and_falls_back_to_host_origin(self):
        # No fake `run` needed: a known canonical is resolved via
        # clone_url() alone, without ever touching the host directory.
        run = lambda argv, **kw: FakeCompletedProcess(1)
        plan = c.upstream_plan([
            ("gerrit:mediawiki/skins/MinervaNeue", "/home/c/Minerva"),
            ("svn:foo/bar", "/home/c/foo"),   # no URL builder -> falls back
            (None, "/home/c/scratch"),        # raw path, no canonical -> falls back
        ], run=run)
        # foo/scratch fall back to host_upstream_url, which also fails
        # here (no origin), so they stay absent: build_plan treats a
        # missing entry as "leave the host mirror as origin".
        self.assertEqual(plan, {
            "/home/c/Minerva":
                "https://gerrit.wikimedia.org/r/mediawiki/skins/MinervaNeue",
        })

    def test_falls_back_to_host_origin_for_unidentified_repos(self):
        # A GitLab (or any other) clone with no canonical and no exact
        # repos.yaml rule -- but the host directory itself already knows
        # its own origin, no forge-specific lookup required.
        def run(argv, **kw):
            self.assertEqual(argv, ["git", "-C", "/home/c/claudebox", "remote", "get-url", "origin"])
            return FakeCompletedProcess(0, stdout="https://gitlab.wikimedia.org/daniel/claudebox.git\n")

        plan = c.upstream_plan([(None, "/home/c/claudebox")], run=run)
        self.assertEqual(plan, {
            "/home/c/claudebox": "https://gitlab.wikimedia.org/daniel/claudebox.git",
        })

    def test_rejects_non_http_host_origin(self):
        # No SSH agent inside the sandbox (sbx/NOTES.md #8) -- an
        # ssh://, git@host:path, or local-path origin isn't fetchable
        # from inside it, so it must not be used as `origin` there.
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout="git@gitlab.wikimedia.org:daniel/claudebox.git\n")
        plan = c.upstream_plan([(None, "/home/c/claudebox")], run=run)
        self.assertEqual(plan, {})

    def test_host_dir_not_a_git_repo(self):
        run = lambda argv, **kw: FakeCompletedProcess(1, stderr="not a git repository")
        plan = c.upstream_plan([(None, "/home/c/scratch")], run=run)
        self.assertEqual(plan, {})

    def test_gitlab_fork_is_repointed_at_its_true_upstream(self):
        run = lambda argv, **kw: FakeCompletedProcess(1)
        plan = c.upstream_plan(
            [("gitlab:cscott/wmf-claude", "/home/c/wmf-claude")], run=run,
            gitlab_upstream=lambda path: "repos/psi/wmf-claude")
        self.assertEqual(plan, {
            "/home/c/wmf-claude": "https://gitlab.wikimedia.org/repos/psi/wmf-claude.git"})

    def test_gitlab_outage_keeps_the_canonicals_own_url(self):
        def down(path):
            raise c.resolve_mod.ResolutionError("offline")
        run = lambda argv, **kw: FakeCompletedProcess(1)
        plan = c.upstream_plan(
            [("gitlab:cscott/wmf-claude", "/home/c/wmf-claude")], run=run,
            gitlab_upstream=down)
        self.assertEqual(plan, {
            "/home/c/wmf-claude": "https://gitlab.wikimedia.org/cscott/wmf-claude.git"})


class HostUpstreamUrlTests(unittest.TestCase):
    def test_returns_http_origin(self):
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout="https://example.org/foo.git\n")
        self.assertEqual(c.host_upstream_url("/home/c/foo", run=run), "https://example.org/foo.git")

    def test_none_on_failure(self):
        run = lambda argv, **kw: FakeCompletedProcess(1)
        self.assertIsNone(c.host_upstream_url("/home/c/foo", run=run))

    def test_none_on_ssh_origin(self):
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout="git@example.org:foo.git\n")
        self.assertIsNone(c.host_upstream_url("/home/c/foo", run=run))

    def test_none_on_oserror(self):
        def run(argv, **kw):
            raise OSError("no such file or directory: git")
        self.assertIsNone(c.host_upstream_url("/home/c/foo", run=run))


class DefaultSandboxNameTests(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(c.default_sandbox_name("gerrit:mediawiki/extensions/Cite"), "sbx-cite")

    def test_sanitizes_punctuation(self):
        self.assertEqual(
            c.default_sandbox_name("gerrit:mediawiki/services/parsoid_js"), "sbx-parsoid-js"
        )


class ExistingSandboxNamesTests(unittest.TestCase):
    # `wmf-sbx ls --json` on cananian's host, trimmed to two sandboxes and
    # otherwise verbatim (MEASURED, sbx/NOTES.md §75.4). A stopped sandbox
    # still occupies its name, which is the whole point of the check.
    LS_JSON = json.dumps({"sandboxes": [
        {"name": "sbx-cite",
         "id": "06f806a4-0101-4535-b25f-6002df7ed68f",
         "agent": "claude", "status": "stopped",
         "workspaces": ["/home/cananian/Projects/Wikimedia/Extensions/Cite",
                        "/home/cananian/Projects/Wikimedia/core:ro"]},
        {"name": "wmf-claude-sbx",
         "id": "8178072c-1d38-4055-8736-5412fa7030df",
         "agent": "claude", "status": "running",
         "ports": [{"host_ip": "127.0.0.1", "host_port": 32773,
                    "sandbox_port": 9977, "protocol": "tcp4"}],
         "workspaces": ["/home/cananian/Projects/Wikimedia/wmf-claude"]},
    ]})

    def test_parses_the_json_listing(self):
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout=self.LS_JSON)
        self.assertEqual(c.existing_sandbox_names(run=run),
                         {"sbx-cite", "wmf-claude-sbx"})

    def test_uses_wmf_sbx_ls_json(self):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            return FakeCompletedProcess(0, stdout='{"sandboxes": []}')

        c.existing_sandbox_names(run=run)
        # stdin=DEVNULL so an unexpected interactive prompt from `sbx`
        # itself (e.g. a version-mismatch restart prompt) fails fast with
        # EOF instead of hanging invisibly -- MEASURED, cananian, 2026-09-08.
        self.assertEqual(
            calls,
            [([c.WMF_SBX, "--upstream", "ls", "--json"],
              {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL})],
        )

    def test_output_that_does_not_parse_returns_empty_set(self):
        # Unreadable is not a reason to refuse to create: `sbx create` is
        # the final authority on a name collision either way. Contrast
        # host_mcp_registrations, where unreadable has to stay unreadable.
        for stdout in ("mw-cite\nclaude-ve\n", "<html>", "",
                       '{"sandboxes": "not a list"}'):
            with self.subTest(stdout=stdout):
                run = lambda argv, **kw: FakeCompletedProcess(0, stdout=stdout)
                self.assertEqual(c.existing_sandbox_names(run=run), set())

    def test_a_sandbox_with_no_name_is_skipped(self):
        listing = json.dumps({"sandboxes": [{"status": "running"},
                                            {"name": "sbx-cite"}]})
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout=listing)
        self.assertEqual(c.existing_sandbox_names(run=run), {"sbx-cite"})

    def test_command_failure_returns_empty_set(self):
        run = lambda argv, **kw: FakeCompletedProcess(1, stdout="")
        self.assertEqual(c.existing_sandbox_names(run=run), set())

    def test_missing_binary_returns_empty_set(self):
        def run(argv, **kw):
            raise FileNotFoundError("no such file")

        self.assertEqual(c.existing_sandbox_names(run=run), set())


class UniqueSandboxNameTests(unittest.TestCase):
    def test_no_collision_leaves_name_unchanged_and_never_prompts(self):
        def prompt(_msg):
            self.fail("should not prompt when there's no collision")

        self.assertEqual(c.unique_sandbox_name("mw-cite", set(), prompt=prompt), "mw-cite")

    def test_empty_response_accepts_first_free_suffix(self):
        name = c.unique_sandbox_name("mw-cite", {"mw-cite"}, prompt=lambda _msg: "")
        self.assertEqual(name, "mw-cite-2")

    def test_suggestion_skips_taken_suffixes(self):
        existing = {"mw-cite", "mw-cite-2", "mw-cite-3"}
        name = c.unique_sandbox_name("mw-cite", existing, prompt=lambda _msg: "")
        self.assertEqual(name, "mw-cite-4")

    def test_explicit_response_is_used_as_is(self):
        name = c.unique_sandbox_name("mw-cite", {"mw-cite"}, prompt=lambda _msg: "custom-name")
        self.assertEqual(name, "custom-name")

    def test_reprompts_if_the_typed_name_also_collides(self):
        responses = iter(["mw-cite-2", "mw-final"])
        name = c.unique_sandbox_name(
            "mw-cite", {"mw-cite", "mw-cite-2"}, prompt=lambda _msg: next(responses)
        )
        self.assertEqual(name, "mw-final")

    def test_response_is_stripped(self):
        name = c.unique_sandbox_name("mw-cite", {"mw-cite"}, prompt=lambda _msg: "  custom  ")
        self.assertEqual(name, "custom")


class FindNestedMountConflictTests(unittest.TestCase):
    def test_no_conflict_for_siblings(self):
        self.assertIsNone(c.find_nested_mount_conflict(["/w/Cite", "/w/core", "/w/Skins"]))

    def test_extra_inside_primary_is_a_conflict(self):
        self.assertEqual(
            c.find_nested_mount_conflict(["/w/Wikimedia/wmf-claude", "/w/Wikimedia"]),
            ("/w/Wikimedia", "/w/Wikimedia/wmf-claude"),
        )

    def test_primary_inside_extra_is_a_conflict_regardless_of_order(self):
        self.assertEqual(
            c.find_nested_mount_conflict(["/w/Wikimedia", "/w/Wikimedia/wmf-claude"]),
            ("/w/Wikimedia", "/w/Wikimedia/wmf-claude"),
        )

    def test_identical_paths_are_not_flagged_as_nested(self):
        # Duplicate mounts aren't the nesting bug being guarded against here.
        self.assertIsNone(c.find_nested_mount_conflict(["/w/core", "/w/core"]))

    def test_similar_prefix_that_is_not_a_real_subdirectory_is_not_a_conflict(self):
        # "/w/core2" is not inside "/w/core" -- must not match on a bare
        # string prefix without the separator.
        self.assertIsNone(c.find_nested_mount_conflict(["/w/core", "/w/core2"]))


class ModuleDerivedPathsTests(unittest.TestCase):
    """sbx/NOTES.md §66: the paths the code works out about itself.

    These pass trivially on a checkout reached through no symlink, and
    that is fine -- they exist to fire on the hosts where it matters,
    where `~/Wikimedia` is a symlink to `~/Projects/Wikimedia` and
    `wmf-sbx-create` is therefore found through the symlinked name.
    `abspath` would keep the link; a mount, or a stored `sbx mcp add`
    registration, must not.
    """

    def test_no_module_derived_path_carries_a_symlink(self):
        for label, path in [
            ("create._SBX_ROOT", c._SBX_ROOT),
            ("create.WMF_SBX", c.WMF_SBX),
            ("create.MCP_REPO_ROOT", c.MCP_REPO_ROOT),
            ("kit.SBX_ROOT", kit_mod.SBX_ROOT),
            ("kit.REPO_ROOT", kit_mod.REPO_ROOT),
            ("kit.MCP_PROXY_SOURCE", kit_mod.MCP_PROXY_SOURCE),
            ("kit.STATIC_SETUP_SCRIPT", kit_mod.STATIC_SETUP_SCRIPT),
        ]:
            with self.subTest(label):
                self.assertEqual(path, os.path.realpath(path))


class BuildSbxCommandTests(unittest.TestCase):
    def test_no_kit_no_extras(self):
        self.assertEqual(
            c.build_sbx_command("mw-cite", None, "/w/Cite", []),
            [c.WMF_SBX, "--upstream", "create", "--name", "mw-cite", "claude", "/w/Cite"],
        )

    def test_with_kit_and_extras(self):
        cmd = c.build_sbx_command("mw-cite", "/w/mediawiki-kit", "/w/Cite", ["/w/core", "/w/Skins"])
        self.assertEqual(
            cmd,
            [
                c.WMF_SBX, "--upstream", "create", "--name", "mw-cite",
                "--kit", "/w/mediawiki-kit",
                "claude", "/w/Cite",
                "/w/core:ro", "/w/Skins:ro",
            ],
        )

    def test_every_extra_gets_ro_suffix_regardless_of_user_suffix(self):
        # Nothing should ever write to a host-mirrored original, and
        # create-time ':ro' is the only layer that enforces that --
        # wmf-sbx-setup's in-sandbox remount is escapable with `sudo mount
        # -o remount,rw` (see sbx/NOTES.md #22). The user's own ':ro' only
        # picks the parallel-tree strategy, and never reaches here.
        cmd = c.build_sbx_command("mw-cite", None, "/w/Cite", ["/w/core", "/w/Skins"])
        self.assertEqual(
            cmd,
            [
                c.WMF_SBX, "--upstream", "create", "--name", "mw-cite",
                "claude", "/w/Cite",
                "/w/core:ro", "/w/Skins:ro",
            ],
        )

    def test_primary_never_gets_ro_suffix(self):
        # `sbx create` rejects ':ro' on the primary workspace outright
        # ("ERROR: primary workspace must be read/write"); main() warns
        # separately rather than emitting a suffix that would just fail.
        cmd = c.build_sbx_command("mw-cite", None, "/w/Cite", [])
        self.assertEqual(cmd[-1], "/w/Cite")

    def test_invokes_wmf_sbx_wrapper_not_sbx_directly(self):
        # See sbx/NOTES.md #8: sbx forwards the host SSH agent to every
        # sandbox unless SSH_AUTH_SOCK is stripped for every invocation,
        # forever, which is what the wmf-sbx wrapper guarantees and a
        # bare "sbx" call does not.
        cmd = c.build_sbx_command("mw-cite", None, "/w/Cite", [])
        self.assertTrue(os.path.isfile(c.WMF_SBX), f"{c.WMF_SBX} should exist")
        self.assertEqual(os.path.basename(cmd[0]), "wmf-sbx")


    def test_static_mcp_goes_before_the_agent_positional(self):
        # --static-mcp mounts the host-side servers at creation *and*
        # deletes mcp-add/mcp-find/mcp-config-set from the gateway
        # (sbx/NOTES.md §61.3) -- a capability removed, not hidden.
        cmd = c.build_sbx_command("mw-cite", None, "/w/Cite", [],
                                  static_mcp=["phabricator", "gerrit"])
        self.assertEqual(
            cmd,
            [
                c.WMF_SBX, "--upstream", "create", "--name", "mw-cite",
                "--static-mcp", "phabricator,gerrit",
                "claude", "/w/Cite",
            ],
        )

    def test_no_servers_means_no_flag(self):
        # An unregistered name fails the whole create with `400 Bad
        # Request: unknown --static-mcp server(s): ...` (§59.2), so an
        # empty list must not become `--static-mcp ""`.
        self.assertNotIn("--static-mcp",
                         c.build_sbx_command("mw-cite", None, "/w/Cite", []))

    def test_the_skills_flag_goes_before_the_agent_positional(self):
        # Shared agent-skills store: one host directory mounted into every
        # sandbox, holding instructions a model acts on (§77.2-§77.4).
        cmd = c.build_sbx_command("mw-cite", None, "/w/Cite", [],
                                  skills_flag="--skills=off")
        self.assertEqual(
            cmd,
            [
                c.WMF_SBX, "--upstream", "create", "--name", "mw-cite",
                "--skills=off",
                "claude", "/w/Cite",
            ],
        )

    def test_the_deprecated_spelling_is_passed_through_as_given(self):
        # supported_skills_flag decides which spelling this sbx takes;
        # build_sbx_command must not second-guess it.
        self.assertIn(
            "--no-share-skills",
            c.build_sbx_command("mw-cite", None, "/w/Cite", [],
                                skills_flag="--no-share-skills"),
        )

    def test_no_skills_flag_means_no_flag(self):
        # 0.42.1 has neither spelling in `create --help` (MEASURED §79.6).
        # An unknown flag would fail the whole create, so None must stay
        # out of the command line entirely rather than become an empty arg.
        cmd = c.build_sbx_command("mw-cite", None, "/w/Cite", [],
                                  skills_flag=None)
        self.assertNotIn("--skills=off", cmd)
        self.assertNotIn("--no-share-skills", cmd)
        self.assertNotIn(None, cmd)
        self.assertEqual(
            cmd,
            [c.WMF_SBX, "--upstream", "create", "--name", "mw-cite", "claude", "/w/Cite"],
        )


class SupportedSkillsFlagTests(unittest.TestCase):
    # Verbatim from cananian's host, 2026-09-14 (MEASURED §79.6): 0.42.1's
    # `sbx create --help` mentions no skills flag at all. Trimmed to the
    # flags block; the point is what is absent.
    HELP_0_42_1 = """\
Usage:
  sbx create [flags] AGENT|SANDBOX_KIT [PATH...]

Flags:
      --clone                    Run the agent on a private in-container clone
      --deny-network stringArray Add a per-sandbox network deny rule
  -e, --env stringArray          Set an environment variable in the sandbox
      --kit strings              (Experimental) Additional kit reference
      --name string              Name for the sandbox
      --static-mcp strings       MCP server names for the static MCP set
"""

    # 0.43.0-rc3's release notes: "`sbx create`/`sbx run` now share skills
    # read-only by default via a new tri-state
    # `--skills=off|readonly|readwrite` flag" (§81.1). The help line itself
    # is a plausible reconstruction, not a transcript -- what is asserted
    # here is only that the string `--skills` appears.
    HELP_0_43 = HELP_0_42_1.replace(
        "      --static-mcp strings",
        "      --skills string            Share skills: off|readonly|readwrite\n"
        "      --static-mcp strings",
    )

    def test_prefers_the_tri_state_flag_when_it_is_there(self):
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout=self.HELP_0_43)
        self.assertEqual(c.supported_skills_flag(run=run), "--skills=off")

    def test_off_not_readonly(self):
        # readonly protects the shared store from this sandbox; off
        # protects this sandbox from what another one planted there. We
        # want the second -- Route A means we neither read nor write it
        # (§81.1).
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout=self.HELP_0_43)
        self.assertNotIn("readonly", c.supported_skills_flag(run=run))

    def test_falls_back_to_the_deprecated_spelling(self):
        # 0.43 keeps --no-share-skills as an alias for --skills=off, and
        # some older build may have only the old one -- 0.42.1 carried it
        # undocumented (§81.1). Take whatever is actually offered.
        help_text = self.HELP_0_42_1.replace(
            "      --static-mcp strings",
            "      --no-share-skills          Do not share the skills directory\n"
            "      --static-mcp strings",
        )
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout=help_text)
        self.assertEqual(c.supported_skills_flag(run=run), "--no-share-skills")

    def test_0_42_1_has_neither(self):
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout=self.HELP_0_42_1)
        self.assertIsNone(c.supported_skills_flag(run=run))

    def test_reads_stderr_too(self):
        # Don't depend on which stream help lands on, or on the exit
        # status: a wrong answer here silently drops a security flag.
        run = lambda argv, **kw: FakeCompletedProcess(1, stdout="",
                                                      stderr=self.HELP_0_43)
        self.assertEqual(c.supported_skills_flag(run=run), "--skills=off")

    def test_probes_create_help_with_stdin_closed(self):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            return FakeCompletedProcess(0, stdout=self.HELP_0_43)

        c.supported_skills_flag(run=run)
        # stdin=DEVNULL for the same reason as existing_sandbox_names: a
        # stray prompt from `sbx` on a call nobody expects to be
        # interactive must fail fast, not hang invisibly.
        self.assertEqual(
            calls,
            [([c.WMF_SBX, "--upstream", "create", "--help"],
              {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL})],
        )

    def test_sbx_missing_entirely_is_not_fatal(self):
        def run(argv, **kw):
            raise OSError(2, "No such file or directory")

        self.assertIsNone(c.supported_skills_flag(run=run))


class BuildSbxRunCommandTests(unittest.TestCase):
    def test_basic(self):
        # wmf-sbx-resume, not `wmf-sbx run --name`: re-attaching has to
        # re-point the host remotes at the daemon's new published port.
        with mock.patch.object(c.shutil, "which", return_value=None):
            self.assertEqual(
                c.build_sbx_run_command("mw-cite"),
                [os.path.join(os.path.dirname(c.WMF_SBX), "wmf-sbx-resume"), "mw-cite"],
            )

    def test_prefers_the_bare_name_when_path_resolves_to_the_same_script(self):
        full_path = os.path.join(os.path.dirname(c.WMF_SBX), "wmf-sbx-resume")
        with mock.patch.object(c.shutil, "which", return_value=full_path):
            self.assertEqual(
                c.build_sbx_run_command("mw-cite"),
                ["wmf-sbx-resume", "mw-cite"],
            )

    def test_falls_back_to_the_full_path_when_path_resolves_elsewhere(self):
        # An unrelated or stale wmf-sbx-resume earlier on PATH must not be
        # recommended in place of the one this repo ships.
        full_path = os.path.join(os.path.dirname(c.WMF_SBX), "wmf-sbx-resume")
        with mock.patch.object(c.shutil, "which",
                                return_value="/usr/local/bin/wmf-sbx-resume"):
            self.assertEqual(
                c.build_sbx_run_command("mw-cite"),
                [full_path, "mw-cite"],
            )


class FakeCompletedProcess:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class SplitRoSuffixTests(unittest.TestCase):
    def test_strips_trailing_ro_suffix(self):
        self.assertEqual(c.split_ro_suffix("gerrit:mediawiki/core:ro"), ("gerrit:mediawiki/core", True))

    def test_raw_path_with_ro_suffix(self):
        self.assertEqual(c.split_ro_suffix("/w/Skins:ro"), ("/w/Skins", True))

    def test_no_suffix_leaves_spec_unchanged(self):
        self.assertEqual(c.split_ro_suffix("gerrit:mediawiki/core"), ("gerrit:mediawiki/core", False))

    def test_only_strips_one_trailing_occurrence(self):
        self.assertEqual(c.split_ro_suffix("Cite:ro:ro"), ("Cite:ro", True))


class LookupPublishedHostPortTests(unittest.TestCase):
    def test_returns_the_matching_ipv4_host_port(self):
        # A dual-stack publish lists both an IPv4 and an IPv6 entry sharing
        # the same host_port -- only the 127.0.0.1/tcp one matters here (see
        # publish_daemon_port).
        mappings = [
            {"host_ip": "127.0.0.1", "host_port": 54321, "sandbox_port": 9977, "protocol": "tcp"},
            {"host_ip": "::1", "host_port": 54321, "sandbox_port": 9977, "protocol": "tcp"},
        ]
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout=json.dumps(mappings))
        self.assertEqual(c.lookup_published_host_port("mw-cite", 9977, run=run), 54321)

    def test_uses_wmf_sbx_ports_json(self):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            return FakeCompletedProcess(0, stdout="[]")

        c.lookup_published_host_port("mw-cite", 9977, run=run)
        self.assertEqual(
            calls,
            [([c.WMF_SBX, "--upstream", "ports", "mw-cite", "--json"],
              {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL})],
        )

    def test_no_matching_mapping_returns_none(self):
        mappings = [{"host_ip": "127.0.0.1", "host_port": 1, "sandbox_port": 4444, "protocol": "tcp"}]
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout=json.dumps(mappings))
        self.assertIsNone(c.lookup_published_host_port("mw-cite", 9977, run=run))

    def test_command_failure_returns_none(self):
        run = lambda argv, **kw: FakeCompletedProcess(1, stdout="")
        self.assertIsNone(c.lookup_published_host_port("mw-cite", 9977, run=run))

    def test_invalid_json_returns_none(self):
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout="not json")
        self.assertIsNone(c.lookup_published_host_port("mw-cite", 9977, run=run))

    def test_a_tcp4_mapping_is_still_our_port(self):
        # sbx 0.42.0 made `tcp4` the default for `ports --publish` and for
        # kit-declared ports. Pinning `protocol == "tcp"` would have made
        # the daemon's port simply stop being found, and the symptom --
        # host remotes that don't fetch -- names nothing.
        mappings = [{"host_ip": "127.0.0.1", "host_port": 32784,
                     "sandbox_port": 9977, "protocol": "tcp4"}]
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout=json.dumps(mappings))
        self.assertEqual(c.lookup_published_host_port("mw-cite", 9977, run=run), 32784)

    def test_a_mapping_with_no_protocol_at_all_is_taken_as_ipv4(self):
        mappings = [{"host_ip": "127.0.0.1", "host_port": 32785, "sandbox_port": 9977}]
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout=json.dumps(mappings))
        self.assertEqual(c.lookup_published_host_port("mw-cite", 9977, run=run), 32785)

    def test_a_wildcard_bind_is_reachable_over_loopback(self):
        mappings = [{"host_ip": "0.0.0.0", "host_port": 32786,
                     "sandbox_port": 9977, "protocol": "tcp4"}]
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout=json.dumps(mappings))
        self.assertEqual(c.lookup_published_host_port("mw-cite", 9977, run=run), 32786)

    def test_an_ipv6_only_publish_is_not_a_port_we_can_dial(self):
        # Our URLs say 127.0.0.1. A ::1 mapping is not a port we have.
        mappings = [{"host_ip": "::1", "host_port": 32787,
                     "sandbox_port": 9977, "protocol": "tcp6"}]
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout=json.dumps(mappings))
        self.assertIsNone(c.lookup_published_host_port("mw-cite", 9977, run=run))

    def test_udp_is_not_a_git_daemon(self):
        mappings = [{"host_ip": "127.0.0.1", "host_port": 32788,
                     "sandbox_port": 9977, "protocol": "udp"}]
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout=json.dumps(mappings))
        self.assertIsNone(c.lookup_published_host_port("mw-cite", 9977, run=run))

    def test_the_explicit_loopback_entry_wins_over_a_wildcard_one(self):
        mappings = [
            {"host_ip": "0.0.0.0", "host_port": 40001, "sandbox_port": 9977, "protocol": "tcp4"},
            {"host_ip": "127.0.0.1", "host_port": 40002, "sandbox_port": 9977, "protocol": "tcp4"},
        ]
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout=json.dumps(mappings))
        self.assertEqual(c.lookup_published_host_port("mw-cite", 9977, run=run), 40002)


class PublishDaemonPortTests(unittest.TestCase):
    def test_success_publishes_then_looks_up_the_host_port(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if "--json" in argv:
                return FakeCompletedProcess(0, stdout=json.dumps([
                    {"host_ip": "127.0.0.1", "host_port": 54321, "sandbox_port": 9977, "protocol": "tcp"},
                ]))
            return FakeCompletedProcess(0)

        self.assertEqual(c.publish_daemon_port("mw-cite", 9977, run=run), 54321)
        self.assertEqual(
            calls,
            [
                [c.WMF_SBX, "--upstream", "ports", "mw-cite", "--publish", "9977"],
                [c.WMF_SBX, "--upstream", "ports", "mw-cite", "--json"],
            ],
        )

    def test_publish_failure_skips_lookup_and_returns_none(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return FakeCompletedProcess(1, stdout="")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = c.publish_daemon_port("mw-cite", 9977, run=run)
        self.assertIsNone(result)
        self.assertEqual(calls, [[c.WMF_SBX, "--upstream", "ports", "mw-cite", "--publish", "9977"]])
        self.assertIn("wmf-sbx ports mw-cite --publish 9977", err.getvalue())

    def test_publish_succeeds_but_lookup_fails_returns_none(self):
        def run(argv, **kw):
            if "--json" in argv:
                return FakeCompletedProcess(1, stdout="")
            return FakeCompletedProcess(0)

        self.assertIsNone(c.publish_daemon_port("mw-cite", 9977, run=run))


class RefreshHostPortTests(unittest.TestCase):
    """The daemon's published host port changes on every container start
    (MEASURED, sbx/NOTES.md §34), so a recorded port is a stale port."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": os.path.join(self.tmp.name, "state")}
        self.synced = []
        patcher = mock.patch.object(
            c.remotes_mod, "sync_remotes",
            lambda name, candidates, **kw: (
                self.synced.append((name, candidates)),
                ([dict(cand, adopted=True) for cand in candidates], []),
            )[1],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_state(self, host_port=32783, sandbox_path="/home/agent/Wikimedia/Cite"):
        state = c.state_mod.new_state("mw-cite", daemon_port=9977, host_port=host_port)
        entry = {
            "hostDir": "/home/cananian/Wikimedia/Cite",
            "remote": "sandbox-mw-cite",
            "url": f"git://127.0.0.1:{host_port}/Wikimedia/Cite",
        }
        if sandbox_path:
            entry["sandboxPath"] = sandbox_path
        state["remotes"] = [entry]
        c.state_mod.save(state, env=self.env)
        return state

    @staticmethod
    def ports_run(host_port, calls=None, published=True):
        """Stands in for `wmf-sbx ports`. `published=False` reports an
        empty mapping list until something publishes."""
        seen = {"published": published}

        def run(argv, **kw):
            if calls is not None:
                calls.append(argv)
            if "--publish" in argv:
                seen["published"] = True
                return FakeCompletedProcess(0)
            if not seen["published"]:
                return FakeCompletedProcess(0, stdout="[]")
            return FakeCompletedProcess(0, stdout=json.dumps([
                {"host_ip": "127.0.0.1", "host_port": host_port,
                 "sandbox_port": 9977, "protocol": "tcp"},
            ]))
        return run

    def test_the_recorded_port_is_never_trusted(self):
        state = self.make_state(host_port=32783)
        port = c.refresh_host_port(
            "mw-cite", state, run=self.ports_run(32784), env=self.env
        )
        self.assertEqual(port, 32784)
        (_name, candidates), = self.synced
        self.assertEqual(
            [cand["url"] for cand in candidates],
            ["git://127.0.0.1:32784/Wikimedia/Cite"],
        )
        saved = c.state_mod.load("mw-cite", env=self.env)
        self.assertEqual(saved["hostPort"], 32784)
        self.assertEqual(saved["remotes"][0]["url"], "git://127.0.0.1:32784/Wikimedia/Cite")

    def test_nothing_published_is_republished(self):
        # The mapping can be lost rather than merely moved; re-publishing
        # is what makes the sandbox fetchable again.
        calls = []
        state = self.make_state()
        port = c.refresh_host_port(
            "mw-cite", state,
            run=self.ports_run(40000, calls=calls, published=False), env=self.env,
        )
        self.assertEqual(port, 40000)
        self.assertIn([c.WMF_SBX, "--upstream", "ports", "mw-cite", "--publish", "9977"], calls)

    def test_an_unreachable_sandbox_changes_nothing(self):
        # A stopped sandbox publishes no port. Re-pointing the remotes at a
        # guess would be worse than leaving them stale.
        state = self.make_state()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            port = c.refresh_host_port(
                "mw-cite", state,
                run=lambda argv, **kw: FakeCompletedProcess(1), env=self.env,
            )
        self.assertIsNone(port)
        self.assertEqual(self.synced, [])
        self.assertEqual(c.state_mod.load("mw-cite", env=self.env)["hostPort"], 32783)

    def test_a_remote_with_no_recorded_sandbox_path_is_skipped(self):
        # Its URL can't be rebuilt, and re-pointing blind could aim a host
        # repo at some other repo's tree.
        state = self.make_state(sandbox_path=None)
        port = c.refresh_host_port(
            "mw-cite", state, run=self.ports_run(32784), env=self.env
        )
        self.assertEqual(port, 32784)
        self.assertEqual(self.synced, [])


class EnsurePublishedHostPortTests(unittest.TestCase):
    """The generated kit declares the daemon's port in its own `ports:`
    block, so the usual case is a lookup, not a publish."""

    def test_an_already_published_port_is_not_published_again(self):
        calls = []
        run = RefreshHostPortTests.ports_run(32784, calls=calls)
        self.assertEqual(c.ensure_published_host_port("mw-cite", 9977, run=run), 32784)
        self.assertFalse(any("--publish" in argv for argv in calls))

    def test_an_unpublished_port_falls_back_to_publishing(self):
        # An explicit --kit that doesn't declare the port still has to work.
        calls = []
        run = RefreshHostPortTests.ports_run(40000, calls=calls, published=False)
        self.assertEqual(c.ensure_published_host_port("mw-cite", 9977, run=run), 40000)
        self.assertIn([c.WMF_SBX, "--upstream", "ports", "mw-cite", "--publish", "9977"], calls)


class AddHostRemotesTests(unittest.TestCase):
    """MEASURED, cananian, 2026-09-08: the state add_host_remotes() records
    must come out attached=True -- the real interactive `sbx create ...
    claude ...` attach already succeeded by the time main() calls this, so
    that attach IS the sandbox's first conversation. A fresh attached=False
    state here made the very next `wmf-sbx-resume` strip `--continue`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env_patch = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name})
        env_patch.start()
        self.addCleanup(env_patch.stop)

        candidates = [{"hostDir": "/h/Cite", "remote": "sandbox-mw-cite", "url": "git://x/y"}]
        self.candidates = candidates

        def fake_sync_remotes(name, cands, run=None, warn=None):
            return (
                [{"hostDir": cands[0]["hostDir"], "remote": cands[0]["remote"], "url": cands[0]["url"]}],
                [],
            )

        sync_patch = mock.patch.object(c.remotes_mod, "sync_remotes", fake_sync_remotes)
        sync_patch.start()
        self.addCleanup(sync_patch.stop)

    def test_recorded_state_is_attached(self):
        state = c.add_host_remotes("mw-cite", self.candidates, 9418, 32784)
        self.assertTrue(state["attached"])
        self.assertTrue(c.state_mod.load("mw-cite")["attached"])


class StartSandboxTests(unittest.TestCase):
    def test_exec_true_is_what_starts_a_stopped_sandbox(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return FakeCompletedProcess(0)

        self.assertTrue(c.start_sandbox("mw-cite", run=run))
        self.assertEqual(
            calls, [[c.WMF_SBX, "--upstream", "exec", "mw-cite", "--", "true"]]
        )

    def test_a_sandbox_that_will_not_start_reports_false(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            started = c.start_sandbox(
                "mw-cite",
                run=lambda argv, **kw: FakeCompletedProcess(1, stderr="no such sandbox"),
            )
        self.assertFalse(started)
        self.assertIn("could not start", err.getvalue())
        self.assertNotIn("answer y", err.getvalue())

    def test_a_daemon_that_must_restart_gets_a_next_step(self):
        # Measured with sbx v0.43.0 against a v0.43.0-rc3 daemon (§96).
        stderr = (
            "Docker Sandboxes has been updated and needs to restart. All "
            "running sandboxes will be stopped.\nERROR: ensure daemon: cannot "
            "prompt for restart: stdin is not a terminal; run the command in "
            "an interactive terminal to confirm the restart\n"
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            started = c.start_sandbox(
                "mw-cite",
                run=lambda argv, **kw: FakeCompletedProcess(1, stderr=stderr),
            )
        self.assertFalse(started)
        self.assertIn("`wmf-sbx ls`", err.getvalue())
        self.assertIn("stops every running sandbox", err.getvalue())


class WaitForDaemonTests(unittest.TestCase):
    """The container is up before the daemon is: the kit's startup command
    is `background: true`, and `exec ... true` returns as soon as the
    startup commands have been launched (§36.1)."""

    def test_it_keeps_probing_until_the_daemon_answers(self):
        calls, slept = [], []

        def run(argv, **kw):
            calls.append(argv)
            return FakeCompletedProcess(0 if len(calls) >= 3 else 1)

        self.assertTrue(c.wait_for_daemon(
            "mw-cite", 9977, run=run, delay=0.5, sleep=slept.append
        ))
        self.assertEqual(len(calls), 3)
        self.assertEqual(slept, [0.5, 0.5])
        # Probed inside the sandbox, not against the published host port:
        # Docker's port proxy accepts and *then* resets, so from the host
        # a connect can't tell "ready" from "too early".
        self.assertEqual(
            calls[0][:5], [c.WMF_SBX, "--upstream", "exec", "mw-cite", "--"]
        )
        self.assertIn("9977", calls[0][-1])

    def test_it_gives_up_rather_than_hanging(self):
        slept = []
        self.assertFalse(c.wait_for_daemon(
            "mw-cite", 9977, run=lambda argv, **kw: FakeCompletedProcess(1),
            attempts=3, delay=0.1, sleep=slept.append,
        ))
        # No trailing sleep after the last failed attempt.
        self.assertEqual(slept, [0.1, 0.1])


class SetupReportTests(unittest.TestCase):
    """`sbx create` collapses the whole install step to one ✓ line, so the
    setup script's own warnings only reach the engineer if we go back in
    and fetch them (sbx/NOTES.md §32.1)."""

    def status_run(self, status, returncode=0, prefix=""):
        def run(argv, **kw):
            self.last = argv
            return FakeCompletedProcess(
                returncode, stdout=prefix + json.dumps(status)
            )
        return run

    def report(self, run, **kw):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status = c.report_setup_problems("mw-cite", run=run, **kw)
        return status, err.getvalue()

    def test_it_cats_the_status_file_from_inside_the_sandbox(self):
        run = self.status_run({"version": 1, "exit": 0, "problems": []})
        c.read_setup_status("mw-cite", run=run)
        self.assertEqual(
            self.last,
            [c.WMF_SBX, "--upstream", "exec", "mw-cite", "--", "cat",
             "/var/log/wmf-sbx-setup.status"],
        )

    def test_sbx_exec_chrome_around_the_json_is_tolerated(self):
        # `sbx exec` prints its own lines when it has to start the sandbox.
        run = self.status_run(
            {"version": 1, "exit": 0, "problems": []},
            prefix="Sandbox mw-cite started successfully\n",
        )
        self.assertEqual(c.read_setup_status("mw-cite", run=run)["exit"], 0)

    def test_a_clean_report_says_nothing(self):
        status, err = self.report(
            self.status_run({"version": 1, "exit": 0, "problems": []})
        )
        self.assertEqual(status["exit"], 0)
        self.assertEqual(err, "")

    def test_problems_are_printed_with_the_log_to_read(self):
        status, err = self.report(self.status_run({
            "version": 1, "exit": 0,
            "problems": ["warning: git safe-reset origin failed in /home/agent/Parsoid"],
            "log": "/var/log/wmf-sbx-setup.log",
        }))
        self.assertIn("1 problem(s)", err)
        self.assertIn("git safe-reset origin failed", err)
        self.assertIn(
            "wmf-sbx exec mw-cite cat /var/log/wmf-sbx-setup.log", err
        )

    def test_a_failed_setup_says_the_sandbox_is_incomplete(self):
        _status, err = self.report(self.status_run({
            "version": 1, "exit": 1, "problems": ["error: npm install failed"],
        }))
        self.assertIn("setup step failed (exit 1)", err)
        self.assertIn("npm install failed", err)

    def test_an_unreadable_report_is_named_not_swallowed(self):
        status, err = self.report(lambda argv, **kw: FakeCompletedProcess(1))
        self.assertIsNone(status)
        self.assertIn("could not read the setup report", err)
        self.assertIn("/var/log/wmf-sbx-setup.log", err)

    def test_quiet_drops_that_warning(self):
        # The `sbx create` failed early and there may be no sandbox at all;
        # "couldn't read the report" would be the wrong headline (§37.1).
        status, err = self.report(
            lambda argv, **kw: FakeCompletedProcess(1), quiet=True
        )
        self.assertIsNone(status)
        self.assertEqual(err, "")

    def test_garbage_in_the_status_file_is_not_a_crash(self):
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout="not json at all")
        self.assertIsNone(c.read_setup_status("mw-cite", run=run))


class AmendWorkspaceClaudeMdTests(unittest.TestCase):
    """sbx/NOTES.md §95: run the sandbox's CLAUDE.md edit where the
    engineer sees its result, and say how to fix edits that no longer
    apply."""

    def amend(self, status, probe_rc=0, edit_rc=0):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if "test" in argv:
                return FakeCompletedProcess(probe_rc)
            if "--claude-md" in argv:
                return FakeCompletedProcess(edit_rc, stderr="boom")
            return FakeCompletedProcess(0, stdout=json.dumps(status))

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = c.amend_workspace_claude_md("mw-cite", run=run)
        return result, calls, err.getvalue()

    def test_a_clean_edit_says_nothing(self):
        result, calls, err = self.amend({"version": 1, "exit": 0, "problems": []})
        self.assertEqual(result["exit"], 0)
        self.assertEqual(err, "")
        self.assertEqual(calls[1][-4:], ["python3", "/home/agent/wmf-sbx-setup",
                                         "--claude-md",
                                         "/home/agent/.claude/wmf-sbx-claude-md.json"])
        self.assertEqual(calls[1][5], "sudo")
        self.assertEqual(calls[2][-1], "/var/log/wmf-sbx-claude-md.status")

    def test_edits_that_no_longer_apply_say_how_to_fix_them(self):
        _result, _calls, err = self.amend({
            "version": 1, "exit": 1, "log": None,
            "problems": ["error: edit 1 (replace '## Git workspace mode'): no such heading"],
        })
        self.assertIn("CLAUDE.md edit failed (exit 1)", err)
        self.assertIn("no such heading", err)
        self.assertIn("wmf-sbx refresh-claude-md mw-cite", err)
        self.assertIn("sbx/patches/sbx-claude-md/edits.json", err)
        self.assertNotIn("full log", err)

    def test_a_sandbox_without_the_edits_is_left_alone(self):
        result, calls, err = self.amend({}, probe_rc=1)
        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(err, "")

    def test_a_failed_exec_is_a_warning_not_a_failure(self):
        result, calls, err = self.amend({}, edit_rc=2)
        self.assertIsNone(result)
        self.assertIn("warning: the CLAUDE.md edit failed", err)
        self.assertEqual(len(calls), 2)


class ParallelTreeRemotesTests(unittest.TestCase):
    def test_repo_under_host_home_gets_a_remote(self):
        resolved = [
            ("gerrit:mediawiki/core", "/home/cananian/Wikimedia/core", False),
            (None, "/srv/shared/Skins", False),  # outside host_home -- no remote
        ]
        remotes = c.parallel_tree_remotes("/home/cananian", "mw-core", resolved, 9418)
        self.assertEqual(
            remotes,
            [
                {
                    "hostDir": "/home/cananian/Wikimedia/core",
                    "remote": "mw-core",
                    "url": "git://127.0.0.1:9418/Wikimedia/core",
                    "sandboxPath": "/home/agent/Wikimedia/core",
                    "readOnly": False,
                },
            ],
        )

    def test_no_repos_under_host_home_returns_empty(self):
        resolved = [(None, "/srv/shared/Skins", False)]
        self.assertEqual(c.parallel_tree_remotes("/home/cananian", "mw-core", resolved, 9418), [])

    def test_ro_repos_are_included_but_flagged(self):
        # The daemon serves the whole parallel tree either way (see
        # sbx/DESIGN-parallel-clone-tree.md §3), so a ':ro' repo is listed
        # rather than dropped -- but its sandbox path is a bind mount of
        # the host directory, so sync_remotes must not point that repo at
        # itself. The flag is how it knows.
        resolved = [
            ("gerrit:mediawiki/core", "/home/cananian/core", False),
            (None, "/home/cananian/Skins", False),
        ]
        remotes = c.parallel_tree_remotes(
            "/home/cananian", "mw-core", resolved, 9418,
            readonly_dirs={"/home/cananian/Skins"},
        )
        self.assertEqual([r["readOnly"] for r in remotes], [False, True])


class PrintRemoteAddReminderTests(unittest.TestCase):
    def _candidates(self, host_home, name, resolved, port, readonly_dirs=()):
        return c.parallel_tree_remotes(host_home, name, resolved, port, readonly_dirs)

    def test_prints_git_remote_add_for_each_parallel_repo(self):
        resolved = [("gerrit:mediawiki/core", "/home/cananian/Wikimedia/core", False)]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            c.print_remote_add_reminder(
                self._candidates("/home/cananian", "mw-core", resolved, 9418)
            )
        self.assertIn(
            "git -C /home/cananian/Wikimedia/core remote add --no-tags "
            "mw-core git://127.0.0.1:9418/Wikimedia/core",
            err.getvalue(),
        )

    def test_prints_nothing_when_no_repos_are_in_the_parallel_tree(self):
        resolved = [(None, "/srv/shared/Skins", False)]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            c.print_remote_add_reminder(
                self._candidates("/home/cananian", "mw-core", resolved, 9418)
            )
        self.assertEqual(err.getvalue(), "")

    def test_skips_readonly_repos(self):
        resolved = [(None, "/home/cananian/Skins", False)]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            c.print_remote_add_reminder(
                self._candidates(
                    "/home/cananian", "mw-core", resolved, 9418,
                    readonly_dirs={"/home/cananian/Skins"},
                )
            )
        self.assertEqual(err.getvalue(), "")


class IsRawPathTests(unittest.TestCase):
    def test_absolute_path(self):
        self.assertTrue(c.is_raw_path("/home/cananian/Wikimedia/Skins"))

    def test_home_relative_path(self):
        self.assertTrue(c.is_raw_path("~/Wikimedia/Skins"))

    def test_dot_relative_paths(self):
        self.assertTrue(c.is_raw_path("./Skins"))
        self.assertTrue(c.is_raw_path("../Wikimedia/Skins"))

    def test_bare_dot_paths(self):
        self.assertTrue(c.is_raw_path("."))
        self.assertTrue(c.is_raw_path(".."))

    def test_bare_name_is_not_raw(self):
        self.assertFalse(c.is_raw_path("Cite"))

    def test_scheme_prefixed_spec_is_not_raw(self):
        self.assertFalse(c.is_raw_path("gerrit:mediawiki/core"))
        self.assertFalse(c.is_raw_path("gitlab:repos/product-safety-and-integrity/wmf-claude"))


class ResolveRepoRawPathTests(unittest.TestCase):
    def test_existing_directory_resolves_with_no_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "Skins")
            os.makedirs(target)
            canonical, path, needs_clone = c.resolve_repo(target, "/nonexistent/repos.yaml")
            self.assertIsNone(canonical)
            self.assertEqual(path, os.path.realpath(target))
            self.assertFalse(needs_clone)

    def test_missing_directory_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "NoSuchDir")
            with self.assertRaises(r.ResolutionError):
                c.resolve_repo(missing, "/nonexistent/repos.yaml")


class CanonicalsForKitTests(unittest.TestCase):
    def test_resolved_canonical_passed_through(self):
        resolved = [("gerrit:mediawiki/core", "/w/core", False)]
        self.assertEqual(c.canonicals_for_kit(resolved, []), [("gerrit:mediawiki/core", "/w/core")])

    def test_raw_path_identified_via_exact_rule(self):
        rules = [{"match": "gerrit:mediawiki/core", "path": "/w/core"}]
        resolved = [(None, os.path.realpath("/w/core"), False)]
        self.assertEqual(c.canonicals_for_kit(resolved, rules), [("gerrit:mediawiki/core", "/w/core")])

    def test_raw_path_with_no_matching_rule_stays_none(self):
        resolved = [(None, "/w/Skins", False)]
        self.assertEqual(c.canonicals_for_kit(resolved, []), [(None, "/w/Skins")])


class NodeVersionAndPhabricatorUsernameStdinTests(unittest.TestCase):
    """Both of these calls postdated the original §47.8 sweep and were
    found missing stdin=DEVNULL during the 0.43 recreate, 2026-09-14
    (responses30.txt; sbx/NOTES.md §82.9) -- an unexpected interactive
    prompt from the invoked program should fail fast with EOF rather
    than hang invisibly, exactly like the 8 call sites §47.8 fixed."""

    def test_node_version_passes_stdin_devnull(self):
        calls = []

        def run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return FakeCompletedProcess(0, stdout="v22.22.1\n")

        self.assertEqual(c.node_version("node", run=run), (22, 22, 1))
        self.assertEqual(
            calls,
            [(["node", "--version"],
              {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL})],
        )

    def test_phabricator_username_claude_mcp_get_passes_stdin_devnull(self):
        calls = []

        def run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return FakeCompletedProcess(
                0, stdout="Command: env PHABRICATOR_USERNAME=cscott node x.js")

        with mock.patch.object(c, "wmf_claude_config", return_value={}):
            self.assertEqual(c.phabricator_username(run=run), "cscott")
        self.assertEqual(
            calls,
            [(["claude", "mcp", "get", "phabricator"],
              {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL})],
        )


class LinkPlanTests(unittest.TestCase):
    """The naming rules live in wmf_sbx_deps; this covers how a resolved
    (canonical, path) list turns into the plan file's linkName/linkDir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def repo(self, name, manifest_filename=None, data=None):
        path = os.path.join(self.tmp.name, name)
        os.makedirs(path, exist_ok=True)
        if manifest_filename:
            with open(os.path.join(path, manifest_filename), "w", encoding="utf-8") as f:
                json.dump(data or {}, f)
        return path

    def test_extension_and_skin_go_to_their_own_sections(self):
        cite = self.repo("Cite", "extension.json")
        vector = self.repo("Vector", "skin.json")
        links = c.link_plan([
            ("gerrit:mediawiki/extensions/Cite", cite),
            ("gerrit:mediawiki/skins/Vector", vector),
        ])
        self.assertEqual(links, {
            cite: ("Cite", "extensions"),
            vector: ("Vector", "skins"),
        })

    def test_core_is_not_linked_into_itself(self):
        core = self.repo("core")
        self.assertEqual(c.link_plan([("gerrit:mediawiki/core", core)]), {})

    def test_a_directory_with_no_manifest_outside_the_trees_is_not_linked(self):
        # e.g. the mediawiki/extensions container directory itself.
        container = self.repo("extensions")
        self.assertEqual(c.link_plan([("gerrit:mediawiki/extensions", container)]), {})

    def test_a_raw_path_is_linked_under_its_own_basename(self):
        path = self.repo("MyExt", "extension.json", {"name": "Something Else"})
        self.assertEqual(c.link_plan([(None, path)]), {path: ("MyExt", "extensions")})

    def test_outside_the_trees_the_name_field_picks_the_link_name(self):
        # mediawiki/services/parsoid has to land at extensions/Parsoid.
        path = self.repo("parsoid", "extension.json", {"name": "Parsoid"})
        links = c.link_plan([("gerrit:mediawiki/services/parsoid", path)], warn=lambda m: None)
        self.assertEqual(links, {path: ("Parsoid", "extensions")})

    def test_config_overrides_beat_every_other_rule(self):
        cite = self.repo("Cite", "extension.json")
        links = c.link_plan([("gerrit:mediawiki/extensions/Cite", cite)],
                            overrides={"gerrit:mediawiki/extensions/Cite": "Citation"})
        self.assertEqual(links, {cite: ("Citation", "extensions")})

    def test_a_path_that_is_not_checked_out_yet_still_gets_a_link(self):
        # The clone happens inside the sandbox, so link_plan runs before the
        # directory exists; the tree prefix is enough to name it.
        missing = os.path.join(self.tmp.name, "Echo")
        links = c.link_plan([("gerrit:mediawiki/extensions/Echo", missing)])
        self.assertEqual(links, {missing: ("Echo", "extensions")})


class ExpandDependenciesTests(unittest.TestCase):
    """The walk itself is tested in test_wmf_sbx_deps.py; this covers the
    seam -- how a discovered canonical becomes a mountable extra."""

    EXT = "gerrit:mediawiki/extensions/"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = os.path.join(self.tmp.name, "repos.yaml")
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(
                'rules:\n'
                '  - match: "gerrit:**/{name}"\n'
                f'    path: "{self.tmp.name}/{{name}}"\n'
            )
        self.rules = [{"match": "gerrit:**/{name}", "path": f"{self.tmp.name}/{{name}}"}]
        self.cite = os.path.join(self.tmp.name, "Cite")
        os.makedirs(self.cite)
        patch_offline(self)

    def manifest(self, **data):
        return c.deps_mod.Manifest("extension.json", data)

    def write_manifest(self, directory, **data):
        with open(os.path.join(directory, "extension.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def expand(self, fetch=None, **kw):
        resolved = [(self.EXT + "Cite", self.cite, False)]
        split = [(self.EXT + "Cite", False)]
        return c.expand_dependencies(
            resolved, split, {"rules": self.rules}, self.config_path,
            fetch=fetch or (lambda canonical, warn=None: None),
            warn=lambda msg: None, **kw
        )

    @unittest.skipUnless(r.yaml is not None, "PyYAML not installed")
    def test_discovered_repos_become_writable_extras(self):
        self.write_manifest(self.cite, name="Cite")
        resolved, split, origins, _unreachable = self.expand()
        paths = [path for _canonical, path, _needs_clone in resolved]
        self.assertEqual(paths[0], self.cite)  # primary is untouched
        self.assertIn(os.path.join(self.tmp.name, "core"), paths)
        self.assertIn(os.path.join(self.tmp.name, "Vector"), paths)
        # Never ':ro' -- you routinely need to edit a dependency, and the
        # writable copy costs nothing (it's a --reference clone).
        self.assertEqual([is_ro for _spec, is_ro in split], [False, False, False])

    @unittest.skipUnless(r.yaml is not None, "PyYAML not installed")
    def test_discovered_repos_are_marked_needing_a_clone(self):
        self.write_manifest(self.cite, name="Cite")
        resolved, _split, _origins, _unreachable = self.expand()
        by_path = {path: needs_clone for _canonical, path, needs_clone in resolved}
        self.assertTrue(by_path[os.path.join(self.tmp.name, "core")])

    @unittest.skipUnless(r.yaml is not None, "PyYAML not installed")
    def test_origin_chain_is_reported_per_path(self):
        self.write_manifest(self.cite, name="Cite")
        _resolved, _split, origins, _unreachable = self.expand()
        self.assertEqual(
            origins[os.path.join(self.tmp.name, "core")],
            [self.EXT + "Cite", "gerrit:mediawiki/core"],
        )

    @unittest.skipUnless(r.yaml is not None, "PyYAML not installed")
    def test_no_manifest_means_no_expansion(self):
        # Cite's directory exists but holds no extension.json, so there is
        # nothing to walk -- and, importantly, no gitiles fetch either.
        resolved, split, origins, _unreachable = self.expand()
        self.assertEqual(len(resolved), 1)
        self.assertEqual(origins, {})

    @unittest.skipUnless(r.yaml is not None, "PyYAML not installed")
    def test_manifests_are_fetched_for_repos_not_yet_cloned(self):
        self.write_manifest(self.cite, name="Cite",
                            requires={"extensions": {"Echo": "*"}})
        fetched = []

        def fetch(canonical, warn=None):
            fetched.append(canonical)
            if canonical == self.EXT + "Echo":
                return self.manifest(name="Echo",
                                     requires={"extensions": {"EventLogging": "*"}})
            return None

        resolved, _split, origins, _unreachable = self.expand(fetch=fetch)
        # The transitive dependency is only reachable through the fetched
        # manifest -- nothing is on disk to read it from.
        self.assertIn(self.EXT + "Echo", fetched)
        self.assertIn(os.path.join(self.tmp.name, "EventLogging"),
                      [path for _c, path, _n in resolved])
        self.assertEqual(
            origins[os.path.join(self.tmp.name, "EventLogging")],
            [self.EXT + "Cite", self.EXT + "Echo", self.EXT + "EventLogging"],
        )

    @unittest.skipUnless(r.yaml is not None, "PyYAML not installed")
    def test_no_suggests_is_passed_through(self):
        self.write_manifest(self.cite, name="Cite",
                            suggests={"extensions": {"Echo": "*"}})
        _resolved, split, _origins, _unreachable = self.expand(include_suggests=False)
        self.assertNotIn((self.EXT + "Echo", False), split)

    @unittest.skipUnless(r.yaml is not None, "PyYAML not installed")
    def test_unresolvable_dependency_names_its_chain_and_the_escape_hatch(self):
        # A dependency we can't turn into a path is a hard error: dropping
        # it silently resurfaces much later as inexplicable failures
        # inside the sandbox.
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(
                'rules:\n'
                '  - match: "gerrit:mediawiki/extensions/{name}"\n'
                f'    path: "{self.tmp.name}/{{name}}"\n'
            )
        self.rules = [
            {"match": "gerrit:mediawiki/extensions/{name}", "path": f"{self.tmp.name}/{{name}}"}
        ]
        self.write_manifest(self.cite, name="Cite")
        with self.assertRaises(r.ResolutionError) as caught:
            self.expand()
        message = str(caught.exception)
        self.assertIn("gerrit:mediawiki/core", message)
        self.assertIn("--no-deps", message)


class DoCloneTests(unittest.TestCase):
    def test_success_creates_parent_and_runs_git_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "nested", "Cite")
            calls = []

            def fake_run(argv):
                calls.append(argv)
                return FakeCompletedProcess(0)

            c.do_clone("gerrit:mediawiki/extensions/Cite", target, run=fake_run)
            self.assertTrue(os.path.isdir(os.path.dirname(target)))
            self.assertEqual(
                calls,
                [["git", "clone", "https://gerrit.wikimedia.org/r/mediawiki/extensions/Cite", target]],
            )

    def test_failure_raises_launch_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "Cite")
            with self.assertRaises(c.LaunchError):
                c.do_clone(
                    "gerrit:mediawiki/extensions/Cite", target, run=lambda argv: FakeCompletedProcess(1)
                )


_SAME_AS_USERNAME = object()  # see HostMcpTests.fake_run


class HostMcpTests(unittest.TestCase):
    """The host half of Route 1: the Phabricator and Gerrit MCP servers are
    registered with `sbx mcp add` on the *host*, so their credentials never
    enter the sandbox. sbx/DESIGN-plugin-integration.md §5 step 4.

    Nothing here was measured against a real `sbx mcp` -- that needs the
    host (see sbx/NOTES.md §65) -- so these pin the shapes §61.1/§62.1
    recorded and the fail-closed behaviour around them.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        # Every test here resolves a Phabricator username somewhere.
        # Without this they read the developer's own
        # ~/.config/wmf-claude/config.json and $PHABRICATOR_USERNAME, and
        # pass or fail depending on whose machine runs them.
        env = mock.patch.dict(
            os.environ, {"HOME": os.path.join(self.root, "home")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PHABRICATOR_USERNAME", None)

    def build_checkout(self, phabricator=True, gerrit=True):
        """A repo root with the submodules built, as bin/wmf-claude-build
        leaves them."""
        if phabricator:
            self._touch("mcp-phabricator", "src", "index.js")
        if gerrit:
            self._touch("gerrit-mcp-server", ".venv", "bin", "python")
            self._touch("gerrit-mcp-server", "gerrit_mcp_server", "main.py")

    def _touch(self, *parts):
        path = os.path.join(self.root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("")

    def fake_run(self, node_version="v22.22.1", username="cscott",
                 inspect=_SAME_AS_USERNAME):
        """A host whose `node`, stored answers and MCP registration all
        agree, unless a test says otherwise.

        `inspect` is what `sbx mcp inspect phabricator` reports the
        registration filters by -- by default the same username, since a
        mismatch is the exceptional case and every other test would
        rather not think about it. None makes `mcp inspect` fail, as an
        sbx that predates it would.
        """
        if inspect is _SAME_AS_USERNAME:
            inspect = username

        def run(cmd, **kwargs):
            if cmd[1:] == ["--version"]:
                return FakeCompletedProcess(0, stdout=node_version + "\n")
            if cmd[:3] == ["claude", "mcp", "get"]:
                if username is None:
                    return FakeCompletedProcess(1)
                return FakeCompletedProcess(
                    0, stdout=f"Command: env PHABRICATOR_USERNAME={username} node x.js")
            if cmd[2:4] == ["mcp", "inspect"]:
                if inspect is None:
                    return FakeCompletedProcess(2, stderr="unknown flag --json")
                # The shape §75.2 measured. "" is a registration that
                # names no username -- an engineer's own wrapper.
                assert "--json" in cmd, cmd
                argv = ["env"]
                if inspect:
                    argv.append(f"PHABRICATOR_USERNAME={inspect}")
                argv += ["/usr/bin/node", "/x/src/index.js"]
                return FakeCompletedProcess(0, stdout=json.dumps({
                    "name": "phabricator", "type": "local", "command": argv,
                    "requires_oauth": False, "resolved_command": "/usr/bin/env",
                }))
            raise AssertionError(f"unexpected call: {cmd}")
        return run

    def plan(self, **kwargs):
        with mock.patch.object(c.shutil, "which", return_value="/usr/bin/node"):
            return c.host_mcp_plan(run=self.fake_run(**kwargs), root=self.root)

    def test_both_servers_get_the_env_command_workaround(self):
        # `sbx mcp add` has no --env (§58.2); `--command env --args
        # "KEY=V,...,cmd,args"` is what works (§61.1, §62.1).
        self.build_checkout()
        plan, problems = self.plan()
        self.assertEqual(problems, [])
        self.assertEqual(plan["phabricator"], [
            "PHABRICATOR_USERNAME=cscott", "/usr/bin/node",
            os.path.join(self.root, "mcp-phabricator", "src", "index.js"),
        ])
        self.assertEqual(plan["gerrit"], [
            f"PYTHONPATH={os.path.join(self.root, 'gerrit-mcp-server')}",
            "PYTHONDONTWRITEBYTECODE=1",
            os.path.join(self.root, "gerrit-mcp-server", ".venv", "bin", "python"),
            os.path.join(self.root, "gerrit-mcp-server", "gerrit_mcp_server", "main.py"),
            "stdio",
        ])
        cmd = c.mcp_add_command("gerrit", plan["gerrit"])
        self.assertEqual(
            cmd[:7],
            [c.WMF_SBX, "--upstream", "mcp", "add", "gerrit", "--command", "env"],
        )
        self.assertEqual(cmd[8], ",".join(plan["gerrit"]))

    def write_config(self, payload):
        """A ~/.config/wmf-claude/config.json, as bin/wmf-claude-setup
        leaves it. `payload` is written raw so a test can hand over
        something that isn't JSON at all."""
        path = os.path.join(self.root, "home", ".config", "wmf-claude", "config.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload if isinstance(payload, str) else json.dumps(payload))
        return path

    def username(self, *, env=None, config=None, mcp_get="cscott"):
        """phabricator_username() with each of its three sources under
        the test's control."""
        with mock.patch.dict(os.environ, {}, clear=False):
            if env is not None:
                os.environ["PHABRICATOR_USERNAME"] = env
            if config is not None:
                self.write_config(config)
            return c.phabricator_username(run=self.fake_run(username=mcp_get))

    def test_the_config_file_beats_the_old_mcp_get_scrape(self):
        """bin/wmf-claude-setup now stores the answer; the registration it
        used to be scraped from is the fallback, not the source."""
        self.assertEqual(
            self.username(config={"phabricatorUsername": "stashed"},
                          mcp_get="registered"),
            "stashed")

    def test_the_mcp_get_scrape_still_answers_an_older_install(self):
        self.assertEqual(self.username(config=None, mcp_get="registered"),
                         "registered")
        # …and so does a config file that simply hasn't got the key.
        self.assertEqual(self.username(config={"somethingElse": 1},
                                       mcp_get="registered"),
                         "registered")

    def test_the_environment_beats_the_config_file(self):
        self.assertEqual(
            self.username(env="fromenv",
                          config={"phabricatorUsername": "stashed"}),
            "fromenv")

    def test_a_broken_config_file_falls_through_rather_than_failing(self):
        """A create must not die over a config file it does not own."""
        for payload in ("not json at all",
                        '["a list", "not an object"]',
                        '{"phabricatorUsername": 42}',
                        '{"phabricatorUsername": "   "}'):
            with self.subTest(payload=payload):
                self.assertEqual(self.username(config=payload,
                                               mcp_get="registered"),
                                 "registered")

    def test_nothing_anywhere_is_not_an_error(self):
        self.assertIsNone(self.username(config=None, mcp_get=None))

    def test_an_unbuilt_checkout_registers_nothing(self):
        plan, problems = self.plan()
        self.assertEqual(plan, {})
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("wmf-claude-build" in p for p in problems))

    def test_node_below_the_floor_drops_phabricator(self):
        # `sbx mcp ls` would call it `✓ ready` and it would then fail at
        # run time with `ReferenceError: File is not defined` (§60.2), so
        # this is the only place the version can be caught.
        self.build_checkout()
        plan, problems = self.plan(node_version="v18.19.1")
        self.assertEqual(sorted(plan), ["gerrit"])
        self.assertTrue(any("20.18.1" in p for p in problems), problems)
        node = [p for p in problems if "20.18.1" in p][0]
        # ... and it stops the create rather than warning, because the
        # engineer has to choose how to fix it. §68.
        self.assertTrue(node.stop)
        self.assertEqual(node.server, "phabricator")

    def test_an_unbuilt_checkout_does_not_stop_the_create(self):
        # The printed command fixes it in place and the next create picks
        # the server up by itself; nothing was written anywhere. Contrast
        # the node floor.
        _plan, problems = self.plan()
        self.assertTrue(problems)
        self.assertFalse(any(p.stop for p in problems), problems)

    def test_a_node_that_will_not_say_its_version_stops_the_create(self):
        self.build_checkout()
        _plan, problems = self.plan(node_version="not a version")
        stopping = [p for p in problems if p.stop]
        self.assertEqual(len(stopping), 1)
        self.assertIn("did not say which version", stopping[0])

    def test_no_node_at_all_stops_the_create(self):
        self.build_checkout()
        with mock.patch.object(c.shutil, "which", return_value=None):
            _plan, problems = c.host_mcp_plan(run=self.fake_run(),
                                              root=self.root)
        self.assertEqual([p.stop for p in problems], [True])

    def test_the_declared_engine_can_raise_the_floor_but_not_lower_it(self):
        # mcp-phabricator's own package.json says >=20.0.0, but the floor
        # that bites is cheerio -> undici at >=20.18.1 (§60.2). Reading
        # `engines` *instead* of the measured floor would let node 20.0.0
        # through, so it is only ever a maximum.
        self._write_package_json('{"engines": {"node": ">=20.0.0"}}')
        self.assertEqual(c.required_node_version(self.root),
                         c.MIN_NODE_VERSION)
        self._write_package_json('{"engines": {"node": ">=24.1.0"}}')
        self.assertEqual(c.required_node_version(self.root), (24, 1, 0))

    def test_an_unreadable_or_odd_engines_field_falls_back(self):
        for body in ['{"engines": {"node": "^20 || ^22"}}',
                     '{"engines": {"node": true}}',
                     '{"engines": {}}',
                     "not json at all"]:
            with self.subTest(body):
                self._write_package_json(body)
                self.assertEqual(c.required_node_version(self.root),
                                 c.MIN_NODE_VERSION)

    def test_a_missing_package_json_falls_back(self):
        self.assertEqual(c.required_node_version(self.root),
                         c.MIN_NODE_VERSION)

    def _write_package_json(self, body):
        path = os.path.join(self.root, "mcp-phabricator", "package.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)

    def test_an_old_node_stops_the_create_with_advice(self):
        self.build_checkout()

        def run(cmd, **kwargs):
            if cmd[2:4] == ["mcp", "ls"]:
                return FakeCompletedProcess(0, stdout='{"servers": []}')
            if cmd[2:4] == ["mcp", "add"]:
                raise AssertionError("must not register with an old node")
            return self.fake_run(node_version="v18.19.1")(cmd, **kwargs)

        with mock.patch.object(c.shutil, "which", return_value="/usr/bin/node"), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(c.McpPreflightError):
                c.ensure_host_mcp_servers(run=run, root=self.root)
        message = err.getvalue()
        self.assertIn("v18.19.1", message)
        self.assertIn("v20.18.1", message)
        # The two ways out, both spelled: fix node, or --no-mcp.
        self.assertIn("--no-mcp", message)
        self.assertIn("PATH", message)

    def test_an_old_node_does_not_stop_a_host_that_already_has_it(self):
        # That registration carries its own resolved command and nothing
        # here is about to touch it; only a server we'd have to add now
        # can block. (Whether *it* was registered under a good node is a
        # separate question -- §68.)
        self.build_checkout()

        def run(cmd, **kwargs):
            if cmd[2:4] == ["mcp", "ls"]:
                return FakeCompletedProcess(
                    0, stdout='{"servers": [{"name": "phabricator"}]}')
            if cmd[2:4] == ["mcp", "add"]:
                return FakeCompletedProcess(0)
            return self.fake_run(node_version="v18.19.1")(cmd, **kwargs)

        with mock.patch.object(c.shutil, "which", return_value="/usr/bin/node"), \
             contextlib.redirect_stderr(io.StringIO()):
            registered = c.ensure_host_mcp_servers(run=run, root=self.root)
        self.assertEqual(registered, ["gerrit", "phabricator"])

    def ensure_with_registered_phabricator(self, *, inspect, config=None,
                                           **kwargs):
        """ensure_host_mcp_servers against a host that already has both
        servers registered -- the only situation the username check runs
        in. Returns (registered names, stderr); `mcp add` and `mcp rm`
        are assertion failures, since neither may happen here."""
        self.build_checkout()
        if config is not None:
            self.write_config(config)
        inner = self.fake_run(inspect=inspect, **kwargs)

        def run(cmd, **kw):
            if cmd[2:4] == ["mcp", "ls"]:
                return FakeCompletedProcess(0, stdout=self.MCP_LS_JSON)
            if cmd[2:4] in (["mcp", "add"], ["mcp", "rm"]):
                raise AssertionError(
                    f"must not touch the host's registration: {cmd}")
            return inner(cmd, **kw)

        with mock.patch.object(c.shutil, "which", return_value="/usr/bin/node"), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            registered = c.ensure_host_mcp_servers(run=run, root=self.root)
        return registered, err.getvalue()

    def test_a_changed_username_stops_the_create(self):
        # `sbx mcp add` has no update-in-place and we never overwrite, so
        # without this the sandbox filters "my tasks" by whoever the
        # engineer used to be -- answering, and answering about the wrong
        # person.
        self.build_checkout()
        self.write_config({"phabricatorUsername": "newname"})

        def run(cmd, **kwargs):
            if cmd[2:4] == ["mcp", "ls"]:
                return FakeCompletedProcess(0, stdout=self.MCP_LS_JSON)
            if cmd[2:4] in (["mcp", "add"], ["mcp", "rm"]):
                raise AssertionError(
                    "the running server is the engineer's to remove")
            return self.fake_run(inspect="oldname")(cmd, **kwargs)

        with mock.patch.object(c.shutil, "which", return_value="/usr/bin/node"), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(c.McpPreflightError):
                c.ensure_host_mcp_servers(run=run, root=self.root)
        message = err.getvalue()
        self.assertIn("oldname", message)
        self.assertIn("newname", message)
        # The fix is named, and it is the engineer's to run: removing a
        # server mid-session takes the tools away from every sandbox
        # holding it open.
        self.assertIn("wmf-sbx mcp rm phabricator", message)
        # ...and the node advice, which is about something else entirely,
        # does not come along with it.
        self.assertNotIn("--no-mcp", message)

    def test_the_same_username_is_not_a_mismatch(self):
        registered, err = self.ensure_with_registered_phabricator(
            inspect="cscott", config={"phabricatorUsername": "cscott"})
        self.assertEqual(registered, ["gerrit", "phabricator"])
        self.assertNotIn("filters", err)

    def test_a_registration_we_cannot_read_is_not_a_mismatch(self):
        # An sbx without `mcp inspect`, and a registration that passes the
        # username some other way (a wrapper script of the engineer's).
        # Neither is evidence of disagreement, and a create must not stop
        # on the absence of evidence.
        for inspect in (None, ""):
            with self.subTest(inspect=inspect):
                registered, _err = self.ensure_with_registered_phabricator(
                    inspect=inspect, config={"phabricatorUsername": "newname"})
                self.assertEqual(registered, ["gerrit", "phabricator"])

    # `wmf-sbx mcp inspect phabricator --json` on cananian's host, copied
    # verbatim (MEASURED, sbx/NOTES.md §75.2).
    MCP_INSPECT_JSON = """{
      "name": "phabricator",
      "type": "local",
      "command": [
        "env",
        "PHABRICATOR_USERNAME=cscott",
        "/home/cananian/.nave/installed/26.8.2/bin/node",
        "/home/cananian/Projects/Wikimedia/wmf-claude/mcp-phabricator/src/index.js"
      ],
      "requires_oauth": false,
      "resolved_command": "/usr/bin/env"
    }"""

    def test_the_stored_username_is_read_out_of_the_json(self):
        calls = []

        def run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return FakeCompletedProcess(0, stdout=self.MCP_INSPECT_JSON)

        self.assertEqual(c.registered_phabricator_username(run=run), "cscott")
        # One element of `command` either is the assignment or is not:
        # no parsing of the human layout, which is a layout.
        # stdin=DEVNULL so an unexpected interactive prompt from `sbx`
        # itself (e.g. a version-mismatch restart prompt) fails fast with
        # EOF instead of hanging invisibly -- regression fixed
        # 2026-09-14, sbx/NOTES.md §82.9 (this call site postdated the
        # original §47.8 sweep).
        self.assertEqual(
            calls,
            [([c.WMF_SBX, "--upstream", "mcp", "inspect", "phabricator", "--json"],
              {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL})],
        )

    def test_json_we_cannot_read_is_no_answer_rather_than_a_wrong_one(self):
        for stdout in ("Command:   env PHABRICATOR_USERNAME=cscott /x/node",
                       "<html>",
                       '{"command": "env PHABRICATOR_USERNAME=cscott"}',
                       '{"command": ["env", "/x/node"]}',
                       '{"command": ["env", "PHABRICATOR_USERNAME="]}',
                       ""):
            with self.subTest(stdout=stdout):
                self.assertIsNone(c.registered_phabricator_username(
                    run=lambda cmd, **kw: FakeCompletedProcess(0,
                                                               stdout=stdout)))

    def test_a_host_with_no_username_of_its_own_does_not_stop(self):
        # Nothing to disagree with: the server works anonymously and the
        # username is only a default filter.
        registered, _err = self.ensure_with_registered_phabricator(
            inspect="cscott", username=None)
        self.assertEqual(registered, ["gerrit", "phabricator"])

    def test_the_username_is_not_checked_before_there_is_a_registration(self):
        # Nothing to compare against, and `mcp inspect` on a name that
        # isn't there is a subprocess spent to learn that.
        self.build_checkout()

        def run(cmd, **kwargs):
            if cmd[2:4] == ["mcp", "ls"]:
                return FakeCompletedProcess(0, stdout='{"servers": []}')
            if cmd[2:4] == ["mcp", "add"]:
                return FakeCompletedProcess(0)
            if cmd[2:4] == ["mcp", "inspect"]:
                raise AssertionError("nothing is registered to inspect")
            return self.fake_run()(cmd, **kwargs)

        with mock.patch.object(c.shutil, "which", return_value="/usr/bin/node"), \
             contextlib.redirect_stderr(io.StringIO()):
            registered = c.ensure_host_mcp_servers(run=run, root=self.root)
        self.assertEqual(registered, ["gerrit", "phabricator"])

    def test_an_unrelated_host_registration_is_not_proxied(self):
        # The engineer's own `sbx mcp add` of something else is none of
        # our business: only the servers this tool knows how to put an
        # allowlist in front of get a proxy entry in the kit.
        self.build_checkout()

        def run(cmd, **kwargs):
            if cmd[2:4] == ["mcp", "ls"]:
                return FakeCompletedProcess(
                    0, stdout='{"servers": [{"name": "some-other-server"}]}')
            if cmd[2:4] == ["mcp", "add"]:
                return FakeCompletedProcess(0)
            return self.fake_run()(cmd, **kwargs)

        with mock.patch.object(c.shutil, "which", return_value="/usr/bin/node"), \
             contextlib.redirect_stderr(io.StringIO()):
            registered = c.ensure_host_mcp_servers(run=run, root=self.root)
        self.assertEqual(registered, ["gerrit", "phabricator"])

    def test_register_false_checks_everything_and_adds_nothing(self):
        self.build_checkout()

        def run(cmd, **kwargs):
            if cmd[2:4] == ["mcp", "ls"]:
                return FakeCompletedProcess(0, stdout='{"servers": []}')
            if cmd[2:4] == ["mcp", "add"]:
                raise AssertionError("register=False must not add")
            return self.fake_run()(cmd, **kwargs)

        with mock.patch.object(c.shutil, "which", return_value="/usr/bin/node"), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            registered = c.ensure_host_mcp_servers(
                run=run, root=self.root, register=False)
        self.assertEqual(registered, ["gerrit", "phabricator"])
        self.assertIn("would run", err.getvalue())

    def test_no_username_still_registers_phabricator(self):
        # The server is anonymous and public-data-only; the username is a
        # default filter for "my tasks", not an authenticator.
        self.build_checkout()
        plan, _problems = self.plan(username=None)
        self.assertEqual(plan["phabricator"][0], "/usr/bin/node")

    def test_a_comma_in_the_checkout_path_drops_the_server(self):
        # `--args` is comma-split (§61.1), so such a command cannot be
        # spelled at all -- better no server than a mangled one.
        self.root = os.path.join(self.tmp.name, "we,ird")
        self.build_checkout()
        plan, problems = self.plan()
        self.assertEqual(plan, {})
        self.assertTrue(all("comma-split" in p for p in problems), problems)

    # `sbx mcp ls --json` on a host with both servers registered, copied
    # from the real output (MEASURED, sbx/NOTES.md §67).
    MCP_LS_JSON = json.dumps({
        "gateway": {"name": "LOCAL", "local": True,
                    "operator": "managed by you", "decision": "local",
                    "signed_in_as": "cscott"},
        "servers": [
            {"name": "gerrit", "transport": "local stdio",
             "status": "ready", "type": "local"},
            {"name": "phabricator", "transport": "local stdio",
             "status": "ready", "type": "local"},
        ],
    })

    def test_registration_listing_reads_the_json(self):
        calls = []

        def run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return FakeCompletedProcess(0, stdout=self.MCP_LS_JSON)

        self.assertEqual(c.host_mcp_registrations(run=run),
                         {"phabricator", "gerrit"})
        # One call: there is no fallback to the human table any more.
        # stdin=DEVNULL so an unexpected interactive prompt from `sbx`
        # itself (e.g. a version-mismatch restart prompt) fails fast with
        # EOF instead of hanging invisibly -- regression MEASURED,
        # cananian, 2026-09-14 (responses30.txt): this exact call hung on
        # the restart prompt during a real create because this call site
        # postdated the original §47.8 sweep. Fixed, sbx/NOTES.md §82.9.
        self.assertEqual(
            calls,
            [([c.WMF_SBX, "--upstream", "mcp", "ls", "--json"],
              {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL})],
        )

    def test_json_that_does_not_parse_is_not_an_empty_store(self):
        # "Nothing registered" would mean re-adding over whatever is
        # actually there; unreadable has to stay unreadable. The table is
        # in here because it is what an sbx too old for `--json` would
        # print, and reading it is exactly what must not happen.
        for stdout in ("<html>", "",
                       "LOCAL · managed by you · ✓ on\n\n"
                       "  gerrit        local stdio   ✓ ready\n"
                       "  phabricator   local stdio   ✓ ready\n"):
            with self.subTest(stdout=stdout):
                self.assertIsNone(c.host_mcp_registrations(
                    run=lambda cmd, **kw: FakeCompletedProcess(0,
                                                               stdout=stdout)))

    def test_a_server_with_no_name_is_skipped(self):
        listing = json.dumps({"servers": [{"transport": "local stdio"},
                                          {"name": "gerrit"}]})
        self.assertEqual(
            c.host_mcp_registrations(
                run=lambda cmd, **kw: FakeCompletedProcess(0, stdout=listing)),
            {"gerrit"})

    def test_an_unreadable_store_registers_nothing(self):
        # Fail closed: --static-mcp with a name sbx does not know fails the
        # entire create.
        self.build_checkout()
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[2:4] == ["mcp", "ls"]:
                return FakeCompletedProcess(1)
            return self.fake_run()(cmd, **kwargs)

        with mock.patch.object(c.shutil, "which", return_value="/usr/bin/node"), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(c.ensure_host_mcp_servers(run=run, root=self.root), [])
        self.assertNotIn("add", [cmd[2] for cmd in calls if cmd[0] == c.WMF_SBX])
        self.assertIn("could not read the host's MCP store", err.getvalue())

    def test_an_existing_registration_is_never_overwritten(self):
        # It may be the engineer's own, with credentials or paths this has
        # no business replacing, and nothing tells theirs from ours.
        self.build_checkout()
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[2:4] == ["mcp", "ls"]:
                return FakeCompletedProcess(
                    0, stdout=json.dumps({"servers": [{"name": "gerrit"}]}))
            if cmd[2:4] == ["mcp", "add"]:
                return FakeCompletedProcess(0)
            return self.fake_run()(cmd, **kwargs)

        with mock.patch.object(c.shutil, "which", return_value="/usr/bin/node"), \
             contextlib.redirect_stderr(io.StringIO()):
            registered = c.ensure_host_mcp_servers(run=run, root=self.root)
        self.assertEqual(registered, ["gerrit", "phabricator"])
        added = [cmd for cmd in calls if cmd[2:4] == ["mcp", "add"]]
        self.assertEqual([cmd[4] for cmd in added], ["phabricator"])

    def test_a_failed_add_leaves_that_server_out_of_the_kit(self):
        self.build_checkout()

        def run(cmd, **kwargs):
            if cmd[2:4] == ["mcp", "ls"]:
                return FakeCompletedProcess(0, stdout='{"servers": []}')
            if cmd[2:4] == ["mcp", "add"]:
                return FakeCompletedProcess(0 if cmd[4] == "gerrit" else 1)
            return self.fake_run()(cmd, **kwargs)

        with mock.patch.object(c.shutil, "which", return_value="/usr/bin/node"), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            registered = c.ensure_host_mcp_servers(run=run, root=self.root)
        self.assertEqual(registered, ["gerrit"])
        self.assertIn("phabricator", err.getvalue())


if __name__ == "__main__":
    unittest.main()
