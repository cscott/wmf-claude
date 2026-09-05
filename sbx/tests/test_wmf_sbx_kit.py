#!/usr/bin/env python3
"""Unit tests for wmf_sbx_kit.py -- run with:
  python3 -m unittest discover -s sbx/tests -v
"""

import json
import os
import re
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


class WikiFamilyDomainsTests(unittest.TestCase):
    def _write_profile(self, tmp, allow_domain):
        path = os.path.join(tmp, "wmf-engineer.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"network": {"allow_domain": allow_domain}}, f)
        return path

    def test_keeps_only_wiki_family_plain_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_profile(tmp, [
                "*.wikipedia.org", "*.wiktionary.org", "*.mediawiki.org",
                "*.wikinews.org",  # a domain the old hand-copied kit was missing
                "api.anthropic.com", "codesearch.wmcloud.org",
                {"domain": "docs.python.org", "endpoints": []},
            ])
            self.assertEqual(
                k.wiki_family_domains(path),
                ["*.mediawiki.org", "*.wikinews.org", "*.wikipedia.org", "*.wiktionary.org"],
            )

    def test_missing_profile_falls_back(self):
        domains = k.wiki_family_domains("/nonexistent/profile.json")
        self.assertIn("*.wikipedia.org", domains)
        self.assertIn("*.mediawiki.org", domains)

    def test_malformed_profile_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("not json")
            domains = k.wiki_family_domains(path)
            self.assertIn("*.wikipedia.org", domains)


class BuildKitSpecTests(unittest.TestCase):
    def _profile(self, tmp):
        path = os.path.join(tmp, "wmf-engineer.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"network": {"allow_domain": ["*.wikipedia.org"]}}, f)
        return path

    def test_wires_up_known_repo_environment_vars(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = k.build_kit_spec(
                [
                    ("gerrit:mediawiki/core", "/w/core"),
                    ("gerrit:mediawiki/services/parsoid", "/w/Parsoid"),
                    ("gerrit:mediawiki/extensions/Cite", "/w/Cite"),
                    (None, "/w/Skins"),  # raw path, no canonical -- no vars from it
                ],
                profile_path=self._profile(tmp),
                host_home="/nowhere",
            )
            variables = spec["environment"]["variables"]
            # MW_SERVER/MW_SCRIPT_PATH come from core too (see
            # mediawiki_env_vars), here at their defaults since /w/core
            # doesn't exist to be read. The paths stay literal because
            # they're outside host_home -- see the next test for the case
            # that actually matters.
            self.assertEqual(
                {k_: v for k_, v in variables.items() if k_.endswith(("PATH", "REPO", "PARSOID"))},
                {"MW_INSTALL_PATH": "/w/core", "MW_CORE_REPO": "/w/core",
                 "MW_SCRIPT_PATH": "/", "PARSOID": "/w/Parsoid"},
            )
            self.assertNotIn("CITE", " ".join(variables))

    def test_repo_environment_vars_use_the_literal_path_the_agent_works_in(self):
        # Since §39 the literal host path *is* the writable clone (sbx's
        # mirror is moved to ~/.sbx-originals and the clone bind-mounted
        # over the path it vacated), and it is the agent's own working
        # directory. $IP must name the same path composer's autoloader and
        # LocalSettings.php baked in, which is that one -- not
        # /home/agent/<rel>, the second path to the same directory.
        with tempfile.TemporaryDirectory() as tmp:
            spec = k.build_kit_spec(
                [
                    ("gerrit:mediawiki/core", "/home/cananian/Projects/Wikimedia/core"),
                    ("gerrit:mediawiki/services/parsoid", "/home/cananian/Parsoid"),
                    ("gerrit:mediawiki/vendor", "/elsewhere/vendor"),
                ],
                profile_path=self._profile(tmp),
                host_home="/home/cananian",
            )
            variables = spec["environment"]["variables"]
            self.assertEqual(
                variables["MW_INSTALL_PATH"], "/home/cananian/Projects/Wikimedia/core"
            )
            self.assertEqual(
                variables["MW_CORE_REPO"], "/home/cananian/Projects/Wikimedia/core"
            )
            self.assertEqual(variables["PARSOID"], "/home/cananian/Parsoid")
            # Outside host_home there is no parallel path at all; this one
            # is unchanged by the switch, which is the point of pinning it
            # in the same test.
            self.assertEqual(variables["MW_VENDOR_REPO"], "/elsewhere/vendor")

    def test_a_parsoid_clone_makes_unit_tests_load_the_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            def variables(resolved, readonly_dirs=None):
                return k.build_kit_spec(
                    resolved, profile_path=self._profile(tmp), host_home="/nowhere",
                    readonly_dirs=readonly_dirs,
                )["environment"]["variables"]
            core = ("gerrit:mediawiki/core", "/w/core")
            parsoid = ("gerrit:mediawiki/services/parsoid", "/w/Parsoid")
            self.assertEqual(
                variables([core, parsoid])["MEDIAWIKI_HAS_INTEGRATION_TESTS"], "1")
            # No Parsoid clone: unit tests keep the vendor copy, and
            # bootstrap.php keeps its own guess.
            self.assertNotIn("MEDIAWIKI_HAS_INTEGRATION_TESTS", variables([core]))
            # A :ro Parsoid is not wired into LocalSettings.php.
            self.assertNotIn("MEDIAWIKI_HAS_INTEGRATION_TESTS",
                             variables([core, parsoid], readonly_dirs=["/w/Parsoid"]))
            # No core, no bootstrap.php to read it.
            self.assertNotIn("MEDIAWIKI_HAS_INTEGRATION_TESTS", variables([parsoid]))

    def test_extra_environment_applied_on_top(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = k.build_kit_spec(
                [("gerrit:mediawiki/core", "/w/core")],
                extra_environment={"GERRITUA": "custom-ua", "MW_CORE_REPO": "/w/override"},
                profile_path=self._profile(tmp),
            )
            variables = spec["environment"]["variables"]
            self.assertEqual(variables["GERRITUA"], "custom-ua")
            self.assertEqual(variables["MW_CORE_REPO"], "/w/override")  # extras can override

    def test_declares_the_sandbox_backend_for_the_session_start_hook(self):
        # The wmf-claude plugin's SessionStart hook defaults to describing
        # nono. Every generated kit must say otherwise, or the hook tells
        # the model the session is enforced by something it isn't.
        with tempfile.TemporaryDirectory() as tmp:
            spec = k.build_kit_spec([], profile_path=self._profile(tmp))
            self.assertEqual(
                spec["environment"]["variables"]["WMF_CLAUDE_SANDBOX_BACKEND"], "sbx"
            )
            # WMF_CLAUDE_DOCKER_MODE stays unset: the hook's own default is
            # `none`, which is right for a kit that installs PHP into this
            # container rather than reaching another one through mwdocker.
            self.assertNotIn("WMF_CLAUDE_DOCKER_MODE", spec["environment"]["variables"])

    def test_extra_packages_appended_to_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = k.build_kit_spec([], extra_packages=["nano"], profile_path=self._profile(tmp))
            command = spec["setup"]["install"][0]["command"]
            self.assertIn("nano", command)
            for pkg in k.BASE_PACKAGES:
                self.assertIn(pkg, command)

    def test_setup_step_invokes_static_script_with_host_home_and_repos(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = k.build_kit_spec(
                [("gerrit:mediawiki/core", "/w/core"), (None, "/w/Skins")],
                profile_path=self._profile(tmp),
                host_home="/home/cananian",
            )
            # The step is found by content, not position or count: the
            # install list grows (§71.4).
            setup_cmd = setup_install_command(spec)
            self.assertIn("python3 /home/agent/wmf-sbx-setup", setup_cmd)
            self.assertIn("/home/cananian", setup_cmd)
            self.assertIn("/w/core", setup_cmd)
            self.assertIn("/w/Skins", setup_cmd)

    def test_setup_step_defaults_host_home_to_process_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = k.build_kit_spec([], profile_path=self._profile(tmp))
            setup_cmd = setup_install_command(spec)
            self.assertIn(os.path.expanduser("~"), setup_cmd)

    def test_setup_step_includes_daemon_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = k.build_kit_spec(
                [("gerrit:mediawiki/core", "/w/core")],
                profile_path=self._profile(tmp),
                host_home="/home/cananian",
                daemon_port=9418,
            )
            setup_cmd = setup_install_command(spec)
            self.assertIn("/home/agent/wmf-sbx-setup /home/cananian 9418 /w/core", setup_cmd)

    def test_setup_step_defaults_daemon_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = k.build_kit_spec([], profile_path=self._profile(tmp))
            setup_cmd = setup_install_command(spec)
            self.assertIn(str(k.DEFAULT_DAEMON_PORT), setup_cmd)

    def test_readonly_dirs_get_ro_suffix_others_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = k.build_kit_spec(
                [("gerrit:mediawiki/core", "/w/core"), (None, "/w/Skins")],
                profile_path=self._profile(tmp),
                host_home="/home/cananian",
                readonly_dirs={"/w/Skins"},
            )
            setup_cmd = setup_install_command(spec)
            self.assertIn("/w/Skins:ro", setup_cmd)
            self.assertIn("/w/core", setup_cmd)
            self.assertNotIn("/w/core:ro", setup_cmd)

    def test_network_domains_include_wiki_family_and_the_package_registries(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = k.build_kit_spec([], profile_path=self._profile(tmp))
            self.assertEqual(
                spec["permissions"]["network"]["allow"],
                sorted(["*.wikipedia.org"] + list(k.EXTRA_DOMAINS)),
            )
            # composer and npm are the two steps that would silently stop
            # working under a narrower local policy -- state the need.
            self.assertIn("packagist.org", spec["permissions"]["network"]["allow"])
            self.assertIn("registry.npmjs.org", spec["permissions"]["network"]["allow"])

    def test_top_level_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = k.build_kit_spec([], profile_path=self._profile(tmp))
            self.assertEqual(spec["schemaVersion"], "2")
            self.assertEqual(spec["kind"], "mixin")
            self.assertEqual(spec["name"], "mediawiki-kit")
            self.assertEqual(spec["requires"], {"agent": "claude"})

    def test_no_v1_field_survives_in_a_v2_spec(self):
        # "Legacy v1 fields in a schemaVersion: '2' spec are rejected
        # during decode" -- so a leftover key isn't ignored, it fails the
        # create. See the v1->v2 table in the kit-spec reference.
        with tempfile.TemporaryDirectory() as tmp:
            spec = k.build_kit_spec([], profile_path=self._profile(tmp))
        for key in ("network", "commands", "publishedPorts", "memory", "settings"):
            self.assertNotIn(key, spec)

    def test_the_daemon_comes_back_on_every_container_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = k.build_kit_spec(
                [], profile_path=self._profile(tmp), daemon_port=9418)
        startup = spec["setup"]["startup"]
        # The restore pass first, the daemon second: the daemon should
        # serve clones whose borrowed objects resolve (NOTES.md §40).
        # Only the order of those two is asserted -- the list grows, and a
        # length assertion here is a test that fails for the wrong reason
        # (§71.4).
        self.assertIn("--restore", startup[0]["command"])
        entry = startup[1]
        # A never-exiting daemon with background:false would block every
        # later startup command.
        self.assertTrue(entry["background"])
        # Not root: a root-owned daemon hits git's dubious-ownership check
        # on every fetch of an agent-owned clone (NOTES.md #21).
        self.assertEqual(entry["user"], "1000")
        # argv, run with no shell -- the guard needs its own `sh -c`.
        self.assertEqual(entry["command"][:2], ["sh", "-c"])
        self.assertIn("--port=9418", entry["command"][2])
        self.assertIn("9418", entry["command"][2])

    def test_the_mounts_are_restored_on_every_container_start(self):
        entry = k.restore_startup_command()
        # `mount` needs root, and startup commands default to the agent.
        self.assertEqual(entry["user"], "0")
        # Not background: the daemon entry after it depends on the clones'
        # alternates resolving again (NOTES.md §40).
        self.assertNotIn("background", entry)
        self.assertEqual(
            entry["command"], ["python3", k.SANDBOX_SETUP_SCRIPT, "--restore"]
        )

    def test_the_daemon_port_is_published_by_the_kit_itself(self):
        # Declared here rather than `sbx ports --publish`-ed after create,
        # so it's re-published on every container start instead of only
        # the one wmf-sbx-create happened to run after (NOTES.md §33.1).
        with tempfile.TemporaryDirectory() as tmp:
            spec = k.build_kit_spec(
                [], profile_path=self._profile(tmp), daemon_port=9418)
        self.assertEqual(
            spec["ports"], [{"container": 9418, "name": k.DAEMON_PORT_NAME}]
        )
        # No `protocol:`, per the reference -- though on the host it
        # published 127.0.0.1 *and* ::1 regardless (§36.3), so nothing
        # depends on the IPv4-only claim.
        self.assertNotIn("protocol", spec["ports"][0])

    def test_the_startup_daemon_is_the_same_command_the_install_step_starts(self):
        # Two hand-maintained copies of this argv would drift; the flags
        # are all load-bearing (see wmf_sbx_setup.start_daemon).
        entry = k.daemon_startup_command(port=9418)
        for arg in setup_mod.daemon_argv(setup_mod.SANDBOX_HOME, 9418):
            self.assertIn(arg, entry["command"][2])

    def test_the_startup_daemon_no_ops_when_one_is_already_listening(self):
        # Startup commands replay on every container restart, and at
        # create time the install step has already started one.
        entry = k.daemon_startup_command(port=9418)
        script = entry["command"][2]
        self.assertIn("connect_ex", script)
        # `if ...; then exec ...; fi`, not `... && exec ...`: skipping must
        # exit 0, or every restart logs a failed startup command.
        self.assertTrue(script.startswith("if "), script)
        self.assertTrue(script.endswith("; fi"), script)


@unittest.skipUnless(r.yaml is not None, "PyYAML not installed")
class WriteKitDirTests(unittest.TestCase):
    def test_writes_valid_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "kit")
            spec = {"schemaVersion": "2", "kind": "mixin", "name": "mediawiki-kit"}
            result = k.write_kit_dir(spec, dest)
            self.assertEqual(result, dest)
            with open(os.path.join(dest, "spec.yaml"), encoding="utf-8") as f:
                loaded = r.yaml.safe_load(f)
            self.assertEqual(loaded, spec)

    def test_creates_missing_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "a", "b", "kit")
            k.write_kit_dir({"name": "mediawiki-kit"}, dest)
            self.assertTrue(os.path.isfile(os.path.join(dest, "spec.yaml")))

    def test_copies_static_setup_script_into_files_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "kit")
            k.write_kit_dir({"name": "mediawiki-kit"}, dest)
            installed = os.path.join(dest, "files", "home", "wmf-sbx-setup")
            self.assertTrue(os.path.isfile(installed))
            with open(installed, encoding="utf-8") as f:
                installed_contents = f.read()
            with open(k.STATIC_SETUP_SCRIPT, encoding="utf-8") as f:
                source_contents = f.read()
            self.assertEqual(installed_contents, source_contents)

    def test_writes_home_claude_md_describing_the_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "kit")
            k.write_kit_dir({"name": "mediawiki-kit"}, dest)
            installed = os.path.join(dest, "files", "home", ".claude", "CLAUDE.md")
            self.assertTrue(os.path.isfile(installed))
            with open(installed, encoding="utf-8") as f:
                self.assertEqual(f.read(), k.home_claude_md(None))
            self.assertIn("/home/agent", k.HOME_CLAUDE_MD)
            self.assertIn(".sbx-originals", k.HOME_CLAUDE_MD)
            # The instruction this replaced told Claude to run `/cd`, which
            # a model cannot do -- it's a slash command the user types
            # (sbx/NOTES.md §30). Don't let it come back.
            self.assertNotIn("/cd", k.HOME_CLAUDE_MD)

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

    def test_startup_edits_the_file_as_root(self):
        startup = k.build_kit_spec([], host_home="/home/u")["setup"]["startup"]
        steps = [s for s in startup if "--claude-md" in (s.get("command") or [])]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["user"], "0")
        self.assertEqual(steps[0]["command"][-1], k.SANDBOX_CLAUDE_MD_EDITS)

    def test_install_does_not_edit_it(self):
        # sbx writes the file after the install steps (§95).
        install = k.build_kit_spec([], host_home="/home/u")["setup"]["install"]
        for step in install:
            self.assertNotIn("--claude-md", str(step.get("command")))

    def test_runs_after_the_mount_restore(self):
        startup = k.build_kit_spec([], host_home="/home/u")["setup"]["startup"]
        commands = [str(s.get("command")) for s in startup]
        restore = next(i for i, c in enumerate(commands) if "--restore" in c)
        edit = next(i for i, c in enumerate(commands) if "--claude-md" in c)
        self.assertLess(restore, edit)

    @unittest.skipUnless(r.yaml is not None, "PyYAML not installed")
    def test_write_kit_dir_ships_the_edits_and_the_templated_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "kit")
            plan = k.build_plan([("gerrit:mediawiki/core", "/home/c/W/core")],
                                host_home="/home/c")
            k.write_kit_dir({"name": "mediawiki-kit"}, dest, plan=plan)
            claude_dir = os.path.join(dest, "files", "home", ".claude")
            with open(os.path.join(claude_dir, "CLAUDE.md"), encoding="utf-8") as f:
                self.assertIn("`/home/c/W/CLAUDE.md`", f.read())
            shipped = os.path.join(
                claude_dir, os.path.basename(k.SANDBOX_CLAUDE_MD_EDITS))
            with open(shipped, encoding="utf-8") as f, \
                    open(k.CLAUDE_MD_EDITS_SOURCE, encoding="utf-8") as g:
                self.assertEqual(f.read(), g.read())

    def test_the_edits_apply_to_the_upstream_snapshot(self):
        """The seam with sbx. If this fails after `wmf-sbx
        refresh-claude-md` updated the snapshot, change edits.json."""
        with open(k.CLAUDE_MD_EDITS_SOURCE, encoding="utf-8") as f:
            edits = json.load(f)["edits"]
        with open(k.CLAUDE_MD_UPSTREAM_SNAPSHOT, encoding="utf-8") as f:
            upstream = f.read()
        new, failures = setup_mod.apply_claude_md_edits(upstream, edits)
        self.assertEqual(failures, [])
        self.assertNotIn("/run/sandbox/source ]; then", new)
        self.assertNotIn("Aspire", new)
        self.assertNotIn("gh pr create", new)
        # The correct advice stays.
        self.assertIn("## Network access", new)
        self.assertIn("## Critical: Shell Completions", new)


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

    def test_both_copies_are_written_and_the_hook_is_executable(self):
        version = k.plugin_version()
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "kit")
            k.write_kit_dir({"name": "mediawiki-kit"}, dest)
            home = os.path.join(dest, "files", "home")
            market, cache = k.plugin_tree_dests(version)
            self.assertIn(version, cache)
            for tree in (market, cache):
                manifest = os.path.join(home, tree, ".claude-plugin", "plugin.json")
                self.assertTrue(os.path.isfile(manifest), manifest)
                hook = os.path.join(home, tree, "bin", "session-start.sh")
                self.assertTrue(os.access(hook, os.X_OK), hook)
                self.assertTrue(os.path.isfile(
                    os.path.join(home, tree, "skills", "run-tests", "SKILL.md")))

    def test_the_state_files_point_at_the_trees_that_were_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "kit")
            k.write_kit_dir({"name": "mediawiki-kit"}, dest)
            home = os.path.join(dest, "files", "home")

            def load(*parts):
                with open(os.path.join(home, *parts), encoding="utf-8") as f:
                    return json.load(f)

            market, cache = k.plugin_tree_dests()
            known = load(".claude", "plugins", "known_marketplaces.json")
            location = known["wikimedia"]["installLocation"]
            marketplace_dir = os.path.dirname(os.path.dirname(market))
            self.assertEqual(location,
                             os.path.join(setup_mod.SANDBOX_HOME, marketplace_dir))
            self.assertNotIn("$HOME", location)
            self.assertTrue(os.path.isdir(os.path.join(home, marketplace_dir)))

            installed = load(".claude", "plugins", "installed_plugins.json")
            entry = installed["plugins"]["wmf-claude@wikimedia"][0]
            self.assertEqual(entry["installPath"],
                             os.path.join(setup_mod.SANDBOX_HOME, cache))
            self.assertTrue(os.path.isdir(os.path.join(home, cache)))

            # The marketplace manifest's relative `source` has to resolve to
            # the tree next to it, or `claude plugin list` names nothing.
            marketplace = load(".claude", "plugins", "marketplaces", "wikimedia",
                               ".claude-plugin", "marketplace.json")
            source = marketplace["plugins"][0]["source"]
            resolved = os.path.normpath(os.path.join(
                home, ".claude", "plugins", "marketplaces", "wikimedia", source))
            self.assertEqual(resolved, os.path.normpath(os.path.join(home, market)))

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


class ExecBitTests(unittest.TestCase):
    """sbx/NOTES.md §71 -- a kit's files/home drop does not carry the
    executable bit, so the kit says the mode again from inside."""

    def test_every_program_the_kit_stages_is_in_the_list(self):
        # The anti-drift test: ship a new executable and forget the list,
        # and it arrives in the sandbox unrunnable -- exactly the failure
        # that made this step exist.
        version = k.plugin_version()
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "kit")
            k.write_kit_dir({"name": "mediawiki-kit"}, dest)
            home = os.path.join(dest, "files", "home")
            staged = set()
            for dirpath, _dirnames, filenames in os.walk(home):
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    if os.stat(path).st_mode & 0o111:
                        staged.add(os.path.join(setup_mod.SANDBOX_HOME,
                                                os.path.relpath(path, home)))
        # install_helper_scripts restates 0755 on these itself, and the
        # ~/bin copies it leaves behind are never run.
        helpers = {os.path.join(setup_mod.SANDBOX_HOME, "bin", name)
                   for name in k.HELPER_SCRIPTS}
        self.assertEqual(staged - helpers, set(k.exec_bit_paths(version)))

    def test_a_kit_with_no_mcp_does_not_chmod_a_proxy_it_never_shipped(self):
        paths = k.exec_bit_paths(mcp=False)
        self.assertFalse([p for p in paths if k.MCP_PROXY in p])
        self.assertTrue(all(p.endswith("session-start.sh") for p in paths))

    def test_both_halves_run_the_same_command(self):
        # setup.install takes a shell string and setup.startup takes argv,
        # so the command is spelled twice; exec_bits_argv keeps them equal.
        argv = k.exec_bits_argv()
        self.assertEqual(k.exec_bits_startup_command()["command"], argv)
        self.assertEqual(
            k.exec_bits_install_step()["command"].split(),
            argv,  # no path here needs quoting, so a split is a fair test
        )
        # Root, not the agent: chmod needs ownership, and the drop's
        # ownership is sbx's business.
        self.assertNotIn("user", k.exec_bits_startup_command())

    def test_the_spec_fixes_the_bits_at_install_and_on_every_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = os.path.join(tmp, "wmf-engineer.json")
            with open(profile, "w", encoding="utf-8") as f:
                json.dump({"network": {"allow_domain": []}}, f)
            spec = k.build_kit_spec([], profile_path=profile)
        install = [s for s in spec["setup"]["install"] if "--exec-bits" in s["command"]]
        self.assertEqual(len(install), 1)
        # Before the long MediaWiki step: a create that dies in composer
        # still leaves a sandbox whose SessionStart hook runs.
        self.assertLess(spec["setup"]["install"].index(install[0]),
                        spec["setup"]["install"].index(
                            next(s for s in spec["setup"]["install"]
                                 if s["command"] == setup_install_command(spec))))
        self.assertEqual(
            len([s for s in spec["setup"]["startup"]
                 if "--exec-bits" in s["command"]]), 1)


class McpTests(unittest.TestCase):
    """sbx/DESIGN-plugin-integration.md §5 step 4 -- the servers run on the
    host (so their credentials stay there) and the kit ships a proxy."""

    def test_each_entry_runs_the_proxy_with_its_own_allowlist(self):
        config = k.mcp_config()
        self.assertEqual(sorted(config), ["gerrit", "phabricator"])
        entry = config["gerrit"]
        self.assertEqual(entry["command"],
                         os.path.join(k.SANDBOX_LOCAL_BIN, k.MCP_PROXY))
        # Named after the server it fronts, so the tools arrive as
        # mcp__gerrit__<tool> rather than mcp__mcp-gateway__<tool>.
        self.assertEqual(entry["args"][0], "gerrit")
        tools = entry["args"][entry["args"].index("--tools") + 1].split(",")
        self.assertEqual(tools, k.MCP_SERVER_TOOLS["gerrit"])
        # Sandboxes are created --static-mcp, where the gateway has no
        # mcp-add to call (NOTES.md §61.3). Say so rather than relying on
        # the proxy's fallback.
        self.assertIn("--no-add", entry["args"])

    def test_the_gerrit_allowlist_is_read_only(self):
        # The 15 of gerrit's 20 tools that write to Gerrit under the
        # engineer's own credential are the reason --tools is mandatory.
        for tool in ("abandon_change", "post_review_comment", "create_change",
                     "revert_change", "set_work_in_progress", "add_reviewer"):
            self.assertNotIn(tool, k.MCP_SERVER_TOOLS["gerrit"])

    def test_the_allowlists_cover_every_tool_the_plugin_names(self):
        """A skill or agent naming a tool the kit does not serve is a
        silently broken skill. Derived the other way round -- grepping the
        tree to *build* the allowlist -- a sentence like "never call
        mcp__gerrit__abandon_change" would become a grant, so the lists
        are explicit and this check is what keeps them honest."""
        pattern = re.compile(r"mcp__([a-z-]+)__([a-z_]+)")
        named = set()
        for rel in k.plugin_files():
            path = os.path.join(k.REPO_ROOT, rel)
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            named.update(pattern.findall(text))
        # If this is empty the regex has rotted, and the test would pass
        # while checking nothing.
        self.assertTrue(named)
        for server, tool in sorted(named):
            if server not in k.MCP_SERVER_TOOLS:
                continue  # mcp__mcp-gateway__*, mcp__chrome-devtools__*
            self.assertIn(tool, k.MCP_SERVER_TOOLS[server],
                          f"the plugin names mcp__{server}__{tool}, which "
                          f"MCP_SERVER_TOOLS[{server!r}] does not allow")

    def test_a_server_with_no_allowlist_gets_no_entry(self):
        # Fail closed: a name wmf-sbx-create found on the host but the kit
        # has no policy for is not registered at all.
        self.assertEqual(k.mcp_config(["phabricator", "nonesuch"]),
                         {"phabricator": k.mcp_config(["phabricator"])["phabricator"]})
        self.assertEqual(k.mcp_config([]), {})

    def test_the_registration_runs_at_install_and_at_every_start(self):
        spec = k.build_kit_spec([], host_home="/home/u",
                                mcp_servers=["phabricator"])
        install = [s for s in spec["setup"]["install"]
                   if k.SANDBOX_MCP_FILE in s["command"]]
        startup = [s for s in spec["setup"]["startup"]
                   if k.SANDBOX_MCP_FILE in s["command"]]
        self.assertEqual(len(install), 1)
        self.assertEqual(len(startup), 1)
        # install: a shell string. startup: an argv array, run with no
        # shell. A v2 spec that mixes them up fails to decode.
        self.assertIsInstance(install[0]["command"], str)
        self.assertIsInstance(startup[0]["command"], list)
        # It is the agent's ~/.claude.json; the install-time run is root
        # and goes through `sudo -u agent`.
        self.assertEqual(startup[0]["user"], "1000")

    def test_no_servers_means_no_mcp_anywhere_in_the_spec(self):
        spec = k.build_kit_spec([], host_home="/home/u", mcp_servers=[])
        for step in spec["setup"]["install"] + spec["setup"]["startup"]:
            self.assertNotIn(k.SANDBOX_MCP_FILE, step["command"])

    def test_the_proxy_and_its_registrations_ship_in_the_kit(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "kit")
            spec = k.build_kit_spec([], host_home="/home/u",
                                    mcp_servers=["gerrit"])
            k.write_kit_dir(spec, dest, mcp_servers=["gerrit"])
            home = os.path.join(dest, "files", "home")
            proxy = os.path.join(home, ".local", "bin", k.MCP_PROXY)
            self.assertTrue(os.access(proxy, os.X_OK), proxy)
            with open(os.path.join(home, os.path.relpath(
                    k.SANDBOX_MCP_FILE, setup_mod.SANDBOX_HOME)),
                    encoding="utf-8") as f:
                config = json.load(f)
            self.assertEqual(sorted(config), ["gerrit"])

    def test_a_kit_with_no_mcp_ships_no_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "kit")
            spec = k.build_kit_spec([], host_home="/home/u", mcp_servers=[])
            k.write_kit_dir(spec, dest, mcp_servers=[])
            proxy = os.path.join(dest, "files", "home", ".local", "bin",
                                 k.MCP_PROXY)
            self.assertFalse(os.path.exists(proxy))

    def test_the_gateway_meta_tools_stay_denied_under_static_mcp(self):
        # --static-mcp deletes mcp-add/mcp-find/mcp-config-set, but
        # code-mode survives it and is an arbitrary-JS path no Bash(...)
        # rule touches (NOTES.md §61.3).
        self.assertIn(k.GATEWAY_MCP_DENY,
                      k.settings_patch()["permissions"]["deny"])


class SettingsPatchTests(unittest.TestCase):
    def test_it_enables_the_plugin_and_keeps_the_denies(self):
        patch = k.settings_patch()
        self.assertTrue(patch["enabledPlugins"]["wmf-claude@wikimedia"])
        deny = patch["permissions"]["deny"]
        self.assertIn("Bash(ssh:*)", deny)
        self.assertIn("Read(**/*.pem)", deny)
        # The gateway's meta-tools (mcp-add, code-mode) let the agent mount
        # arbitrary MCP servers -- sbx/NOTES.md §60.4.
        self.assertIn(k.GATEWAY_MCP_DENY, deny)
        # allow/ask are redundant under bypassPermissions and `sandbox` is
        # nono's, not sbx's; shipping them would be noise in a file the
        # engineer may well read.
        self.assertEqual(set(patch["permissions"]), {"deny"})
        self.assertNotIn("sandbox", patch)
        # A single-slash absolute pattern matches nothing (§6 Q5).
        for rule in deny:
            self.assertNotRegex(rule, r"\(/[^/]")

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

    def test_it_is_shipped_where_the_setup_steps_read_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "kit")
            k.write_kit_dir({"name": "mediawiki-kit"}, dest)
            shipped = os.path.join(dest, "files", "home",
                                   os.path.relpath(k.SANDBOX_SETTINGS_PATCH,
                                                   setup_mod.SANDBOX_HOME))
            with open(shipped, encoding="utf-8") as f:
                self.assertEqual(json.load(f), k.settings_patch())

    def test_the_merge_runs_at_install_and_at_every_start(self):
        spec = k.build_kit_spec([], host_home="/home/u")
        install = spec["setup"]["install"]
        startup = spec["setup"]["startup"]
        # install takes a shell string, startup an argv array -- the two
        # halves of setup: disagree about this, and a decode error in a v2
        # spec is fatal.
        for step in install:
            self.assertIsInstance(step["command"], str)
        for step in startup:
            self.assertIsInstance(step["command"], list)
        patches = [s for s in install if k.SANDBOX_SETTINGS_PATCH in s["command"]]
        self.assertEqual(len(patches), 1)
        # `--settings` is the mechanism, not the target: the ~/.claude.json
        # seed borrows it too, so pick the step by the file it patches.
        merges = [s for s in startup if k.SANDBOX_SETTINGS_PATCH in s["command"]]
        self.assertEqual(len(merges), 1)
        # It is the agent's settings.json; the install-time run is root and
        # chowns after itself.
        self.assertEqual(merges[0]["user"], "1000")

    def test_the_claude_json_seed_patches_that_file_and_not_settings(self):
        """The auto-mode nudge is per-install state in ~/.claude.json, not
        a setting (§72.3). It borrows `--settings`, so the target argument
        is the only thing that keeps the two steps apart."""
        spec = k.build_kit_spec([], host_home="/home/u")
        seeds = [
            s for s in spec["setup"]["startup"]
            if k.SANDBOX_CLAUDE_JSON_PATCH in s["command"]
        ]
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0]["command"][-1], setup_mod.SANDBOX_CLAUDE_JSON)
        self.assertEqual(seeds[0]["user"], "1000")  # the agent's file
        installs = [
            s for s in spec["setup"]["install"]
            if k.SANDBOX_CLAUDE_JSON_PATCH in s["command"]
        ]
        self.assertEqual(len(installs), 1)
        self.assertIn(setup_mod.SANDBOX_CLAUDE_JSON, installs[0]["command"])

    def test_the_claude_json_seed_is_shipped_and_holds_only_the_nudge_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "kit")
            k.write_kit_dir({"name": "mediawiki-kit"}, dest)
            shipped = os.path.join(dest, "files", "home",
                                   os.path.relpath(k.SANDBOX_CLAUDE_JSON_PATCH,
                                                   setup_mod.SANDBOX_HOME))
            with open(shipped, encoding="utf-8") as f:
                patch = json.load(f)
        # One key, and nothing that changes a permission: the sandbox keeps
        # the bypass mode the nudge's own "no" would keep.
        self.assertEqual(patch, {"hasSeenAutoDefaultNudge": True})

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

    def test_startup_checks_that_the_plugin_actually_loaded(self):
        startup = k.build_kit_spec([], host_home="/home/u")["setup"]["startup"]
        check = startup[-1]
        self.assertEqual(check["user"], "1000")
        self.assertEqual(check["command"][:2], ["sh", "-c"])
        # Absolute, because sudo's PATH is not the agent's (§69.3).
        self.assertIn(setup_mod.SANDBOX_CLAUDE_BIN, check["command"][-1])


class PluginCheckScriptTests(unittest.TestCase):
    """Run the check's shell for real against a stub `claude`.

    The old test asserted the script *contained* `claude plugin list` and a
    grep, which is how it kept passing while the bare `claude` it named was
    on no PATH the step ran under and the `2>&1` into `grep -q` made "not
    found" indistinguishable from "plugin missing" (§72.1). Four stubs, four
    distinguishable messages -- and every one of them exits 0.
    """

    def _run(self, stub=None):
        """Render the script with SANDBOX_CLAUDE_BIN pointed at a path we
        control, then run it with `claude` either stubbed on PATH or gone."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            os.mkdir(bin_dir)
            # PATH will be bin_dir and nothing else (see below), so the two
            # utilities the script trims its output with have to be here.
            for tool in ("tr", "cut"):
                found = shutil.which(tool)
                self.assertIsNotNone(found, "%s is needed to run the check" % tool)
                os.symlink(found, os.path.join(bin_dir, tool))
            claude = os.path.join(bin_dir, "claude")
            if stub is not None:
                with open(claude, "w") as fh:
                    fh.write(stub)
                os.chmod(claude, 0o755)
            with mock.patch.object(setup_mod, "SANDBOX_CLAUDE_BIN", claude):
                script = k.plugin_check_startup_command()["command"][-1]
            proc = subprocess.run(
                # /bin/sh by absolute path because PATH below holds only
                # the stub: a PATH that still had a real `claude` on it
                # would be testing the developer's laptop.
                ["/bin/sh", "-c", script],
                capture_output=True,
                text=True,
                env={"PATH": bin_dir},
            )
        # Never fails the start, whatever it found.
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stderr

    def test_silent_when_the_plugin_is_listed(self):
        err = self._run('#!/bin/sh\necho "wikimedia:wmf-claude (enabled)"\n')
        self.assertEqual(err, "")

    def test_names_the_plugin_and_quotes_the_listing_when_it_is_missing(self):
        err = self._run('#!/bin/sh\necho "other:other-plugin (enabled)"\n')
        self.assertIn("the wmf-claude plugin did not load", err)
        self.assertIn("other:other-plugin", err)  # what it actually saw

    def test_a_failing_claude_is_not_reported_as_a_missing_plugin(self):
        err = self._run('#!/bin/sh\necho "claude: cannot run" >&2\nexit 3\n')
        self.assertIn("failed (exit 3)", err)
        self.assertIn("claude: cannot run", err)
        self.assertNotIn("did not load", err)

    def test_no_claude_at_all_says_so(self):
        err = self._run(stub=None)
        self.assertIn("no claude executable", err)
        self.assertNotIn("did not load", err)


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
    def test_every_helper_ships_executable_in_the_kit(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "kit")
            k.write_kit_dir({"name": "mediawiki-kit"}, dest)
            for name in k.HELPER_SCRIPTS:
                path = os.path.join(dest, "files", "home", "bin", name)
                self.assertTrue(os.access(path, os.X_OK), f"{name} is not executable")

    def test_the_shipped_names_are_the_ones_the_setup_script_installs(self):
        # git-safe-reset calls git-review-check by bare name, so shipping
        # one without the other is a runtime failure, not a missing nicety.
        self.assertEqual(tuple(k.HELPER_SCRIPTS), tuple(setup_mod.HELPER_SCRIPTS))

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
    def test_the_guide_ships_beside_claude_md_not_inside_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "kit")
            k.write_kit_dir({"name": "mediawiki-kit"}, dest)
            files_home = os.path.join(dest, "files", "home")
            with open(os.path.join(files_home, "MEDIAWIKI-TESTING.md"),
                      encoding="utf-8") as f:
                shipped = f.read()
            with open(k.TESTING_GUIDE_SOURCE, encoding="utf-8") as f:
                self.assertEqual(shipped, f.read())
            # ~/.claude/ is for context every session carries; the guide is
            # reference material to read when testing.
            self.assertFalse(os.path.exists(
                os.path.join(files_home, ".claude", "MEDIAWIKI-TESTING.md")))

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

    def test_setup_step_reads_the_plan_file_when_one_is_given(self):
        spec = k.build_kit_spec(
            self.RESOLVED, profile_path="/nonexistent/profile.json",
            plan=k.build_plan(self.RESOLVED, host_home="/home/c"),
        )
        command = setup_install_command(spec)
        self.assertIn(k.SANDBOX_PLAN_FILE, command)
        # The positional repo list is what the plan file replaces.
        self.assertNotIn("/home/c/Wikimedia/Extensions/Cite", command)


class WriteKitDirPlanTests(unittest.TestCase):
    def test_writes_the_plan_next_to_the_setup_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "kit")
            plan = k.build_plan([("gerrit:mediawiki/core", "/home/c/core")],
                                host_home="/home/c")
            k.write_kit_dir({"name": "mediawiki-kit"}, dest, plan=plan)
            written = os.path.join(dest, "files", "home", "wmf-sbx-plan.json")
            with open(written, encoding="utf-8") as f:
                self.assertEqual(json.load(f), plan)

    def test_refuses_to_write_a_plan_reading_kit_without_the_plan(self):
        # Otherwise this fails inside the sandbox, long after creation, as
        # "could not read the plan file" with nothing cloned.
        with tempfile.TemporaryDirectory() as tmp:
            spec = k.build_kit_spec(
                [("gerrit:mediawiki/core", "/home/c/core")],
                profile_path="/nonexistent/profile.json",
                plan=k.build_plan([("gerrit:mediawiki/core", "/home/c/core")]),
            )
            with self.assertRaises(RuntimeError):
                k.write_kit_dir(spec, os.path.join(tmp, "kit"))

    def test_the_plan_the_kit_writes_is_one_the_setup_script_accepts(self):
        # The two halves ship together; this is the seam between them.
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "kit")
            plan = k.build_plan(
                [("gerrit:mediawiki/extensions/Cite", "/home/c/Extensions/Cite")],
                host_home="/home/c", daemon_port=9977,
                links={"/home/c/Extensions/Cite": ("Cite", "extensions")},
            )
            k.write_kit_dir({"name": "mediawiki-kit"}, dest, plan=plan)
            written = os.path.join(dest, "files", "home", "wmf-sbx-plan.json")
            loaded = setup_mod.load_plan(written)
            self.assertEqual(loaded["repos"][0]["linkName"], "Cite")


if __name__ == "__main__":
    unittest.main()
