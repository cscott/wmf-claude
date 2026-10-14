#!/usr/bin/env python3
"""Unit tests for the settings.yaml defaults of command-line options
(cli.py) -- run with:
  python3 -m unittest discover -s sbx/tests -v"""

import argparse
import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.cli as cli  # noqa: E402
import wmf_sbx.resolve as resolve  # noqa: E402


def demo_parser():
    p = argparse.ArgumentParser(prog="wmf-sbx demo")
    p.add_argument("name")
    p.add_argument("--cold", action=argparse.BooleanOptionalAction, default=False,
                   help="start cold")
    p.add_argument("--egress-proxy", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--vm-type", choices=["qemu", "vz"])
    p.add_argument("--scan", action="append", default=[])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--rm", action=argparse.BooleanOptionalAction, default=False)
    g.add_argument("--list", action=argparse.BooleanOptionalAction, default=False)
    return p


def image_parser():
    p = argparse.ArgumentParser(prog="wmf-sbx image")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--keep-builder", action=argparse.BooleanOptionalAction, default=False)
    sub.add_parser("ls")
    return p


class CliTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = os.path.join(self.tmp.name, "settings.yaml")
        self.warned = []

    def settings(self, text):
        with open(self.config, "w", encoding="utf-8") as f:
            f.write(text)

    def parse(self, argv, section="demo", parser=None):
        parser = parser or demo_parser()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            args, config = cli.parse_args(parser, argv, section, config_default=self.config,
                                          warn=self.warned.append)
        self.err = err.getvalue()
        return args

    def fails(self, argv, section="demo", parser=None, code=1):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as cm:
            cli.parse_args(parser or demo_parser(), argv, section,
                           config_default=self.config, warn=self.warned.append)
        self.assertEqual(cm.exception.code, code)
        return err.getvalue()

    def test_camel(self):
        self.assertEqual(cli.camel("clone_missing"), "cloneMissing")
        self.assertEqual(cli.camel("cold"), "cold")
        self.assertEqual(cli.camel("ci_deps"), "ciDeps")

    def test_no_file_gives_the_code_defaults(self):
        args = self.parse(["x"])
        self.assertEqual((args.cold, args.egress_proxy, args.cpus, args.vm_type, args.scan),
                         (False, None, 4, None, []))
        self.assertEqual(args.config, self.config)
        self.assertIsNone(args.settings)

    def test_settings_give_the_defaults_and_the_command_line_wins(self):
        self.settings("demo:\n  cold: true\n  cpus: 8\n  vmType: vz\n  egressProxy: false\n")
        args = self.parse(["x"])
        self.assertEqual((args.cold, args.cpus, args.vm_type, args.egress_proxy),
                         (True, 8, "vz", False))
        args = self.parse(["x", "--no-cold", "--cpus", "2", "--vm-type", "qemu",
                           "--egress-proxy"])
        self.assertEqual((args.cold, args.cpus, args.vm_type, args.egress_proxy),
                         (False, 2, "qemu", True))

    def test_null_is_no_key(self):
        self.settings("demo:\n  egressProxy: null\n  cpus: null\n")
        args = self.parse(["x"])
        self.assertEqual((args.egress_proxy, args.cpus), (None, 4))

    def test_a_list_from_the_command_line_replaces_the_settings_one(self):
        self.settings("demo:\n  scan: [~/a, ~/b]\n")
        self.assertEqual(self.parse(["x"]).scan, ["~/a", "~/b"])
        self.assertEqual(self.parse(["x", "--scan", "c"]).scan, ["c"])

    def test_a_wrong_type_is_an_error(self):
        for text, why in (("cold: yes-please", "want true or false"),
                          ("cold: 1", "want true or false"),
                          ("cpus: four", "want an integer"),
                          ("cpus: true", "want an integer"),
                          ("vmType: kvm", "want one of qemu, vz"),
                          ("vmType: 5", "want a string"),
                          ("scan: ~/a", "want a list")):
            with self.subTest(text=text):
                self.settings(f"demo:\n  {text}\n")
                err = self.fails(["x"])
                self.assertIn("error: settings.yaml demo.", err)
                self.assertIn(why, err)

    def test_a_section_that_is_not_a_mapping_is_an_error(self):
        self.settings("demo: [cold]\n")
        self.assertIn("settings.yaml demo: want a mapping", self.fails(["x"]))

    def test_an_unknown_key_warns_and_is_ignored(self):
        self.settings("demo:\n  colt: true\n  name: y\n  config: /etc\n")
        args = self.parse(["x"])
        self.assertFalse(args.cold)
        self.assertEqual(len(self.warned), 3, self.warned)
        self.assertIn("settings.yaml demo.colt: `wmf-sbx demo` has no such option; ignored",
                      self.warned[0])
        self.assertIn("(it has: cold, cpus, egressProxy, list, rm, scan, vmType)",
                      self.warned[0])

    def test_no_settings_reads_no_defaults(self):
        self.settings("demo:\n  cold: true\n  cpus: four\n  colt: 1\n")
        args = self.parse(["x", "--no-settings"])
        self.assertEqual((args.cold, args.cpus), (False, 4))
        self.assertFalse(args.settings)
        self.assertEqual(self.warned, [])

    def test_session_settings_false_turns_them_off_and_settings_back_on(self):
        self.settings("session:\n  settings: false\ndemo:\n  cold: true\n")
        self.assertFalse(self.parse(["x"]).cold)
        self.assertTrue(self.parse(["x", "--settings"]).cold)
        self.settings("session:\n  settings: true\ndemo:\n  cold: true\n")
        self.assertFalse(self.parse(["x", "--no-settings"]).cold)
        self.settings("session:\n  settings: nope\n")
        self.assertIn("session.settings: 'nope': want true or false", self.fails(["x"]))

    def test_config_names_the_file(self):
        other = os.path.join(self.tmp.name, "other.yaml")
        with open(other, "w", encoding="utf-8") as f:
            f.write("demo:\n  cpus: 3\n")
        args = self.parse(["--config", other, "x"])
        self.assertEqual((args.cpus, args.config), (3, other))

    def test_a_bad_file_is_an_error(self):
        self.settings("- a list\n")
        self.assertIn("want a mapping at the top level", self.fails(["x"]))

    def test_mutually_exclusive_on_the_command_line_drops_the_other(self):
        self.settings("demo:\n  rm: true\n")
        self.assertTrue(self.parse(["x"]).rm)
        args = self.parse(["x", "--list"])
        self.assertEqual((args.rm, args.list), (False, True))

    def test_mutually_exclusive_both_in_settings_is_an_error(self):
        self.settings("demo:\n  rm: true\n  list: true\n")
        self.assertIn("demo.rm and demo.list cannot both be set", self.fails(["x"]))
        self.settings("demo:\n  rm: true\n  list: false\n")
        self.assertTrue(self.parse(["x"]).rm)

    def test_a_subcommand_nests(self):
        self.settings("image:\n  build:\n    keepBuilder: true\n  bild: {}\n")
        args = self.parse(["build"], section="image", parser=image_parser())
        self.assertTrue(args.keep_builder)
        self.assertIn("image.bild: `wmf-sbx image` has no such option", self.warned[0])
        self.assertIn("(it has: build, ls)", self.warned[0])
        args = self.parse(["build", "--no-keep-builder"], section="image",
                          parser=image_parser())
        self.assertFalse(args.keep_builder)
        # --config and --settings come after the subcommand.
        args = self.parse(["build", "--no-settings"], section="image", parser=image_parser())
        self.assertFalse(args.keep_builder)
        self.assertEqual(self.parse(["ls"], section="image", parser=image_parser()).config,
                         self.config)

    def test_help_names_the_key_and_the_value(self):
        self.settings("demo:\n  cold: true\n  scan: ['50%']\n")
        out = io.StringIO()
        # -h works with the positional missing, and after --config.
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            self.parse(["-h"])
        self.assertEqual(cm.exception.code, 0)
        text = " ".join(out.getvalue().split())
        self.assertIn("start cold [settings: demo.cold: true]", text)
        self.assertIn("[settings: demo.cpus]", text)
        self.assertIn('[settings: demo.scan: ["50%"]]', text)
        self.assertNotIn("demo.config", text)

    def test_a_command_line_error_is_argparse_s(self):
        self.assertIn("unrecognized arguments: --colt", self.fails(["x", "--colt"], code=2))
        self.assertIn("the following arguments are required: name", self.fails([], code=2))

    def test_the_parser_is_left_as_it_was(self):
        parser = demo_parser()
        cli._add_own_options(parser, self.config)
        before = [(a.default, a.required) for a in parser._actions]
        self.assertEqual(cli._given(parser, ["x", "--cold"]), {"name": "x", "cold": True})
        self.assertEqual([(a.default, a.required) for a in parser._actions], before)
        self.assertNotIn("error", vars(parser))
        with self.assertRaises(cli._Pass1Error):
            cli._given(parser, ["--cpus", "four"])

    def test_a_constant_flag_is_refused(self):
        p = argparse.ArgumentParser()
        p.add_argument("--cold", action="store_true")
        with self.assertRaisesRegex(AssertionError, "--cold: a constant flag"):
            cli.settable(p)


class UnknownSectionTests(unittest.TestCase):

    def test_an_unknown_section_warns_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("images: {}\ncreate: {}\nrm: {}\nsession: {}\nrepos: []\n")
            said = []
            resolve.load_config(path, warn=said.append)
            resolve.load_config(path, warn=said.append)
            self.assertEqual(len(said), 1, said)
            self.assertIn(f"{path}: unknown section `images:`; ignored", said[0])


if __name__ == "__main__":
    unittest.main()
