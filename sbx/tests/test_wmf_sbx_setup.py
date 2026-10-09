#!/usr/bin/env python3
"""Unit tests for wmf_sbx/setup.py -- run with:
  python3 -m unittest discover -s sbx/tests -v
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.setup as s  # noqa: E402

def patch_sandbox_home(testcase):
    """Point SANDBOX_HOME and the paths under it at a temporary directory.

    The real values are under /home/agent. On a host that directory does
    not exist, so a makedirs there fails. In a sandbox it does exist, so a
    test that does not patch it writes into the real sandbox home. Return
    the temporary directory."""
    tmp = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    patcher = unittest.mock.patch.multiple(s, SANDBOX_HOME=tmp.name)
    patcher.start()
    testcase.addCleanup(patcher.stop)
    return tmp.name


class FakeCompletedProcess:
    def __init__(self, returncode):
        self.returncode = returncode


class SetupLogTests(unittest.TestCase):
    """The report that survives `sbx create`'s collapsed install step --
    sbx/NOTES.md §32.1."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "setup.log")

    def test_everything_written_reaches_both_the_stream_and_the_file(self):
        stream = io.StringIO()
        log = s.SetupLog(stream, self.path)
        print("cloned /a -> /b", file=log)
        log.close()
        self.assertEqual(stream.getvalue(), "cloned /a -> /b\n")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "cloned /a -> /b\n")

    def test_problem_lines_are_collected_and_ordinary_ones_are_not(self):
        log = s.SetupLog(io.StringIO(), self.path)
        print("cloned /a -> /b", file=log)
        print("warning: could not rename origin", file=log)
        print("error: npm install failed", file=log)
        log.close()
        self.assertEqual(
            log.problems,
            ["warning: could not rename origin", "error: npm install failed"],
        )

    def test_a_line_split_across_writes_is_still_recognised(self):
        # print() with several arguments writes each separately, so a
        # scanner that only looked at whole write() calls would miss this.
        log = s.SetupLog(io.StringIO(), self.path)
        log.write("warning: could not ")
        log.write("chown /home/agent/core\n")
        log.close()
        self.assertEqual(log.problems, ["warning: could not chown /home/agent/core"])

    def test_a_trailing_line_without_a_newline_is_not_lost(self):
        log = s.SetupLog(io.StringIO(), self.path)
        log.write("error: died mid-line")
        log.close()
        self.assertEqual(log.problems, ["error: died mid-line"])

    def test_the_same_problem_is_only_reported_once(self):
        log = s.SetupLog(io.StringIO(), self.path)
        print("warning: same thing", file=log)
        log.record("warning: same thing")
        log.close()
        self.assertEqual(log.problems, ["warning: same thing"])

    def test_an_unopenable_log_costs_the_log_not_the_setup(self):
        stream = io.StringIO()
        log = s.SetupLog(stream, os.path.join(self.tmp.name, "nope", "setup.log"))
        print("cloned /a -> /b", file=log)
        log.close()
        self.assertIsNone(log.path)
        self.assertIn("could not open", stream.getvalue())
        self.assertIn("cloned /a -> /b", stream.getvalue())


class WriteStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "setup.status")

    def test_writes_the_version_exit_and_problems(self):
        self.assertTrue(s.write_status(self.path, 1, ["error: boom"], log_path="/var/log/x"))
        with open(self.path, encoding="utf-8") as f:
            status = json.load(f)
        self.assertEqual(status, {
            "version": s.STATUS_VERSION, "exit": 1,
            "problems": ["error: boom"], "log": "/var/log/x",
        })

    def test_an_unwritable_status_warns_rather_than_raising(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            written = s.write_status(
                os.path.join(self.tmp.name, "nope", "setup.status"), 0, []
            )
        self.assertFalse(written)
        self.assertIn("could not write", err.getvalue())


class LinkIntoCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.core = os.path.join(self.tmp.name, "core")
        os.makedirs(os.path.join(self.core, "extensions"))
        self.cite = os.path.join(self.tmp.name, "Extensions", "Cite")
        os.makedirs(self.cite)
        # link_into_core chowns with os.lchown, which needs root -- these
        # tests don't run as root, so record the calls and swallow them.
        self.chowned = []

        def fake_lchown(path, uid, gid):
            self.chowned.append((path, uid, gid))

        patched = unittest.mock.patch.object(os, "lchown", fake_lchown)
        patched.start()
        self.addCleanup(patched.stop)

    UNSET = object()

    def link(self, links=None, core=UNSET, core_readonly=False):
        links = links if links is not None else [("extensions", "Cite", self.cite)]
        core = self.core if core is self.UNSET else core
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            count = s.link_into_core(core, links, core_readonly=core_readonly)
        return count, err.getvalue()

    def test_creates_the_symlink_and_chowns_the_link_itself(self):
        count, _err = self.link()
        dest = os.path.join(self.core, "extensions", "Cite")
        self.assertEqual(count, 1)
        # The target is the *parallel clone*, never the read-only
        # host-mirrored original -- the whole point of the parallel tree.
        self.assertEqual(os.readlink(dest), self.cite)
        # lchown, not chown: the link itself, not the clone it points at
        # (which is already agent-owned). `sudo chown -h` was what this
        # used to shell out to, and it left every link root-owned in the
        # first real sandbox.
        self.assertEqual(self.chowned, [(dest, *s.sandbox_ids())])

    def test_chown_failure_warns_but_keeps_the_link(self):
        def boom(path, uid, gid):
            raise PermissionError(1, "Operation not permitted")

        with unittest.mock.patch.object(os, "lchown", boom):
            count, err = self.link()
        self.assertEqual(count, 1)
        self.assertIn("could not chown", err)
        self.assertTrue(os.path.islink(os.path.join(self.core, "extensions", "Cite")))

    def test_creates_a_missing_skins_directory(self):
        skin = os.path.join(self.tmp.name, "Vector")
        os.makedirs(skin)
        count, _err = self.link([("skins", "Vector", skin)])
        self.assertEqual(count, 1)
        self.assertEqual(os.readlink(os.path.join(self.core, "skins", "Vector")), skin)

    def test_existing_correct_link_is_left_alone(self):
        # wmf-sbx-resume runs this again; a second pass must be a no-op.
        self.link()
        self.chowned.clear()
        count, _err = self.link()
        self.assertEqual(count, 0)
        self.assertEqual(self.chowned, [])

    def test_stale_link_is_replaced(self):
        dest = os.path.join(self.core, "extensions", "Cite")
        os.symlink("/some/host/path/Cite", dest)
        count, _err = self.link()
        self.assertEqual(count, 1)
        self.assertEqual(os.readlink(dest), self.cite)

    def test_real_directory_is_never_clobbered(self):
        dest = os.path.join(self.core, "extensions", "Cite")
        os.makedirs(dest)
        with open(os.path.join(dest, "keep-me"), "w", encoding="utf-8") as f:
            f.write("work in progress\n")
        count, err = self.link()
        self.assertEqual(count, 0)
        self.assertIn("already exists and is not a symlink", err)
        self.assertTrue(os.path.isfile(os.path.join(dest, "keep-me")))

    def test_readonly_core_is_skipped_with_a_reason(self):
        count, err = self.link(core_readonly=True)
        self.assertEqual(count, 0)
        self.assertIn("read-only (':ro')", err)
        self.assertFalse(os.path.exists(os.path.join(self.core, "extensions", "Cite")))

    def test_no_core_in_the_sandbox_is_skipped_with_a_reason(self):
        count, err = self.link(core=None)
        self.assertEqual(count, 0)
        self.assertIn("no mediawiki/core", err)

    def test_nothing_to_link_says_nothing(self):
        count, err = self.link(links=[], core=None)
        self.assertEqual(count, 0)
        self.assertEqual(err, "")

    def test_one_bad_link_does_not_stop_the_rest(self):
        # Warn and continue: the git daemon the host fetches through is
        # worth more than an exact extensions/ directory.
        blocked = os.path.join(self.core, "extensions", "Blocked")
        os.makedirs(blocked)
        echo = os.path.join(self.tmp.name, "Extensions", "Echo")
        os.makedirs(echo)
        count, err = self.link([
            ("extensions", "Blocked", os.path.join(self.tmp.name, "Blocked")),
            ("extensions", "Echo", echo),
        ])
        self.assertEqual(count, 1)
        self.assertIn("warning:", err)
        self.assertTrue(os.path.islink(os.path.join(self.core, "extensions", "Echo")))


class AsAgentTests(unittest.TestCase):
    def test_runs_under_sudo_with_the_agents_own_home(self):
        seen = {}

        def fake_run(argv, cwd=None):
            seen["argv"], seen["cwd"] = argv, cwd
            return FakeCompletedProcess(0)

        s.as_agent(["composer", "update"], cwd="/w/core", run=fake_run)
        # -H, or composer's cache lands in /root and the files it writes
        # come out root-owned.
        self.assertEqual(seen["argv"], ["sudo", "-u", "agent", "-H", "composer", "update"])
        self.assertEqual(seen["cwd"], "/w/core")


class StepCommandTests(unittest.TestCase):
    """git_safe_reset / composer_update / npm_install / install_mediawiki --
    what they run, and when they decline to run at all."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        self.calls = []
        self.code = 0

    def fake_run(self, argv, cwd=None):
        self.calls.append((argv, cwd))
        return FakeCompletedProcess(self.code)

    def touch(self, name, content=""):
        with open(os.path.join(self.repo, name), "w", encoding="utf-8") as f:
            f.write(content)

    def argv(self):
        # Drop the sudo prefix; the rest is the interesting part.
        return self.calls[0][0][4:]


    def test_composer_update_is_skipped_without_a_composer_json(self):
        self.assertIsNone(s.composer_update(self.repo, run=self.fake_run))
        self.assertEqual(self.calls, [])

    def test_composer_update_runs_non_interactively(self):
        self.touch("composer.json", "{}")
        self.assertTrue(s.composer_update(self.repo, run=self.fake_run))
        # No tty in commands.install, and composer's progress bar renders
        # badly in the create log.
        self.assertEqual(self.argv(),
                         ["composer", "update", "--no-interaction", "--no-progress"])

    def test_npm_is_skipped_without_a_package_json(self):
        self.assertIsNone(s.npm_install(self.repo, run=self.fake_run))
        self.assertEqual(self.calls, [])

    def test_npm_ci_when_there_is_a_lockfile(self):
        self.touch("package.json", "{}")
        self.touch("package-lock.json", "{}")
        self.assertTrue(s.npm_install(self.repo, run=self.fake_run))
        # ci, not install: package-lock.json is tracked in core, and an
        # install that rewrites it leaves the clone dirty. The env prefix
        # keeps cypress from downloading an 800 MB binary at every create;
        # mw-install-cypress fetches it on demand (sbx/NOTES.md §91).
        self.assertEqual(
            self.argv(),
            ["env", "CYPRESS_INSTALL_BINARY=0", "npm", "ci", "--no-audit", "--no-fund"])

    def test_npm_install_when_there_is_no_lockfile(self):
        self.touch("package.json", "{}")
        self.assertTrue(s.npm_install(self.repo, run=self.fake_run))
        self.assertEqual(
            self.argv(),
            ["env", "CYPRESS_INSTALL_BINARY=0", "npm", "install", "--no-audit", "--no-fund"])

    def test_phpunit_config_is_skipped_without_a_composer_json(self):
        self.assertIsNone(s.phpunit_config(self.repo, run=self.fake_run))
        self.assertEqual(self.calls, [])

    def test_phpunit_config_runs_cores_own_script(self):
        # Core ships phpunit.xml.template, so a fresh checkout has no
        # config and `vendor/bin/phpunit <path>` loads no bootstrap
        # (sbx/DESIGN-testing-instructions.md §2.1).
        self.touch("composer.json", "{}")
        self.assertTrue(s.phpunit_config(self.repo, run=self.fake_run))
        self.assertEqual(self.argv(), ["composer", "phpunit:config"])
        self.assertEqual(self.calls[0][1], self.repo)

    def test_install_passes_with_extensions(self):
        self.assertTrue(s.install_mediawiki(self.repo, run=self.fake_run))
        # Without this flag CliInstaller enables every skin and no
        # extension, and the whole dependency walk buys nothing.
        self.assertEqual(self.argv(),
                         ["composer", "mw-install:sqlite", "--", "--with-extensions"])

    def test_install_is_skipped_when_localsettings_exists(self):
        self.touch("LocalSettings.php", "<?php")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(s.install_mediawiki(self.repo, run=self.fake_run))
        self.assertEqual(self.calls, [])
        self.assertIn("already exists", err.getvalue())


class LinkParsoidCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.core = os.path.join(self.tmp.name, "core")
        os.makedirs(self.core)
        self.parsoid_dest = os.path.join(self.tmp.name, "parsoid")

    def write_local_settings(self, body):
        path = os.path.join(self.core, "LocalSettings.php")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path

    def test_replaces_the_wfloadextension_line(self):
        path = self.write_local_settings(
            "<?php\n"
            "$wgDefaultSkin = \"vector-2022\";\n\n"
            "// Enabled skins.\n"
            "wfLoadSkin( 'Vector' );\n\n"
            "wfLoadExtension( 'Parsoid' );\n"
            "wfLoadExtension( 'Cite' );\n"
        )
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertTrue(s.link_parsoid_checkout(self.core, self.parsoid_dest))
        with open(path, encoding="utf-8") as f:
            contents = f.read()
        # The original single-line call is gone from its old spot...
        self.assertNotIn("wfLoadExtension( 'Parsoid' );\n", contents)
        # ...replaced by a block pointing at this sandbox's own checkout...
        self.assertIn(f"$parsoidInstallDir = '{self.parsoid_dest}';", contents)
        self.assertIn(
            "wfLoadExtension( 'Parsoid', \"$parsoidInstallDir/extension.json\" );", contents
        )
        # ...with no dead vendor-vs-checkout guard left in...
        self.assertNotIn("vendor/wikimedia/parsoid", contents)
        # ...placed ahead of every skin and extension load, not where the
        # original call was...
        self.assertLess(
            contents.index("$parsoidInstallDir ="), contents.index("// Enabled skins.")
        )
        self.assertLess(contents.index("// Enabled skins."), contents.index("wfLoadSkin"))
        # ...and every other line is untouched, in place.
        self.assertIn("wfLoadSkin( 'Vector' );", contents)
        self.assertIn("wfLoadExtension( 'Cite' );", contents)
        self.assertIn(f"linked Parsoid checkout ({self.parsoid_dest})", err.getvalue())

    def test_escapes_a_single_quote_or_backslash_in_the_path(self):
        # Unusual, but the path is not under this function's control, and a
        # raw interpolation would produce a syntactically broken
        # LocalSettings.php.
        tricky = os.path.join(self.tmp.name, "weird'dir\\here")
        self.write_local_settings("// Enabled skins.\nwfLoadExtension( 'Parsoid' );\n")
        self.assertTrue(s.link_parsoid_checkout(self.core, tricky))
        with open(os.path.join(self.core, "LocalSettings.php"), encoding="utf-8") as f:
            contents = f.read()
        escaped = self.tmp.name + "/weird\\'dir\\\\here"
        self.assertIn(f"$parsoidInstallDir = '{escaped}';", contents)

    def test_missing_marker_line_warns_and_leaves_the_file_alone(self):
        original = "<?php\nwfLoadExtension( 'Cite' );\n"
        self.write_local_settings(original)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertFalse(s.link_parsoid_checkout(self.core, self.parsoid_dest))
        with open(os.path.join(self.core, "LocalSettings.php"), encoding="utf-8") as f:
            self.assertEqual(f.read(), original)
        self.assertIn("no", err.getvalue())
        self.assertIn("Parsoid checkout not linked", err.getvalue())

    def test_missing_skins_marker_warns_and_leaves_the_file_alone(self):
        # The extension line is there, but not the "// Enabled skins."
        # comment mw-install:sqlite is expected to also have written --
        # an unexpectedly-shaped LocalSettings.php, so refuse rather than
        # guess where "before every skin and extension" would even mean.
        original = "<?php\nwfLoadExtension( 'Parsoid' );\n"
        self.write_local_settings(original)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertFalse(s.link_parsoid_checkout(self.core, self.parsoid_dest))
        with open(os.path.join(self.core, "LocalSettings.php"), encoding="utf-8") as f:
            self.assertEqual(f.read(), original)
        self.assertIn("Parsoid checkout not linked", err.getvalue())

    def test_missing_file_warns_instead_of_raising(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertFalse(s.link_parsoid_checkout(self.core, self.parsoid_dest))
        self.assertIn("could not read", err.getvalue())


class ParseInstallParamsTests(unittest.TestCase):
    SCRIPT = ("@php maintenance/run.php install --server=http://localhost:4000 "
              "--dbtype sqlite --with-developmentsettings --dbpath cache/ "
              "--scriptpath= --pass adminpassword MediaWiki Admin")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def core(self, composer=None):
        path = os.path.join(self.tmp.name, "core")
        os.makedirs(path, exist_ok=True)
        if composer is not None:
            with open(os.path.join(path, "composer.json"), "w", encoding="utf-8") as f:
                f.write(composer)
        return path

    def test_reads_the_installs_own_parameters(self):
        core = self.core(json.dumps({"scripts": {"mw-install:sqlite": self.SCRIPT}}))
        params = s.parse_install_params(core)
        self.assertEqual(params["server"], "http://localhost:4000")
        self.assertEqual(params["scriptPath"], "")  # docroot install; valid
        self.assertEqual(params["adminPassword"], "adminpassword")

    def test_a_list_valued_script_is_joined(self):
        core = self.core(json.dumps({"scripts": {"mw-install:sqlite": [
            "@php maintenance/run.php install",
            "--server=http://localhost:9999 --pass hunter2",
        ]}}))
        self.assertEqual(s.parse_install_params(core)["server"], "http://localhost:9999")

    def test_space_separated_flags_are_read_too(self):
        core = self.core(json.dumps({"scripts": {
            "mw-install:sqlite": "install --server http://localhost:1234"}}))
        self.assertEqual(s.parse_install_params(core)["server"], "http://localhost:1234")

    def test_missing_composer_json_falls_back_to_the_plan(self):
        params = s.parse_install_params(self.core(), fallback={"adminUser": "Someone"})
        self.assertEqual(params["adminUser"], "Someone")
        self.assertEqual(params["server"], s.DEFAULT_INSTALL_PARAMS["server"])

    def test_unparseable_composer_json_falls_back_rather_than_raising(self):
        core = self.core("{not json")
        self.assertEqual(s.parse_install_params(core), s.DEFAULT_INSTALL_PARAMS)

    def test_a_core_without_the_script_falls_back(self):
        core = self.core(json.dumps({"scripts": {"test": "phpunit"}}))
        self.assertEqual(s.parse_install_params(core), s.DEFAULT_INSTALL_PARAMS)


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.core = os.path.join(self.tmp.name, "core")
        os.makedirs(self.core)
        self.path = os.path.join(self.core, ".env")
        self.calls = []

    def fake_run(self, argv, cwd=None):
        self.calls.append(argv)
        return FakeCompletedProcess(0)

    def write(self, params=None):
        params = params or dict(s.DEFAULT_INSTALL_PARAMS)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            written = s.write_env_file(self.core, params, uid=1000, gid=1000,
                                       run=self.fake_run)
        return written, err.getvalue()

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def test_writes_the_installs_values_not_the_docker_ones(self):
        written, _err = self.write()
        self.assertTrue(written)
        # mediawiki-core-clean/.env says 8080 and /w and dockerpass; that
        # would point the harnesses at a port nothing listens on.
        self.assertIn("MW_SERVER=http://localhost:4000\n", self.read())
        self.assertIn("MEDIAWIKI_PASSWORD=adminpassword\n", self.read())

    def test_an_empty_script_path_is_written_as_a_slash(self):
        # The install's own --scriptpath= is empty, and wdio-mediawiki
        # rejects that value; Quibble writes `/` here too. See
        # sbx/DESIGN-testing-instructions.md §2.6.
        self.write()
        self.assertIn("MW_SCRIPT_PATH=/\n", self.read())

    def test_the_docker_port_follows_the_server_url(self):
        self.write({"server": "http://localhost:8080", "scriptPath": "/w"})
        self.assertIn("MW_DOCKER_PORT=8080\n", self.read())

    def test_uid_and_gid_are_not_hardcoded(self):
        with contextlib.redirect_stderr(io.StringIO()):
            s.write_env_file(self.core, dict(s.DEFAULT_INSTALL_PARAMS),
                             uid=4242, gid=4243, run=self.fake_run)
        self.assertIn("MW_DOCKER_UID=4242\n", self.read())
        self.assertIn("MW_DOCKER_GID=4243\n", self.read())

    def test_chowns_the_file_to_the_agent(self):
        self.write()
        self.assertEqual(self.calls, [["sudo", "chown", "agent:agent", self.path]])

    def test_rewriting_an_identical_file_is_a_no_op(self):
        self.write()
        self.calls.clear()
        written, _err = self.write()
        self.assertTrue(written)
        self.assertEqual(self.calls, [])

    def test_an_edited_env_is_left_alone(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("MW_SERVER=http://localhost:9999\n")
        written, err = self.write()
        self.assertFalse(written)
        self.assertIn("leaving it alone", err)
        self.assertEqual(self.read(), "MW_SERVER=http://localhost:9999\n")


class ComposerLocalTests(unittest.TestCase):
    """Core's composer.local.json -- quibble's merge of every extension's
    and skin's Composer dependencies into core's vendor/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.core = os.path.join(self.tmp.name, "core")
        os.makedirs(self.core)
        self.path = os.path.join(self.core, s.COMPOSER_LOCAL_NAME)
        self.calls = []

    def fake_run(self, argv, cwd=None):
        self.calls.append(argv)
        return FakeCompletedProcess(0)

    def write(self, parsoid_checkout=False):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            written = s.write_composer_local(
                self.core, parsoid_checkout=parsoid_checkout, run=self.fake_run)
        return written, err.getvalue()

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    QUIBBLE = {"extra": {"merge-plugin": {"include": [
        "extensions/*/composer.json", "skins/*/composer.json"]}}}

    def test_the_file_holds_quibbles_globs(self):
        written, err = self.write()
        self.assertTrue(written)
        self.assertIn("wrote", err)
        self.assertEqual(json.loads(self.read()), self.QUIBBLE)
        self.assertIn(["sudo", "chown", "agent:agent", self.path], self.calls)

    def test_a_parsoid_checkout_adds_the_classmap_exclusion(self):
        # Ours, not quibble's: without it, every core composer update
        # prints 359 warnings for the Parsoid checkout. Without a
        # checkout, the wiki needs the vendor copy, so there is no
        # exclusion then.
        self.write(parsoid_checkout=True)
        self.assertEqual(
            json.loads(self.read()),
            dict(self.QUIBBLE, autoload={
                "exclude-from-classmap": ["vendor/wikimedia/parsoid/"]}),
        )

    def test_the_other_form_is_ours_and_is_replaced(self):
        for before, after in ((False, True), (True, False)):
            with self.subTest(before=before, after=after):
                self.write(parsoid_checkout=before)
                written, err = self.write(parsoid_checkout=after)
                self.assertTrue(written)
                self.assertNotIn("leaving it alone", err)
                self.assertEqual(self.read(), s.composer_local_contents(after))

    def test_the_file_is_indented_with_tabs(self):
        # Core's eslint lints every JSON file, as for the api-testing
        # config.
        self.write(parsoid_checkout=True)
        indented = [line for line in self.read().splitlines() if line[:1].isspace()]
        self.assertTrue(indented)
        for line in indented:
            self.assertTrue(line.startswith("\t"), repr(line))

    def test_our_own_file_is_left_as_it_is(self):
        self.write()
        self.calls.clear()
        written, err = self.write()
        self.assertTrue(written)
        self.assertEqual(err, "")
        self.assertEqual(self.calls, [])

    def test_an_engineers_own_file_is_left_alone(self):
        mine = '{"extra": {"merge-plugin": {"include": ["extensions/Cite/composer.json"]}}}\n'
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(mine)
        written, err = self.write()
        self.assertFalse(written)
        self.assertIn("leaving it alone", err)
        self.assertIn("phan", err)
        self.assertEqual(self.read(), mine)


class ApiTestingConfigTests(unittest.TestCase):
    """sbx/DESIGN-testing-instructions.md §5.5 -- the config the
    api-testing suites need, written into core at create time."""

    SECRET = "22d812ebf36429193b72d9d54adcb2e62fba7b37658b69ed0f3aeabf88700b23"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.core = os.path.join(self.tmp.name, "core")
        os.makedirs(self.core)
        self.path = os.path.join(self.core, s.API_TESTING_CONFIG_NAME)
        self.calls = []

    def fake_run(self, argv, cwd=None):
        self.calls.append(argv)
        return FakeCompletedProcess(0)

    def local_settings(self, body=None):
        if body is None:
            body = f'<?php\n$wgSecretKey = "{self.SECRET}";\n'
        with open(os.path.join(self.core, "LocalSettings.php"), "w",
                  encoding="utf-8") as f:
            f.write(body)

    def write(self, params=None):
        params = params or dict(s.DEFAULT_INSTALL_PARAMS)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            written = s.write_api_testing_config(self.core, params, run=self.fake_run)
        return written, err.getvalue()

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_the_secret_key_comes_from_localsettings(self):
        self.local_settings()
        self.assertEqual(s.read_secret_key(self.core), self.SECRET)

    def test_single_quotes_read_the_same(self):
        self.local_settings(f"<?php\n$wgSecretKey = '{self.SECRET}';\n")
        self.assertEqual(s.read_secret_key(self.core), self.SECRET)

    def test_no_localsettings_means_no_key(self):
        self.assertIsNone(s.read_secret_key(self.core))

    def test_the_config_names_every_field_the_library_reads(self):
        self.local_settings()
        written, _err = self.write()
        self.assertTrue(written)
        config = self.read()
        # api-testing/lib/config.js strips the trailing slash and appends
        # `api.php` itself, and this is a docroot wiki, so the server root
        # is the whole base_uri.
        self.assertEqual(config["base_uri"], "http://localhost:4000/")
        self.assertEqual(config["main_page"], s.API_TESTING_MAIN_PAGE)
        self.assertEqual(config["root_user"],
                         {"name": "Admin", "password": "adminpassword"})
        self.assertEqual(config["secret_key"], self.SECRET)

    def test_the_config_is_indented_with_tabs(self):
        # Core's eslint lints this file in `npm test`. Its `indent` rule
        # rejects spaces, so a space-indented config fails core's lint on
        # a clean checkout. Found by the blind acceptance run (§9).
        self.local_settings()
        self.write()
        with open(self.path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        indented = [line for line in lines if line[:1].isspace()]
        self.assertTrue(indented)
        for line in indented:
            self.assertTrue(line.startswith("\t"), repr(line))
            self.assertNotIn(" ", line[:len(line) - len(line.lstrip())])

    def test_the_old_space_indented_form_is_replaced(self):
        # Sandboxes made before the tab fix hold the four-space form. It is
        # our own file, not an engineer's edit, so resume rewrites it.
        self.local_settings()
        params = dict(s.DEFAULT_INSTALL_PARAMS)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(s.api_testing_config_contents(params, self.SECRET, indent=4))
        written, err = self.write(params)
        self.assertTrue(written)
        self.assertEqual(err, "")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(
                f.read(), s.api_testing_config_contents(params, self.SECRET))

    def test_the_values_are_the_installs_own(self):
        self.local_settings()
        self.write({"server": "http://localhost:8080", "scriptPath": "/w",
                    "adminUser": "Root", "adminPassword": "hunter2"})
        config = self.read()
        self.assertEqual(config["base_uri"], "http://localhost:8080/")
        self.assertEqual(config["root_user"],
                         {"name": "Root", "password": "hunter2"})

    def test_a_wiki_without_a_secret_key_gets_no_config(self):
        # Worse than no file: a config with the wrong key fails at login,
        # with nothing in the message to point at the cause.
        self.local_settings("<?php\n// no secret key here\n")
        written, err = self.write()
        self.assertFalse(written)
        self.assertFalse(os.path.exists(self.path))
        self.assertIn("could not read $wgSecretKey", err)

    def test_chowns_the_file_to_the_agent(self):
        self.local_settings()
        self.write()
        self.assertEqual(self.calls, [["sudo", "chown", "agent:agent", self.path]])

    def test_an_edited_config_is_left_alone(self):
        self.local_settings()
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"base_uri": "http://example.test/"}')
        written, err = self.write()
        self.assertFalse(written)
        self.assertIn("leaving it alone", err)
        self.assertEqual(self.read()["base_uri"], "http://example.test/")


class LimaChainTests(unittest.TestCase):
    """The in-VM chain, driven through main(["--lima", PLAN]) -- ordering,
    which repos each step touches, and what's fatal. See
    sbx/DESIGN-setup-steps.md §8. On Lima each repo's path is its clone
    (D8), and the clones, remotes and resets exist already."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def repo(self, *parts, files=()):
        path = os.path.join(self.root, *parts)
        os.makedirs(path, exist_ok=True)
        for name, content in files:
            with open(os.path.join(path, name), "w", encoding="utf-8") as f:
                f.write(content)
        return path

    def write_plan(self, repos, **kw):
        plan = {"version": 1, "primary": repos[0]["path"] if repos else None,
                "resetAll": False, "repos": repos}
        plan.update(kw)
        path = os.path.join(self.root, "plan.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(plan, f)
        return path

    @staticmethod
    def entry(path, canonical, read_only=False, link=None):
        link_dir, link_name = link or (None, None)
        return {"path": path, "canonical": canonical, "readOnly": read_only,
                "linkName": link_name, "linkDir": link_dir}

    def core_and_cite(self, core_files=(), cite_files=()):
        core = self.repo("Wikimedia", "core", files=core_files)
        self.repo("Wikimedia", "core", "extensions")
        cite = self.repo("Wikimedia", "Extensions", "Cite", files=cite_files)
        path = self.write_plan([
            self.entry(cite, "gerrit:mediawiki/extensions/Cite",
                       link=("extensions", "Cite")),
            self.entry(core, s.CORE_CANONICAL),
        ])
        return path, core, cite

    def run_main(self, path, fail=None, on_install=None, log_dir=None):
        """fail(argv, cwd) -> exit code or None, for the steps that should
        fail; everything else succeeds. on_install(cwd) stands in for what
        mw-install:sqlite writes."""
        self.calls = []

        def fake_run(argv, cwd=None):
            self.calls.append((list(argv), cwd))
            stripped = argv[4:] if argv[:4] == ["sudo", "-u", "agent", "-H"] else argv
            if on_install and "mw-install:sqlite" in stripped:
                on_install(cwd)
            code = fail(stripped, cwd) if fail else None
            return FakeCompletedProcess(code or 0)

        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = s.main(["--lima", path], run=fake_run, log_dir=log_dir)
        return code, err.getvalue()

    def commands(self):
        """Each agent command, with a `sudo -u agent -H` prefix stripped (the
        tests run as root or as a user who is not `agent`)."""
        out = []
        for argv, cwd in self.calls:
            if argv[:4] == ["sudo", "-u", "agent", "-H"]:
                argv = argv[4:]
            if argv[:2] != ["sudo", "chown"]:
                out.append((argv, cwd))
        return out

    def dirs(self, *words):
        return sorted(cwd for argv, cwd in self.commands() if argv[:len(words)] == list(words))

    def test_links_extensions_into_core(self):
        path, core, cite = self.core_and_cite()
        code, err = self.run_main(path)
        self.assertEqual(code, 0)
        self.assertEqual(os.readlink(os.path.join(core, "extensions", "Cite")), cite)
        self.assertIn("linked extensions/Cite ->", err)

    def test_nothing_is_reset_or_cloned_here(self):
        # sandbox-repos.sh did the clones, remotes and resets.
        path, _core, _cite = self.core_and_cite(core_files=[("composer.json", "{}")])
        self.assertEqual(self.run_main(path)[0], 0)
        self.assertEqual([a for a, _c in self.commands() if "safe-reset" in a or "clone" in a], [])

    def test_every_writable_clone_is_composer_updated(self):
        path, core, cite = self.core_and_cite(core_files=[("composer.json", "{}")],
                                              cite_files=[("composer.json", "{}")])
        self.assertEqual(self.run_main(path)[0], 0)
        self.assertEqual(self.dirs("composer", "update"), sorted([core, cite]))

    def test_core_is_composer_updated_after_the_links_and_the_merge_file(self):
        path, core, _cite = self.core_and_cite(core_files=[("composer.json", "{}")])
        seen = []

        def fail(argv, cwd):
            if argv[:2] == ["composer", "update"] and cwd == core:
                seen.append((os.path.islink(os.path.join(core, "extensions", "Cite")),
                             os.path.exists(os.path.join(core, s.COMPOSER_LOCAL_NAME))))
        self.assertEqual(self.run_main(path, fail=fail)[0], 0)
        self.assertEqual(seen, [(True, True)])

    def test_a_failed_dependency_step_warns_and_the_sandbox_still_comes_up(self):
        path, _core, cite = self.core_and_cite(cite_files=[("composer.json", "{}")])
        code, err = self.run_main(
            path, fail=lambda a, cwd: 1 if a[:2] == ["composer", "update"] and cwd == cite else None)
        self.assertEqual(code, 0)
        self.assertIn(f"composer update failed in {cite}", err)
        self.assertIn("1 step(s) did not succeed", err)

    def test_a_failed_install_fails_the_sandbox(self):
        path, _core, _cite = self.core_and_cite()
        code, err = self.run_main(path, fail=lambda a, c: 1 if "mw-install:sqlite" in a else None)
        self.assertEqual(code, 1)
        self.assertIn("mw-install:sqlite failed", err)

    def test_a_failed_npm_in_core_fails_the_sandbox(self):
        path, _core, _cite = self.core_and_cite(core_files=[("package.json", "{}")])
        code, err = self.run_main(path, fail=lambda a, c: 1 if "npm" in a else None)
        self.assertEqual(code, 1)
        self.assertIn("npm install failed", err)

    def test_every_clone_with_a_package_json_gets_an_npm_install(self):
        # sbx/DESIGN-testing-instructions.md §5.1: an extension's own JS
        # tests, linters and selenium harness come from its own node_modules.
        path, core, cite = self.core_and_cite(core_files=[("package.json", "{}")],
                                              cite_files=[("package.json", "{}")])
        self.assertEqual(self.run_main(path)[0], 0)
        self.assertEqual(sorted(c for a, c in self.commands() if "npm" in a), sorted([core, cite]))

    def test_a_failed_npm_in_a_dependency_only_warns(self):
        path, _core, cite = self.core_and_cite(cite_files=[("package.json", "{}")])
        code, err = self.run_main(path, fail=lambda a, c: 1 if "npm" in a else None)
        self.assertEqual(code, 0)
        self.assertIn(f"npm install failed in {cite}", err)

    def test_the_test_configs_are_written_after_the_install(self):
        secret = "b" * 64
        path, core, _cite = self.core_and_cite(core_files=[
            ("composer.json", json.dumps({"scripts": {
                "mw-install:sqlite": "install --server=http://localhost:4000 --scriptpath="}})),
            ("LocalSettings.php", f'<?php\n$wgSecretKey = "{secret}";\n'),
        ])
        self.assertEqual(self.run_main(path)[0], 0)
        # The install is skipped (LocalSettings.php is there); the configs
        # are written either way.
        self.assertEqual(self.dirs("composer", "phpunit:config"), [core])
        self.assertEqual([a for a, _c in self.commands() if "mw-install:sqlite" in a], [])
        with open(os.path.join(core, s.API_TESTING_CONFIG_NAME), encoding="utf-8") as f:
            config = json.load(f)
        self.assertEqual(config["secret_key"], secret)
        self.assertEqual(config["base_uri"], "http://localhost:4000/")

    def test_the_env_file_lands_in_core(self):
        path, core, _cite = self.core_and_cite(core_files=[
            ("composer.json", json.dumps({"scripts": {
                "mw-install:sqlite": "install --server=http://localhost:4000 --scriptpath="}})),
        ])
        self.assertEqual(self.run_main(path)[0], 0)
        with open(os.path.join(core, ".env"), encoding="utf-8") as f:
            self.assertIn("MW_SERVER=http://localhost:4000", f.read())

    def test_a_read_only_core_skips_the_whole_chain(self):
        core = self.repo("Wikimedia", "core")
        cite = self.repo("Wikimedia", "Extensions", "Cite")
        path = self.write_plan([
            self.entry(cite, "gerrit:mediawiki/extensions/Cite", link=("extensions", "Cite")),
            self.entry(core, s.CORE_CANONICAL, read_only=True),
        ])
        code, err = self.run_main(path)
        self.assertEqual(code, 0)
        self.assertEqual(self.commands(), [])
        self.assertIn("skipping the MediaWiki setup", err)

    def test_no_core_at_all_skips_the_chain_and_says_why(self):
        cite = self.repo("Wikimedia", "Extensions", "Cite")
        path = self.write_plan([
            self.entry(cite, "gerrit:mediawiki/extensions/Cite", link=("extensions", "Cite"))])
        code, err = self.run_main(path)
        self.assertEqual(code, 0)
        self.assertEqual(self.commands(), [])
        self.assertIn("no mediawiki/core clone", err)

    def fake_install(self, core):
        def write(_cwd):
            with open(os.path.join(core, "LocalSettings.php"), "w", encoding="utf-8") as f:
                f.write("// Enabled skins.\nwfLoadSkin( 'Vector' );\n\n"
                        "wfLoadExtension( 'Parsoid' );\n")
        return write

    def core_and_parsoid(self, name="w", parsoid_ro=False, with_parsoid=True):
        core = self.repo(name, "core")
        repos = [self.entry(core, s.CORE_CANONICAL)]
        parsoid = None
        if with_parsoid:
            parsoid = self.repo(name, "Parsoid")
            repos.append(self.entry(parsoid, s.PARSOID_CANONICAL, read_only=parsoid_ro))
        return self.write_plan(repos), core, parsoid

    def test_a_writable_parsoid_clone_gets_linked_into_localsettings(self):
        path, core, parsoid = self.core_and_parsoid()
        code, err = self.run_main(path, on_install=self.fake_install(core))
        self.assertEqual(code, 0)
        with open(os.path.join(core, "LocalSettings.php"), encoding="utf-8") as f:
            self.assertIn(f"$parsoidInstallDir = '{parsoid}';", f.read())
        self.assertIn(f"linked Parsoid checkout ({parsoid})", err)

    def test_only_a_parsoid_clone_excludes_the_vendor_copy_from_the_classmap(self):
        for with_parsoid in (True, False):
            with self.subTest(with_parsoid=with_parsoid):
                path, core, _p = self.core_and_parsoid(
                    name="with" if with_parsoid else "without", with_parsoid=with_parsoid)
                self.assertEqual(self.run_main(path, on_install=self.fake_install(core))[0], 0)
                with open(os.path.join(core, s.COMPOSER_LOCAL_NAME), encoding="utf-8") as f:
                    self.assertEqual(f.read(), s.composer_local_contents(with_parsoid))

    def test_a_read_only_parsoid_is_not_linked(self):
        path, core, _parsoid = self.core_and_parsoid(parsoid_ro=True)
        code, err = self.run_main(path, on_install=self.fake_install(core))
        self.assertEqual(code, 0)
        with open(os.path.join(core, "LocalSettings.php"), encoding="utf-8") as f:
            contents = f.read()
        self.assertIn("wfLoadExtension( 'Parsoid' );", contents)
        self.assertNotIn("parsoidInstallDir", contents)
        self.assertNotIn("linked Parsoid checkout", err)

    def test_a_bad_plan_stops_before_touching_anything(self):
        path = os.path.join(self.root, "plan.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"version": 2, "repos": []}')
        code, err = self.run_main(path)
        self.assertEqual(code, 1)
        self.assertIn("error:", err)
        self.assertEqual(self.calls, [])

    def test_lima_writes_the_log_and_the_status(self):
        path, _core, cite = self.core_and_cite(cite_files=[("composer.json", "{}")])
        log_dir = os.path.join(self.root, "log")
        code, _err = self.run_main(
            path, log_dir=log_dir,
            fail=lambda a, cwd: 1 if a[:2] == ["composer", "update"] and cwd == cite else None)
        self.assertEqual(code, 0)
        with open(os.path.join(log_dir, s.SETUP_LOG_NAME), encoding="utf-8") as f:
            self.assertIn("linked extensions/Cite", f.read())
        with open(os.path.join(log_dir, s.SETUP_STATUS_NAME), encoding="utf-8") as f:
            status = json.load(f)
        self.assertEqual(status["exit"], 0)
        self.assertTrue(any("composer update failed" in p for p in status["problems"]))

    def test_without_a_mode_main_prints_the_usage(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(s.main([], log_dir=None), 1)
        self.assertIn("--lima PLAN.json", err.getvalue())


class DeepMergeTests(unittest.TestCase):
    def test_dicts_merge_key_by_key(self):
        self.assertEqual(
            s.deep_merge({"a": 1, "n": {"x": 1}}, {"b": 2, "n": {"y": 2}}),
            {"a": 1, "b": 2, "n": {"x": 1, "y": 2}},
        )

    def test_lists_are_unioned_not_replaced(self):
        # permissions.deny is the only list the kit patches, and replacing
        # would silently drop a deny the engineer or a later sbx added.
        self.assertEqual(
            s.deep_merge({"deny": ["Bash(ssh:*)"]}, {"deny": ["mcp__mcp-gateway"]}),
            {"deny": ["Bash(ssh:*)", "mcp__mcp-gateway"]},
        )

    def test_a_repeated_list_entry_is_not_duplicated(self):
        self.assertEqual(s.deep_merge({"d": ["a"]}, {"d": ["a", "b"]}), {"d": ["a", "b"]})

    def test_scalars_are_replaced_and_the_input_is_not_mutated(self):
        base = {"model": "old", "n": {"k": 1}}
        self.assertEqual(s.deep_merge(base, {"model": "new"})["model"], "new")
        self.assertEqual(base, {"model": "old", "n": {"k": 1}})


class MergeSettingsTests(unittest.TestCase):
    PATCH = {"enabledPlugins": {"wmf-claude@wikimedia": True},
             "permissions": {"deny": ["Bash(ssh:*)"]}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, ".claude", "settings.json")
        self.calls = []

    def fake_run(self, argv, cwd=None):
        self.calls.append(argv)
        return FakeCompletedProcess(0)

    def write(self, content):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))

    def merge(self, patch=None):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            ok = s.merge_settings(self.path, self.PATCH if patch is None else patch,
                                  run=self.fake_run)
        return ok, err.getvalue()

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_creates_the_file_when_there_is_none(self):
        ok, _err = self.merge()
        self.assertTrue(ok)
        self.assertEqual(self.read(), self.PATCH)

    def test_keeps_what_sbx_wrote(self):
        # sbx writes its own settings.json; losing defaultMode would leave
        # the agent asking for approval it has no terminal to give.
        self.write({"permissions": {"defaultMode": "bypassPermissions"},
                    "model": "opus"})
        ok, _err = self.merge()
        self.assertTrue(ok)
        merged = self.read()
        self.assertEqual(merged["model"], "opus")
        self.assertEqual(merged["permissions"]["defaultMode"], "bypassPermissions")
        self.assertEqual(merged["permissions"]["deny"], ["Bash(ssh:*)"])
        self.assertTrue(merged["enabledPlugins"]["wmf-claude@wikimedia"])

    def test_it_is_idempotent_and_the_second_run_does_not_rewrite(self):
        # It runs at install and again on every container start.
        self.merge()
        before = os.stat(self.path).st_mtime_ns
        ok, err = self.merge()
        self.assertTrue(ok)
        self.assertIn("already has", err)
        self.assertEqual(os.stat(self.path).st_mtime_ns, before)

    def test_it_chowns_because_the_install_run_is_root(self):
        self.merge()
        self.assertEqual(self.calls, [["sudo", "chown", "agent:agent", self.path]])

    def test_unparseable_settings_are_left_alone_and_reported(self):
        self.write("{not json")
        ok, err = self.merge()
        self.assertFalse(ok)
        self.assertIn("could not read", err)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{not json")

    def test_a_settings_file_that_is_not_an_object_is_left_alone(self):
        self.write("[1, 2]")
        ok, err = self.merge()
        self.assertFalse(ok)
        self.assertIn("not a JSON object", err)


class RunSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = os.path.join(self.tmp.name, "settings.json")

    def patch_file(self, content):
        path = os.path.join(self.tmp.name, "patch.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))
        return path

    def run_settings(self, argv):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = s.run_settings(argv, run=lambda a, cwd=None: FakeCompletedProcess(0))
        return code, err.getvalue()

    def test_merges_the_patch_into_the_named_target(self):
        patch = self.patch_file({"enabledPlugins": {"wmf-claude@wikimedia": True}})
        code, _err = self.run_settings([patch, self.target])
        self.assertEqual(code, 0)
        with open(self.target, encoding="utf-8") as f:
            self.assertTrue(json.load(f)["enabledPlugins"]["wmf-claude@wikimedia"])

    def test_the_target_defaults_to_the_agents_settings_json(self):
        self.assertEqual(s.SANDBOX_SETTINGS_FILE,
                         os.path.join(s.SANDBOX_HOME, ".claude", "settings.json"))

    def test_no_patch_path_is_an_error(self):
        code, err = self.run_settings([])
        self.assertEqual(code, 1)
        self.assertIn("JSON patch file", err)

    def test_an_unreadable_or_malformed_patch_fails_loudly(self):
        code, err = self.run_settings(["/nonexistent/patch.json", self.target])
        self.assertEqual(code, 1)
        self.assertIn("could not read", err)
        code, err = self.run_settings([self.patch_file("[]"), self.target])
        self.assertEqual(code, 1)
        self.assertIn("not a JSON object", err)

    def test_main_dispatches_settings(self):
        patch = self.patch_file({"permissions": {"deny": ["Bash(ssh:*)"]}})
        with contextlib.redirect_stderr(io.StringIO()):
            code = s.main(["--settings", patch, self.target],
                          run=lambda a, cwd=None: FakeCompletedProcess(0))
        self.assertEqual(code, 0)
        with open(self.target, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["permissions"]["deny"], ["Bash(ssh:*)"])


class RegisterMcpServersTests(unittest.TestCase):
    """`wmf-sbx-setup --mcp` -- one `claude mcp add` per host-side server,
    pointed at the in-sandbox proxy. sbx/DESIGN-plugin-integration.md §5
    step 4."""

    WANTED = {
        "gerrit": {"command": "/home/agent/.local/bin/wmf-sbx-mcp-proxy",
                   "args": ["gerrit", "--tools", "get_file_diff", "--no-add"]},
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "claude.json")
        self.calls = []

    def claude_json(self, servers):
        with open(self.path, "w", encoding="utf-8") as f:
            # Alongside sbx's own gateway entry and the rest of the file,
            # which is the reason this reconciles rather than overwrites.
            json.dump({"numStartups": 3, "mcpServers": dict(
                servers, **{"mcp-gateway": {"type": "http"}})}, f)

    def fake_run(self, cmd, cwd=None):
        self.calls.append(cmd)
        return FakeCompletedProcess(0)

    def argv(self, index=0):
        """One recorded call with any `sudo -u agent -H` prefix stripped and
        `claude` normalised back to its bare name.

        Whether the sudo prefix is there depends on the euid the test
        itself happens to be running under; whether argv[0] is absolute
        depends on where `claude` is installed where the test is running
        (see claude_executable, and §69 for why it is absolute at all)."""
        cmd = list(self.calls[index])
        start = next(i for i, a in enumerate(cmd)
                     if a == "claude" or a.endswith("/claude"))
        return ["claude"] + cmd[start + 1:]

    def verbs(self):
        return [self.argv(i)[2] for i in range(len(self.calls))]

    def register(self, config=None):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            ok = s.register_mcp_servers(config or self.WANTED, path=self.path,
                                        run=self.fake_run)
        return ok, err.getvalue()

    def test_registers_a_server_that_is_not_there(self):
        self.claude_json({})
        ok, err = self.register()
        self.assertTrue(ok)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.argv(), [
            "claude", "mcp", "add", "--scope", "user", "gerrit", "--",
            "/home/agent/.local/bin/wmf-sbx-mcp-proxy",
            "gerrit", "--tools", "get_file_diff", "--no-add",
        ])
        self.assertIn("registered the gerrit MCP server", err)

    def test_an_identical_registration_is_left_alone(self):
        # This runs at install and again on every container start; a
        # restart must not churn ~/.claude.json.
        self.claude_json({"gerrit": dict(self.WANTED["gerrit"], type="stdio",
                                         env={})})
        ok, err = self.register()
        self.assertTrue(ok)
        self.assertEqual(self.calls, [])
        self.assertIn("already registered", err)

    def test_a_changed_allowlist_is_removed_and_re_added(self):
        # `claude mcp add` refuses a name that is taken (MEASURED: exit 1,
        # "already exists in user config") and there is no `mcp set`. A
        # shortened --tools list is a policy change that must land.
        self.claude_json({"gerrit": {"type": "stdio",
                                     "command": self.WANTED["gerrit"]["command"],
                                     "args": ["gerrit", "--tools", "everything"]}})
        ok, _err = self.register()
        self.assertTrue(ok)
        self.assertEqual(self.verbs(), ["remove", "add"])

    def test_an_unreadable_claude_json_still_registers(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        ok, _err = self.register()
        self.assertTrue(ok)
        self.assertEqual(self.verbs(), ["add"])

    def test_a_failed_add_is_reported_and_fails_the_step(self):
        self.claude_json({})

        def failing(cmd, cwd=None):
            self.calls.append(cmd)
            return FakeCompletedProcess(1)

        with contextlib.redirect_stderr(io.StringIO()) as err:
            ok = s.register_mcp_servers(self.WANTED, path=self.path, run=failing)
        self.assertFalse(ok)
        self.assertIn("could not register the gerrit MCP server", err.getvalue())

    def test_as_root_it_goes_through_sudo_u_agent(self):
        # Otherwise the install-time run writes the registration into
        # /root/.claude.json, where the agent never sees it.
        with contextlib.redirect_stderr(io.StringIO()):
            s.claude_mcp(["ls"], run=self.fake_run, root=True)
        self.assertEqual(self.calls[0][:4], ["sudo", "-u", s.SANDBOX_USER, "-H"])
        self.assertEqual(self.calls[0][4:], [s.claude_executable(), "mcp", "ls"])
        with contextlib.redirect_stderr(io.StringIO()):
            s.claude_mcp(["ls"], run=self.fake_run, root=False)
        self.assertEqual(self.calls[1], [s.claude_executable(), "mcp", "ls"])

    def test_the_sudo_run_names_claude_by_absolute_path(self):
        # §69: sudo replaces PATH with its own secure_path, which does not
        # contain ~/.local/bin, so `sudo -u agent -H claude` is
        # `sudo: claude: command not found` -- exit 1, at the last install
        # step of a five-minute create.
        with mock.patch.object(s.os, "access", return_value=True):
            self.assertEqual(s.claude_executable(), s.SANDBOX_CLAUDE_BIN)
        self.assertTrue(os.path.isabs(s.SANDBOX_CLAUDE_BIN))

    def test_claude_executable_falls_back_to_the_path(self):
        # Anywhere that isn't a sandbox built from this image -- a host
        # checkout, the tests -- there is no /home/agent to find.
        with mock.patch.object(s.os, "access", return_value=False), \
                mock.patch.object(s.shutil, "which", return_value="/opt/claude"):
            self.assertEqual(s.claude_executable(), "/opt/claude")
        with mock.patch.object(s.os, "access", return_value=False), \
                mock.patch.object(s.shutil, "which", return_value=None):
            self.assertEqual(s.claude_executable(), "claude")

    def test_the_default_target_is_the_agents_claude_json(self):
        self.assertEqual(s.SANDBOX_CLAUDE_JSON,
                         os.path.join(s.SANDBOX_HOME, ".claude.json"))

    def test_run_mcp_reads_the_kits_file_and_main_dispatches_it(self):
        self.claude_json({})
        config_path = os.path.join(self.tmp.name, "wmf-sbx-mcp.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(self.WANTED, f)
        with contextlib.redirect_stderr(io.StringIO()):
            code = s.main(["--mcp", config_path, self.path], run=self.fake_run)
        self.assertEqual(code, 0)
        self.assertEqual(self.verbs(), ["add"])

    def test_run_mcp_complains_about_a_missing_or_malformed_file(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(s.run_mcp([], run=self.fake_run), 1)
            self.assertEqual(
                s.run_mcp(["/nonexistent/mcp.json"], run=self.fake_run), 1)
        self.assertIn("JSON registration file", err.getvalue())
        self.assertIn("could not read", err.getvalue())


SAMPLE_CLAUDE_MD = """\
## Keep

Good advice.

```bash
# Not a heading
echo hi
```

## Wrong

Direct mode says something false.

### Wrong subsection

More of it.

## Keep too

### Aspire

Remove me.

## Last
"""


if __name__ == "__main__":
    unittest.main()
