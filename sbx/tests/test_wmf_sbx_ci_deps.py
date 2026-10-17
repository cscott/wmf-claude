#!/usr/bin/env python3
"""Unit tests for wmf_sbx/ci_deps.py -- run with:
  python3 -m unittest discover -s sbx/tests -v
"""

import base64
import contextlib
import io
import os
import sys
import unittest
import urllib.error

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, SRC)

import wmf_sbx.ci_deps as ci  # noqa: E402
import isolation  # noqa: E402


def setUpModule():
    isolation.isolate()


EXT = "gerrit:mediawiki/extensions/"
PARSOID = "gerrit:mediawiki/services/parsoid"


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


class EdgesTests(unittest.TestCase):
    TESTS = {"parsoid": {"recurse": False, "dependencies": ["ParserFunctions"]},
             "Cite": {"recurse": True, "dependencies": ["Popups", "Cite"]},
             "Popups": ["TextExtracts"]}
    PHAN = {"parsoid": ["ParserFunctions", "Scribunto"], "Cite": ["VisualEditor"],
            "skins/Vector": ["Echo"]}

    def test_tests_then_phan_only_and_never_itself(self):
        self.assertEqual(ci.edges([PARSOID, EXT + "Cite"], self.TESTS, self.PHAN), {
            PARSOID: [EXT + "ParserFunctions", EXT + "Scribunto"],
            EXT + "Cite": [EXT + "Popups", EXT + "TextExtracts", EXT + "VisualEditor"],
        })

    def test_without_phan(self):
        self.assertEqual(ci.edges([PARSOID], self.TESTS, self.PHAN, include_phan=False),
                         {PARSOID: [EXT + "ParserFunctions"]})

    def test_only_the_roots_and_only_what_the_maps_can_name(self):
        self.assertEqual(ci.edges(["gerrit:mediawiki/skins/Vector", "gerrit:mediawiki/core",
                                   None, "gitlab:x/y"], self.TESTS, self.PHAN),
                         {"gerrit:mediawiki/skins/Vector": [EXT + "Echo"]})

    def test_map_name_inverts_canonical(self):
        for name in ("Cite", "skins/Vector", "parsoid"):
            self.assertEqual(ci.map_name(ci.canonical(name)), name)
        self.assertIsNone(ci.map_name("gerrit:mediawiki/core"))


class FetchMapsTests(unittest.TestCase):
    """Two requests to gitiles, as deps.fetch_manifest makes them."""

    def opener(self, bodies, urls):
        def opener(url, timeout=None):
            urls.append(url)
            body = bodies.pop(0)
            if isinstance(body, Exception):
                raise body
            return contextlib.closing(io.BytesIO(base64.b64encode(body.encode())))
        return opener

    def test_both_maps(self):
        urls = []
        maps = ci.fetch_maps(self.opener(["Cite: [Popups]\n", "Cite:\n  - VisualEditor\n"], urls))
        self.assertEqual(maps, ({"Cite": ["Popups"]}, {"Cite": ["VisualEditor"]}))
        self.assertEqual([u.split("/+/refs/heads/master/")[1] for u in urls],
                         ["zuul/dependencies.yaml?format=TEXT",
                          "zuul/phan_dependencies.yaml?format=TEXT"])
        self.assertTrue(all(u.startswith("https://gerrit.wikimedia.org/") for u in urls))

    def test_unreachable_is_none_with_a_warning(self):
        warnings = []
        err = urllib.error.URLError("no route")
        maps = ci.fetch_maps(self.opener([err, err], []), warn=warnings.append,
                             sleep=lambda s: None, attempts=2)
        self.assertIsNone(maps)
        self.assertTrue(warnings)

    def test_not_a_map_is_none(self):
        self.assertIsNone(ci.fetch_maps(self.opener(["- a list\n", "{}"], []),
                                        warn=lambda m: None))

if __name__ == "__main__":
    unittest.main()
