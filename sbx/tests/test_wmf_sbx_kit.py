#!/usr/bin/env python3
"""Unit tests for wmf_sbx_kit.py -- run with:
  python3 -m unittest discover -s sbx/tests -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.kit as k  # noqa: E402
import wmf_sbx.resolve as r  # noqa: E402
import wmf_sbx.setup as setup_mod  # noqa: E402


def setup_install_command(spec):
    """The install step that runs wmf-sbx-setup's main (plan) form.

    By content, not by position: these tests used to index `install[1]`,
    and inserting one step ahead of it broke six of them at once (§71).
    The flag forms (--settings, --mcp, --exec-bits) are other steps."""
    for step in spec["setup"]["install"]:
        command = step["command"]
        if "wmf-sbx-setup" in command and " --" not in command:
            return command
    raise AssertionError("no wmf-sbx-setup install step in this spec")


@unittest.skipUnless(r.yaml is not None, "PyYAML not installed")
class WriteKitDirTests(unittest.TestCase):


    def test_it_corrects_the_sbx_boilerplate_it_shares_context_with(self):
        """sbx's own CLAUDE.md says this is 'direct mode' and that commits
        reach the host immediately (§73). Ours is the only thing in the
        sandbox that can say otherwise, so it has to."""
        text = k.HOME_CLAUDE_MD
        # The probe an agent would run, and the claim it would believe.
        self.assertIn("/run/sandbox/source", text)
        self.assertIn("direct mode", text)
        # Whatever the wording, it must not leave the false claim standing.
        self.assertRegex(text, r"(?s)direct mode.{0,400}(false|wrong)")

    def test_it_says_how_the_host_names_this_sandbox_without_guessing(self):
        text = k.HOME_CLAUDE_MD
        self.assertIn("$SANDBOX_NAME", text)
        # The remote is the sandbox name verbatim (remotes.remote_name_for),
        # so a `sandbox-` prefix is the out-of-date name the sbx boilerplate
        # still uses. Quoting it to correct it is fine; telling anyone to
        # fetch from it is not.
        self.assertNotIn("fetch sandbox-", text)

    def test_it_tells_the_agent_to_commit_before_stopping(self):
        """Work that is never committed never leaves the sandbox: the host
        sees the clone's HEAD over git://, not the working tree (§73)."""
        text = k.HOME_CLAUDE_MD
        self.assertRegex(text, r"(?i)commit before you stop|end your turn with a commit")
        # …and that it is reversible, so "I'd rather wait" has no foothold.
        self.assertIn("--amend", text)


class WorkspaceClaudeMdTests(unittest.TestCase):
    """sbx/NOTES.md §95: name the path of sbx's CLAUDE.md, and ship the
    edits that correct it."""

    def test_names_the_file_in_the_parent_of_the_primary_workspace(self):
        plan = k.build_plan(
            [("gerrit:mediawiki/extensions/Cite", "/home/c/Extensions/Cite"),
             ("gerrit:mediawiki/core", "/home/c/core")], host_home="/home/c")
        text = k.home_claude_md(plan)
        self.assertIn("`/home/c/Extensions/CLAUDE.md`", text)
        self.assertNotIn(k.WORKSPACE_CLAUDE_MD_TOKEN, text)

    def test_without_a_plan_it_says_where_to_look(self):
        text = k.home_claude_md(None)
        self.assertNotIn(k.WORKSPACE_CLAUDE_MD_TOKEN, text)
        self.assertIn("parent directory of the directory you started in", text)

    def test_it_says_the_file_is_in_context_and_in_no_repo(self):
        text = k.HOME_CLAUDE_MD
        self.assertIn("already\nin your context", text)
        self.assertIn("do not edit it\nor commit it", text)
        self.assertIn("Where the two files disagree, this one is right.", text)


class PluginTreeTests(unittest.TestCase):
    """sbx/DESIGN-plugin-integration.md §5 step 3 -- the plugin arrives as
    a marketplace the kit writes, because ~/.claude/skills is an sbx mount
    and a kit cannot put anything there."""

    def test_the_skill_list_comes_from_the_directory(self):
        skills = k.plugin_skills()
        self.assertIn("run-tests", skills)
        # The skill that was in the directory and in neither of the two
        # hand-maintained lists (sbx/NOTES.md §56.4). Reading the directory
        # is the whole point.
        self.assertIn("standalone-vuln-audit", skills)

    def test_the_tree_is_the_plugin_and_not_the_host_launchers(self):
        files = k.plugin_files()
        self.assertIn(os.path.join(".claude-plugin", "plugin.json"), files)
        self.assertIn(os.path.join("hooks", "hooks.json"), files)
        self.assertIn(os.path.join("skills", "run-tests", "SKILL.md"), files)
        self.assertIn(os.path.join("bin", "session-start.sh"), files)
        # hooks.json invokes session-start.sh and nothing else in bin/; the
        # rest are host-side launchers that would only mislead the agent.
        self.assertNotIn(os.path.join("bin", "claude"), files)
        self.assertNotIn(os.path.join("bin", "launch-docker-broker"), files)
        # init-project reads ${CLAUDE_PLUGIN_ROOT}/templates/.
        self.assertIn(os.path.join("templates", "CLAUDE.md"), files)


    def test_the_overlay_ships_as_part_of_the_plugin_tree(self):
        # sbx/DESIGN-testing-instructions.md §6.1: plugin text only an sbx
        # sandbox reads lives under sbx/, not in the shared tree.
        version = k.plugin_version()
        with tempfile.TemporaryDirectory() as tmp:
            overlay = os.path.join(tmp, "plugin-overlay")
            rel = os.path.join("hooks", "context", "sbx", "environment.txt")
            os.makedirs(os.path.dirname(os.path.join(overlay, rel)))
            with open(os.path.join(overlay, rel), "w", encoding="utf-8") as f:
                f.write("- the sbx bullet\n")
            self.assertEqual(k.plugin_overlay_files(overlay), [rel])
            self.assertIn(rel, k.plugin_files(overlay_dir=overlay))

            home = os.path.join(tmp, "files", "home")
            k.write_plugin_tree(home, overlay_dir=overlay)
            for tree in k.plugin_tree_dests(version):
                with open(os.path.join(home, tree, rel), encoding="utf-8") as f:
                    self.assertEqual(f.read(), "- the sbx bullet\n")

    def test_the_overlay_wins_over_the_repos_own_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            overlay = os.path.join(tmp, "plugin-overlay")
            rel = os.path.join("skills", "run-tests", "SKILL.md")
            os.makedirs(os.path.dirname(os.path.join(overlay, rel)))
            with open(os.path.join(overlay, rel), "w", encoding="utf-8") as f:
                f.write("the sbx skill\n")
            sources = k.plugin_file_sources(overlay_dir=overlay)
            self.assertEqual(sources[rel], os.path.join(overlay, rel))

            home = os.path.join(tmp, "files", "home")
            # An empty patch directory: this test is about the overlay, and
            # the real patches expect the repo's text, not this stub.
            empty = os.path.join(tmp, "patches")
            os.makedirs(empty)
            k.write_plugin_tree(home, overlay_dir=overlay, patch_dir=empty)
            tree = k.plugin_tree_dests()[0]
            with open(os.path.join(home, tree, rel), encoding="utf-8") as f:
                self.assertEqual(f.read(), "the sbx skill\n")

    def test_the_shipped_hook_speaks_for_the_sbx_backend(self):
        # The end of the overlay: the tree the kit writes must make
        # bin/session-start.sh emit the sbx paragraph under the backend
        # kit.py sets, and no nono text. tests/test-templates.sh cannot
        # assert this any more -- the sbx text is not in the shared tree.
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "files", "home")
            k.write_plugin_tree(home)
            hook = os.path.join(home, k.plugin_tree_dests()[0],
                                "bin", "session-start.sh")
            done = subprocess.run(
                ["bash", hook], capture_output=True, text=True,
                env={**os.environ, "WMF_CLAUDE_SANDBOX_BACKEND": "sbx"})
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("sandboxed by sbx", done.stdout)
            self.assertNotIn("sandboxed by nono", done.stdout)
            # The environment bullets come from the same backend directory.
            self.assertIn("composer serve", done.stdout)
            self.assertNotIn("no sandbox context", done.stderr)
            # Nor any nono-only claim: method rules, launcher flags, or MCP
            # servers the kit does not register.
            for nono_only in ("read-only (GET/HEAD)", "--allow-post",
                              "--local-db", "bin/claude --local-web",
                              "mcp__gitlab__", "wmf-engineer profile"):
                self.assertNotIn(nono_only, done.stdout)

    def test_a_version_bump_that_misses_the_wiring_is_caught_here(self):
        # installed-plugin.json spells the version out and has no $VERSION
        # to expand, so a plugin.json bump alone would put the tree in a
        # cache directory installed_plugins.json does not name.
        with self.assertRaises(RuntimeError) as caught:
            k.plugin_state_files(version="9.9.9")
        self.assertIn("plugin.json", str(caught.exception))


class PluginPatchTests(unittest.TestCase):
    """sbx/DESIGN-testing-instructions.md §6.2 -- a file the nono backend
    shares keeps its sbx correction as a patch, applied to the copy the
    kit installs and to nothing in the repo."""

    REL = os.path.join("skills", "run-tests", "SKILL.md")
    BEFORE = "one\ntwo\nthree\n"

    def stage(self, tmp, content=None):
        """A one-file plugin tree, staged the way write_plugin_tree
        stages the real one."""
        tree = os.path.join(tmp, "tree")
        os.makedirs(os.path.dirname(os.path.join(tree, self.REL)))
        with open(os.path.join(tree, self.REL), "w", encoding="utf-8") as f:
            f.write(self.BEFORE if content is None else content)
        return tree

    def patch_dir(self, tmp, name, body):
        patches = os.path.join(tmp, "patches")
        os.makedirs(patches, exist_ok=True)
        with open(os.path.join(patches, name), "w", encoding="utf-8") as f:
            f.write(body)
        return patches

    def diff(self, rel=None, old="two", new="TWO"):
        rel = rel or self.REL
        return (f"diff --git a/{rel} b/{rel}\n"
                f"--- a/{rel}\n"
                f"+++ b/{rel}\n"
                "@@ -1,3 +1,3 @@\n"
                " one\n"
                f"-{old}\n"
                f"+{new}\n"
                " three\n")

    def test_a_patch_changes_the_staged_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = self.stage(tmp)
            patches = self.patch_dir(tmp, "01-entrypoint.patch", self.diff())
            applied = k.apply_plugin_patches(tree, {self.REL}, patches)
            self.assertEqual(applied, ["01-entrypoint.patch"])
            with open(os.path.join(tree, self.REL), encoding="utf-8") as f:
                self.assertEqual(f.read(), "one\nTWO\nthree\n")

    def test_the_order_is_the_name_order(self):
        # Two patches can touch one file, and then the order they were
        # made in is the only order that applies.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self.stage(tmp)
            patches = self.patch_dir(tmp, "02-second.patch",
                                     self.diff(old="TWO", new="deux"))
            self.patch_dir(tmp, "01-first.patch", self.diff())
            self.assertEqual(
                [os.path.basename(p) for p in k.plugin_patches(patches)],
                ["01-first.patch", "02-second.patch"])
            k.apply_plugin_patches(tree, {self.REL}, patches)
            with open(os.path.join(tree, self.REL), encoding="utf-8") as f:
                self.assertEqual(f.read(), "one\ndeux\nthree\n")

    def test_a_stale_patch_stops_the_build(self):
        # The case the mechanism exists for: upstream rewrote the lines we
        # correct. Shipping the file unpatched would hide that.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self.stage(tmp, content="one\nrewritten\nthree\n")
            patches = self.patch_dir(tmp, "01-entrypoint.patch", self.diff())
            with self.assertRaises(RuntimeError) as caught:
                k.apply_plugin_patches(tree, {self.REL}, patches)
            message = str(caught.exception)
            self.assertIn("01-entrypoint.patch", message)
            self.assertIn("Refresh the patch", message)

    def test_a_patch_outside_the_shipped_tree_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = self.stage(tmp)
            patches = self.patch_dir(tmp, "01-wiring.patch",
                                     self.diff(rel="wiring/settings-merge.json"))
            with self.assertRaises(RuntimeError) as caught:
                k.apply_plugin_patches(tree, {self.REL}, patches)
            self.assertIn("does not ship", str(caught.exception))

    def test_a_patch_that_adds_a_file_is_refused(self):
        # A new file belongs in the overlay, where it is readable as a
        # file instead of as a diff.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self.stage(tmp)
            rel = os.path.join("hooks", "context", "sbx", "environment.txt")
            body = (f"diff --git a/{rel} b/{rel}\n"
                    "new file mode 100644\n"
                    "--- /dev/null\n"
                    f"+++ b/{rel}\n"
                    "@@ -0,0 +1 @@\n"
                    "+- the sbx bullet\n")
            patches = self.patch_dir(tmp, "01-add.patch", body)
            with self.assertRaises(RuntimeError) as caught:
                k.apply_plugin_patches(tree, {self.REL, rel}, patches)
            self.assertIn("sbx/plugin-overlay/", str(caught.exception))

    def test_every_shipped_patch_applies_to_the_current_tree(self):
        # The canary for an upstream merge: it fails in the test suite,
        # before anyone creates a sandbox with a half-corrected plugin.
        sources = k.plugin_file_sources()
        with tempfile.TemporaryDirectory() as tmp:
            for rel, source in sources.items():
                dest = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy(source, dest)
            k.apply_plugin_patches(tmp, set(sources))


class SettingsPatchTests(unittest.TestCase):
    def test_it_enables_the_plugin_and_keeps_the_denies(self):
        patch = k.settings_patch()
        self.assertTrue(patch["enabledPlugins"]["wmf-claude@wikimedia"])
        deny = patch["permissions"]["deny"]
        self.assertIn("Bash(ssh:*)", deny)
        self.assertIn("Read(**/*.pem)", deny)
        self.assertEqual(set(patch["permissions"]), {"deny"})
        self.assertNotIn("sandbox", patch)


    def test_no_rule_is_spelled_in_a_way_claude_code_skips(self):
        """A rejected rule is skipped, not enforced -- §70.

        Both spellings shipped for months reading as protection and
        providing none. `claude --settings FILE doctor` is the authority
        (it lists what the running Claude Code rejected, with no API
        call); these two are just the ones that bit us."""
        for rule in k.settings_patch()["permissions"]["deny"]:
            # `:*` means prefix-match and may only end the pattern.
            # `Bash(find:* -exec*)` is rejected outright; `find *-exec*`
            # is the wildcard spelling that works.
            self.assertNotRegex(
                rule, r":\*[^)]", f"{rule}: `:*` must end the pattern")
            # Only Edit(path) is consulted by the file permission checks;
            # it covers Write and every other file-editing tool, and a
            # Write(path) rule is skipped with a warning at every startup.
            self.assertFalse(rule.startswith("Write("),
                             f"{rule}: use Edit(...), which covers Write")


    def test_the_claude_json_seed_is_a_merge_not_an_overwrite(self):
        """~/.claude.json holds everything else Claude Code knows about
        this install; the kit owns one key of it."""
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "claude.json")
            with open(target, "w", encoding="utf-8") as f:
                json.dump({"projects": {"/w/core": {"allowedTools": []}}}, f)
            patch = os.path.join(tmp, "patch.json")
            with open(patch, "w", encoding="utf-8") as f:
                json.dump(k.claude_json_patch(), f)
            setup_mod.run_settings([patch, target], run=lambda *a, **kw: mock.Mock(returncode=0))
            with open(target, encoding="utf-8") as f:
                merged = json.load(f)
        self.assertEqual(merged["projects"], {"/w/core": {"allowedTools": []}})
        self.assertIs(merged["hasSeenAutoDefaultNudge"], True)


class MediawikiEnvVarsTests(unittest.TestCase):
    def test_no_core_means_no_vars(self):
        self.assertEqual(k.mediawiki_env_vars([(None, "/w/Skins")]), {})

    def test_values_come_from_cores_own_install_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            core = os.path.join(tmp, "core")
            os.makedirs(core)
            with open(os.path.join(core, "composer.json"), "w", encoding="utf-8") as f:
                json.dump({"scripts": {"mw-install:sqlite":
                                       "install --server=http://localhost:4000 --scriptpath=/w"}}, f)
            self.assertEqual(
                k.mediawiki_env_vars([("gerrit:mediawiki/core", core)]),
                {"MW_SERVER": "http://localhost:4000",
                 "MW_SCRIPT_PATH": "/w",
                 "CHROME_BIN": k.CHROME_BIN,
                 "MEDIAWIKI_USER": "Admin",
                 "MEDIAWIKI_PASSWORD": "adminpassword",
                 "API_TESTING_CONFIG_FILE": os.path.join(
                     core, setup_mod.API_TESTING_CONFIG_NAME)},
            )

    def test_an_empty_script_path_is_exported_as_a_slash(self):
        # sbx/DESIGN-testing-instructions.md §2.6: the install's own
        # --scriptpath= is the empty string, which wdio-mediawiki rejects
        # before it runs a single test. Quibble exports `/` here too.
        with tempfile.TemporaryDirectory() as tmp:
            core = os.path.join(tmp, "core")
            os.makedirs(core)
            with open(os.path.join(core, "composer.json"), "w", encoding="utf-8") as f:
                json.dump({"scripts": {"mw-install:sqlite":
                                       "install --server=http://localhost:4000 --scriptpath="}}, f)
            variables = k.mediawiki_env_vars([("gerrit:mediawiki/core", core)])
            self.assertEqual(variables["MW_SCRIPT_PATH"], "/")
            # The install itself keeps the empty value: this is a docroot
            # wiki, and --scriptpath=/ would install a different one.
            self.assertEqual(setup_mod.parse_install_params(core)["scriptPath"], "")

    def test_the_api_testing_config_is_named_by_absolute_path(self):
        # An extension's own `npm run api-testing` runs in the extension,
        # so a relative name would never find core's config (§5.5).
        with tempfile.TemporaryDirectory() as tmp:
            core = os.path.join(tmp, "core")
            os.makedirs(core)
            variables = k.mediawiki_env_vars([("gerrit:mediawiki/core", core)])
            path = variables["API_TESTING_CONFIG_FILE"]
            self.assertTrue(os.path.isabs(path))
            self.assertEqual(os.path.basename(path),
                             setup_mod.API_TESTING_CONFIG_NAME)

    def test_the_chrome_download_hosts_are_allowed(self):
        # mw-install-browser resolves the version at the first host and
        # downloads from the second; a kit that omits them installs no
        # browser on a machine with a narrower local policy (§5.4).
        self.assertIn("googlechromelabs.github.io", k.EXTRA_DOMAINS)
        self.assertIn("storage.googleapis.com", k.EXTRA_DOMAINS)

    def test_only_the_cypress_download_hosts_are_allowed(self):
        # mw-install-cypress downloads from the first host, which
        # redirects to the second. The run-time API hosts stay out: the
        # tests pass without them, and they would get run data (§91).
        self.assertIn("download.cypress.io", k.EXTRA_DOMAINS)
        self.assertIn("cdn.cypress.io", k.EXTRA_DOMAINS)
        self.assertNotIn("*.cypress.io", k.EXTRA_DOMAINS)
        self.assertNotIn("api.cypress.io", k.EXTRA_DOMAINS)
        self.assertNotIn("cloud.cypress.io", k.EXTRA_DOMAINS)

    def test_the_kit_and_the_env_file_cannot_disagree(self):
        # Both go through parse_install_params, which is the point of the
        # one host-side import of the setup script.
        with tempfile.TemporaryDirectory() as tmp:
            core = os.path.join(tmp, "core")
            os.makedirs(core)
            with open(os.path.join(core, "composer.json"), "w", encoding="utf-8") as f:
                json.dump({"scripts": {"mw-install:sqlite":
                                       "install --server=http://localhost:1234 --scriptpath="}}, f)
            variables = k.mediawiki_env_vars([("gerrit:mediawiki/core", core)])
            contents = setup_mod.env_file_contents(
                setup_mod.parse_install_params(core), 1000, 1000)
            self.assertIn(f"MW_SERVER={variables['MW_SERVER']}\n", contents)


class HelperScriptTests(unittest.TestCase):

    def test_git_safe_reset_ships_with_git_review_check(self):
        # git-safe-reset calls git-review-check by bare name, so shipping
        # one without the other is a runtime failure, not a missing nicety.
        # The image build installs them (image.helper_files).
        self.assertIn("git-safe-reset", k.HELPER_SCRIPTS)
        self.assertIn("git-review-check", k.HELPER_SCRIPTS)

    def test_each_helper_comes_from_its_own_directory(self):
        # The names come from two trees: sbx/bin/ for what engineers also
        # run on the host, sbx/helpers/ for what only a sandbox runs.
        self.assertEqual(k.HELPER_SCRIPTS["mw-install-browser"],
                         k.SANDBOX_HELPER_DIR)
        self.assertEqual(k.HELPER_SCRIPTS["mw-install-cypress"],
                         k.SANDBOX_HELPER_DIR)
        self.assertEqual(k.HELPER_SCRIPTS["git-safe-reset"],
                         k.HELPER_SCRIPT_DIR)
        for name, source_dir in k.HELPER_SCRIPTS.items():
            self.assertTrue(os.path.exists(os.path.join(source_dir, name)),
                            f"{name} is not in {source_dir}")

    def test_the_helpers_are_valid_bash(self):
        for name, source_dir in k.HELPER_SCRIPTS.items():
            done = subprocess.run(["bash", "-n", os.path.join(source_dir, name)],
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, f"{name}: {done.stderr}")


class TestingGuideTests(unittest.TestCase):

    def test_claude_md_sends_the_agent_to_the_guide(self):
        self.assertIn("## Running tests", k.HOME_CLAUDE_MD)
        self.assertIn("~/MEDIAWIKI-TESTING.md", k.HOME_CLAUDE_MD)
        # The three entry points the section exists to name.
        self.assertIn("composer phpunit:entrypoint", k.HOME_CLAUDE_MD)
        self.assertIn("mw-install-browser", k.HOME_CLAUDE_MD)
        self.assertIn("composer serve", k.HOME_CLAUDE_MD)

    def test_claude_md_offers_cypress_but_does_not_require_it(self):
        # The binary is 800 MB, so the agent installs it only when the
        # repo's Cypress specs are likely to cover the change.
        self.assertIn("mw-install-cypress", k.HOME_CLAUDE_MD)
        self.assertIn("Install it only when", k.HOME_CLAUDE_MD)
        self.assertNotIn("cannot run here", k.HOME_CLAUDE_MD)


class BuildPlanTests(unittest.TestCase):
    RESOLVED = [
        ("gerrit:mediawiki/extensions/Cite", "/home/c/Wikimedia/Extensions/Cite"),
        ("gerrit:mediawiki/core", "/home/c/Wikimedia/core"),
    ]

    def test_shape(self):
        plan = k.build_plan(self.RESOLVED, host_home="/home/c", daemon_port=9977)
        self.assertEqual(plan["version"], k.PLAN_VERSION)
        self.assertEqual(plan["hostHome"], "/home/c")
        self.assertEqual(plan["daemonPort"], "9977")
        self.assertEqual(plan["primary"], "/home/c/Wikimedia/Extensions/Cite")
        self.assertEqual(len(plan["repos"]), 2)

    def test_link_plan_is_attached_per_repo(self):
        plan = k.build_plan(
            self.RESOLVED, host_home="/home/c",
            links={"/home/c/Wikimedia/Extensions/Cite": ("Cite", "extensions")},
        )
        self.assertEqual(plan["repos"][0]["linkName"], "Cite")
        self.assertEqual(plan["repos"][0]["linkDir"], "extensions")
        # core is never linked into itself.
        self.assertIsNone(plan["repos"][1]["linkName"])

    def test_readonly_dirs_are_marked(self):
        plan = k.build_plan(
            self.RESOLVED, host_home="/home/c",
            readonly_dirs={"/home/c/Wikimedia/core"},
        )
        self.assertFalse(plan["repos"][0]["readOnly"])
        self.assertTrue(plan["repos"][1]["readOnly"])

    def test_upstream_urls_are_attached_per_repo(self):
        # What lets the clone call Gerrit `origin` and the host mirror
        # `local`; absent means "keep the mirror as origin".
        plan = k.build_plan(
            self.RESOLVED, host_home="/home/c",
            upstreams={"/home/c/Wikimedia/core":
                       "https://gerrit.wikimedia.org/r/mediawiki/core"},
        )
        self.assertIsNone(plan["repos"][0]["upstreamUrl"])
        self.assertEqual(plan["repos"][1]["upstreamUrl"],
                         "https://gerrit.wikimedia.org/r/mediawiki/core")

    def test_requested_repos_are_flagged(self):
        # The flag the setup script reads to decide which clones keep the
        # host's branch instead of being reset to upstream master.
        plan = k.build_plan(
            self.RESOLVED, host_home="/home/c",
            requested={"/home/c/Wikimedia/Extensions/Cite"},
        )
        self.assertTrue(plan["repos"][0]["requested"])
        self.assertFalse(plan["repos"][1]["requested"])
        self.assertFalse(plan["resetAll"])

    def test_a_symlinked_home_is_resolved(self):
        # sbx/NOTES.md §66: repo directories arrive realpath'd, and
        # host_home is what they're made relative to for the parallel
        # tree. A symlinked $HOME would put every repo "outside" it.
        with tempfile.TemporaryDirectory() as tmp:
            # realpath the tmpdir itself: on macOS /tmp is a symlink.
            tmp = os.path.realpath(tmp)
            real = os.path.join(tmp, "real-home")
            link = os.path.join(tmp, "link-home")
            os.makedirs(real)
            os.symlink(real, link)
            resolved = [("gerrit:mediawiki/core",
                         os.path.join(real, "Wikimedia", "core"))]
            with mock.patch.dict(os.environ, {"HOME": link}):
                plan = k.build_plan(resolved)
        self.assertEqual(plan["hostHome"], real)

    def test_reset_all_rides_in_the_plan(self):
        plan = k.build_plan(self.RESOLVED, host_home="/home/c", reset_all=True)
        self.assertTrue(plan["resetAll"])

    def test_explicit_primary_wins(self):
        plan = k.build_plan(self.RESOLVED, host_home="/home/c",
                            primary="/home/c/Wikimedia/core")
        self.assertEqual(plan["primary"], "/home/c/Wikimedia/core")

    def test_is_json_serialisable(self):
        # It's written into the kit as a file; anything unserialisable
        # would only surface at sandbox-creation time.
        json.dumps(k.build_plan(self.RESOLVED, host_home="/home/c"))


if __name__ == "__main__":
    unittest.main()
