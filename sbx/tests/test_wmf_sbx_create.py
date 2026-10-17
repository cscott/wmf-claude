#!/usr/bin/env python3
"""Unit tests for wmf_sbx_create.py -- run with:
  python3 -m unittest discover -s sbx/tests -v
"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.create as c  # noqa: E402
import wmf_sbx.resolve as r  # noqa: E402
import isolation  # noqa: E402
from fakes import Done  # noqa: E402


def setUpModule():
    isolation.isolate()


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
        run = lambda argv, **kw: Done(1)
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
            return Done(0, stdout="https://gitlab.wikimedia.org/daniel/claudebox.git\n")

        plan = c.upstream_plan([(None, "/home/c/claudebox")], run=run)
        self.assertEqual(plan, {
            "/home/c/claudebox": "https://gitlab.wikimedia.org/daniel/claudebox.git",
        })

    def test_gitlab_fork_is_repointed_at_its_true_upstream(self):
        run = lambda argv, **kw: Done(1)
        plan = c.upstream_plan(
            [("gitlab:alice/wmf-claude", "/home/c/wmf-claude")], run=run,
            gitlab_upstream=lambda path: "repos/psi/wmf-claude")
        self.assertEqual(plan, {
            "/home/c/wmf-claude": "https://gitlab.wikimedia.org/repos/psi/wmf-claude.git"})

    def test_gitlab_outage_keeps_the_canonicals_own_url(self):
        def down(path):
            raise c.resolve_mod.ResolutionError("offline")
        run = lambda argv, **kw: Done(1)
        plan = c.upstream_plan(
            [("gitlab:alice/wmf-claude", "/home/c/wmf-claude")], run=run,
            gitlab_upstream=down)
        self.assertEqual(plan, {
            "/home/c/wmf-claude": "https://gitlab.wikimedia.org/alice/wmf-claude.git"})


class HostUpstreamUrlTests(unittest.TestCase):
    def test_returns_http_origin(self):
        run = lambda argv, **kw: Done(0, stdout="https://example.org/foo.git\n")
        self.assertEqual(c.host_upstream_url("/home/c/foo", run=run), "https://example.org/foo.git")

    def test_none_on_failure(self):
        run = lambda argv, **kw: Done(1)
        self.assertIsNone(c.host_upstream_url("/home/c/foo", run=run))

    def test_none_on_ssh_origin(self):
        run = lambda argv, **kw: Done(0, stdout="git@example.org:foo.git\n")
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
        self.assertTrue(c.is_raw_path("/home/eng/Wikimedia/Skins"))

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


class PhabricatorUsernameTests(unittest.TestCase):
    """phabricator_username(): the username for the Phabricator MCP
    server, from $WMF_CLAUDE_PHAB_USER or the host's registration."""

    def fake_run(self, username="tester", calls=None):
        """`claude mcp get phabricator`; `username=None` fails."""
        def run(cmd, **kwargs):
            if calls is not None:
                calls.append((cmd, kwargs))
            if cmd[:3] == ["claude", "mcp", "get"]:
                if username is None:
                    return Done(1)
                return Done(
                    0, stdout=f"Command: env PHABRICATOR_USERNAME={username} node x.js")
            raise AssertionError(f"unexpected call: {cmd}")
        return run

    def test_the_environment_answers_first(self):
        self.assertEqual(
            c.phabricator_username(run=self.fake_run("registered"),
                                   env={"WMF_CLAUDE_PHAB_USER": "fromenv"}),
            "fromenv")

    def test_the_registration_answers_without_the_variable(self):
        calls = []
        self.assertEqual(
            c.phabricator_username(run=self.fake_run("registered", calls), env={}),
            "registered")
        # An unexpected prompt must fail at once with EOF, not hang.
        self.assertEqual(
            calls,
            [(["claude", "mcp", "get", "phabricator"],
              {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL})],
        )

    def test_nothing_anywhere_is_not_an_error(self):
        self.assertIsNone(c.phabricator_username(run=self.fake_run(None), env={}))


if __name__ == "__main__":
    unittest.main()
