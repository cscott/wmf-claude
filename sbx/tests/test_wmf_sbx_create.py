#!/usr/bin/env python3
"""Unit tests for wmf_sbx_create.py -- run with:
  python3 -m unittest discover -s sbx/tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.create as c  # noqa: E402
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



class PhabricatorUsernameTests(unittest.TestCase):
    """phabricator_username() and wmf_claude_config(): the username for
    the Phabricator MCP server, from $PHABRICATOR_USERNAME, the config
    file that bin/wmf-claude-setup writes, or an old registration. Kept
    from HostMcpTests when C1a removed the host-side MCP code."""

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

    def fake_run(self, username="cscott"):
        """`claude mcp get phabricator`, for the oldest source of the
        username (a registration made before config.json existed)."""
        def run(cmd, **kwargs):
            if cmd[:3] == ["claude", "mcp", "get"]:
                if username is None:
                    return FakeCompletedProcess(1)
                return FakeCompletedProcess(
                    0, stdout=f"Command: env PHABRICATOR_USERNAME={username} node x.js")
            raise AssertionError(f"unexpected call: {cmd}")
        return run

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




if __name__ == "__main__":
    unittest.main()
