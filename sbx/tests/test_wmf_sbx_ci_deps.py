#!/usr/bin/env python3
"""Unit tests for wmf_sbx/ci_deps.py -- run with:
  python3 -m unittest discover -s sbx/tests -v
"""

import io
import os
import sys
import tempfile
import unittest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, SRC)

import wmf_sbx.ci_deps as ci  # noqa: E402
import wmf_sbx.deps as deps  # noqa: E402
import wmf_sbx.image as image  # noqa: E402

EXT = "gerrit:mediawiki/extensions/"


class CanonicalTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(ci.canonical("Cite"), EXT + "Cite")
        self.assertEqual(ci.canonical("skins/Vector"), "gerrit:mediawiki/skins/Vector")
        self.assertEqual(ci.canonical("parsoid"), "gerrit:mediawiki/services/parsoid")


class ClosureTests(unittest.TestCase):
    # zuul/parameter_functions.py get_dependencies, case by case.
    TESTS = {
        "A": {"recurse": True, "dependencies": ["B"]},
        "B": {"recurse": False, "dependencies": ["C"]},
        "C": {"recurse": True, "dependencies": ["D"]},
        "E": {"recurse": True, "dependencies": ["F"]},
        "F": ["G"],            # legacy: inherits E's recurse
        "G": ["H"],
        "X": {"recurse": True, "dependencies": ["Y"]},
        "Y": {"recurse": True, "dependencies": ["X"]},
    }

    def test_an_entrys_recurse_false_stops_below_it(self):
        self.assertEqual(ci.ci_closure("A", self.TESTS), {"B", "C"})

    def test_a_legacy_entry_inherits_its_parents_recurse(self):
        self.assertEqual(ci.ci_closure("E", self.TESTS), {"F", "G", "H"})

    def test_a_cycle_terminates(self):
        self.assertEqual(ci.ci_closure("X", self.TESTS), {"X", "Y"})


class ExtraDependenciesTests(unittest.TestCase):
    def test_tests_then_phan_only_and_never_itself(self):
        edges = ci.extra_dependencies(
            {"parsoid": {"recurse": False, "dependencies": ["ParserFunctions"]},
             "X": {"recurse": True, "dependencies": ["X"]}},
            {"parsoid": ["ParserFunctions", "Scribunto"], "skins/Vector": ["Echo"]})
        self.assertEqual(edges, {
            "gerrit:mediawiki/services/parsoid": [(EXT + "ParserFunctions", False),
                                                  (EXT + "Scribunto", True)],
            "gerrit:mediawiki/skins/Vector": [(EXT + "Echo", True)],
        })

    def test_the_output_is_a_settings_block_the_walk_accepts(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "dependencies.yaml"), "w") as f:
                f.write("parsoid:\n  recurse: false\n  dependencies:\n    - ParserFunctions\n")
            with open(os.path.join(d, "phan_dependencies.yaml"), "w") as f:
                f.write("parsoid:\n  - Scribunto\n")
            out = io.StringIO()
            self.assertEqual(ci.main(["--dir", d], out=out), 0)
        text = out.getvalue()
        self.assertIn(f"    - {EXT}Scribunto  # phan\n", text)
        edges = deps.extra_edges(image.yaml.safe_load(text)["extra_dependencies"])
        self.assertEqual(edges, {"gerrit:mediawiki/services/parsoid":
                                 [EXT + "ParserFunctions", EXT + "Scribunto"]})


class TrimTests(unittest.TestCase):
    # requires: R -> A; B -> C; D <-> E. Nothing else has a manifest.
    REQUIRES = {"R": ["A"], "A": [], "B": ["C"], "C": [], "D": ["E"], "E": ["D"]}

    def manifest_for(self, canonical):
        name = canonical[len(EXT):] if canonical.startswith(EXT) else None
        if name not in self.REQUIRES:
            return None
        return deps.Manifest("extension.json", {
            "requires": {"extensions": {n: "*" for n in self.REQUIRES[name]}}})

    def trim(self, names):
        edges = {EXT + "R": [(EXT + n, n == "C") for n in names]}
        return [(d[len(EXT):], phan) for d, phan in
                ci.trim(edges, self.manifest_for).get(EXT + "R", [])]

    def test_what_the_repos_own_walk_finds_goes(self):
        self.assertEqual(self.trim(["A", "B"]), [("B", False)])

    def test_what_another_extras_walk_finds_goes(self):
        self.assertEqual(self.trim(["C", "B"]), [("B", False)])

    def test_of_two_that_reach_each_other_the_first_stays(self):
        self.assertEqual(self.trim(["E", "D"]), [("E", False)])

    def test_a_repo_left_with_nothing_is_dropped(self):
        self.assertEqual(ci.trim({EXT + "R": [(EXT + "A", False)]}, self.manifest_for), {})


if __name__ == "__main__":
    unittest.main()
