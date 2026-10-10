#!/usr/bin/env python3
"""Unit tests for wmf_sbx_kit.py -- run with:
  python3 -m unittest discover -s sbx/tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.kit as k
import wmf_sbx.session as session  # noqa: E402
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
        self.assertIn("googlechromelabs.github.io", session.REGISTRY_DOMAINS)
        self.assertIn("storage.googleapis.com", session.REGISTRY_DOMAINS)

    def test_only_the_cypress_download_hosts_are_allowed(self):
        # mw-install-cypress downloads from the first host, which
        # redirects to the second. The run-time API hosts stay out: the
        # tests pass without them, and they would get run data (§91).
        self.assertIn("download.cypress.io", session.REGISTRY_DOMAINS)
        self.assertIn("cdn.cypress.io", session.REGISTRY_DOMAINS)
        self.assertNotIn("*.cypress.io", session.REGISTRY_DOMAINS)
        self.assertNotIn("api.cypress.io", session.REGISTRY_DOMAINS)
        self.assertNotIn("cloud.cypress.io", session.REGISTRY_DOMAINS)

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
        self.assertIn("## Running tests", session.RUNNING_TESTS_MD)
        self.assertIn("~/MEDIAWIKI-TESTING.md", session.RUNNING_TESTS_MD)
        # The three entry points the section exists to name.
        self.assertIn("composer phpunit:entrypoint", session.RUNNING_TESTS_MD)
        self.assertIn("mw-install-browser", session.RUNNING_TESTS_MD)
        self.assertIn("composer serve", session.RUNNING_TESTS_MD)

    def test_claude_md_offers_cypress_but_does_not_require_it(self):
        # The binary is 800 MB, so the agent installs it only when the
        # repo's Cypress specs are likely to cover the change.
        self.assertIn("mw-install-cypress", session.RUNNING_TESTS_MD)
        self.assertIn("Install it only when", session.RUNNING_TESTS_MD)
        self.assertNotIn("cannot run here", session.RUNNING_TESTS_MD)


class BuildPlanTests(unittest.TestCase):
    RESOLVED = [
        ("gerrit:mediawiki/extensions/Cite", "/home/c/Wikimedia/Extensions/Cite"),
        ("gerrit:mediawiki/core", "/home/c/Wikimedia/core"),
    ]

    def test_shape(self):
        plan = k.build_plan(self.RESOLVED)
        self.assertEqual(set(plan), {"version", "primary", "repos"})
        self.assertEqual(plan["version"], k.PLAN_VERSION)
        self.assertEqual(plan["primary"], "/home/c/Wikimedia/Extensions/Cite")
        self.assertEqual(set(plan["repos"][0]),
                         {"path", "canonical", "readOnly", "linkName", "linkDir"})

    def test_link_plan_is_attached_per_repo(self):
        plan = k.build_plan(
            self.RESOLVED,
            links={"/home/c/Wikimedia/Extensions/Cite": ("Cite", "extensions")},
        )
        self.assertEqual(plan["repos"][0]["linkName"], "Cite")
        self.assertEqual(plan["repos"][0]["linkDir"], "extensions")
        # core is never linked into itself.
        self.assertIsNone(plan["repos"][1]["linkName"])

    def test_readonly_dirs_are_marked(self):
        plan = k.build_plan(self.RESOLVED, readonly_dirs={"/home/c/Wikimedia/core"})
        self.assertFalse(plan["repos"][0]["readOnly"])
        self.assertTrue(plan["repos"][1]["readOnly"])

    def test_explicit_primary_wins(self):
        plan = k.build_plan(self.RESOLVED, primary="/home/c/Wikimedia/core")
        self.assertEqual(plan["primary"], "/home/c/Wikimedia/core")

    def test_is_json_serialisable(self):
        plan = k.build_plan(self.RESOLVED)
        self.assertEqual(json.loads(json.dumps(plan)), plan)


if __name__ == "__main__":
    unittest.main()
