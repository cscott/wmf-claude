#!/usr/bin/env python3
"""Unit tests for wmf_sbx_deps.py -- run with:
  python3 -m unittest discover -s sbx/tests -v

Nothing here touches the network: the gitiles fetcher is injectable
precisely so the walk can be tested without it.
"""

import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.deps as d  # noqa: E402

CORE = d.CORE
VECTOR = d.DEFAULT_SKIN
EXT = d.EXTENSIONS_PREFIX


def manifest(filename="extension.json", **data):
    return d.Manifest(filename, data)


class ParseManifestTests(unittest.TestCase):
    def test_parses(self):
        m = d.parse_manifest('{"name": "Cite"}', "extension.json", "src")
        self.assertEqual(m, d.Manifest("extension.json", {"name": "Cite"}))

    def test_malformed_json_warns_and_returns_none(self):
        # One of cananian's 298 local manifests really is malformed
        # (AllowExternalImages) -- one broken extension in a closure must
        # not take sandbox creation down with it.
        warnings = []
        self.assertIsNone(
            d.parse_manifest("{oops", "extension.json", "src", warn=warnings.append)
        )
        self.assertIn("not valid JSON", warnings[0])

    def test_non_object_warns_and_returns_none(self):
        warnings = []
        self.assertIsNone(d.parse_manifest("[1, 2]", "extension.json", "s", warn=warnings.append))
        self.assertIn("not a JSON object", warnings[0])


class ReadManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, filename, data):
        with open(os.path.join(self.tmp.name, filename), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_reads_extension_json(self):
        self.write("extension.json", {"name": "Cite"})
        self.assertEqual(d.read_manifest(self.tmp.name).data["name"], "Cite")

    def test_reads_skin_json(self):
        self.write("skin.json", {"name": "Vector"})
        m = d.read_manifest(self.tmp.name)
        self.assertEqual(m.filename, "skin.json")

    def test_no_manifest_is_none(self):
        self.assertIsNone(d.read_manifest(self.tmp.name))

    def test_missing_directory_is_none(self):
        self.assertIsNone(d.read_manifest("/nonexistent/dir"))


def http_error(url, code, reason):
    """An HTTPError that won't warn about an unclosed response at gc time."""
    err = urllib.error.HTTPError(url, code, reason, {}, None)
    err.close()
    return err


class FetchManifestTests(unittest.TestCase):
    def opener_for(self, responses):
        """responses: {url_substring: body-or-HTTPError}."""
        calls = []

        def opener(url, timeout=None):
            calls.append(url)
            for key, value in responses.items():
                if key in url:
                    if isinstance(value, Exception):
                        raise value
                    return contextlib.closing(io.BytesIO(base64.b64encode(value.encode())))
            raise http_error(url, 401, "Unauthorized")

        return opener, calls

    def test_fetches_and_base64_decodes(self):
        opener, _calls = self.opener_for({"extension.json": '{"name": "Cite"}'})
        m = d.fetch_manifest(EXT + "Cite", opener=opener)
        self.assertEqual(m.data["name"], "Cite")

    def test_skins_are_tried_as_skin_json_first(self):
        opener, calls = self.opener_for({"skin.json": '{"name": "Vector"}'})
        m = d.fetch_manifest(VECTOR, opener=opener)
        self.assertEqual(m.filename, "skin.json")
        self.assertEqual(len(calls), 1)  # no wasted extension.json round trip

    def test_401_means_unknown_project(self):
        # gitiles hides existence behind auth: a nonexistent project
        # answers 401, not 404, so absent and forbidden are the same
        # thing from out here. Either way the repo is a leaf.
        opener, _calls = self.opener_for({})
        self.assertIsNone(d.fetch_manifest(EXT + "Nope", opener=opener))

    def test_server_error_is_unreachable_not_a_leaf(self):
        # "We couldn't ask" must not read as "it has no dependencies" --
        # that silently drops repos out of the closure.
        warnings = []

        def opener(url, timeout=None):
            raise http_error(url, 500, "Server Error")

        self.assertIs(
            d.fetch_manifest(EXT + "X", opener=opener, warn=warnings.append,
                             sleep=lambda s: None),
            d.UNREACHABLE,
        )
        self.assertIn("HTTP 500", warnings[0])

    def test_network_failure_is_unreachable(self):
        warnings = []

        def opener(url, timeout=None):
            raise urllib.error.URLError("no route to host")

        self.assertIs(
            d.fetch_manifest(EXT + "X", opener=opener, warn=warnings.append,
                             sleep=lambda s: None),
            d.UNREACHABLE,
        )
        self.assertTrue(warnings)

    def test_429_is_retried_honouring_retry_after(self):
        # The Wikimedia edge (Varnish, not gitiles) answers a burst of
        # manifest fetches with 429 + `retry-after: 60`. Observed live
        # 2026-09-07 while measuring closure sizes.
        slept = []
        attempts = []

        def opener(url, timeout=None):
            attempts.append(url)
            if len(attempts) == 1:
                err = urllib.error.HTTPError(url, 429, "Too Many Requests",
                                             {"retry-after": "60"}, None)
                err.close()
                raise err
            return contextlib.closing(io.BytesIO(base64.b64encode(b'{"name": "X"}')))

        m = d.fetch_manifest(EXT + "X", opener=opener, sleep=slept.append)
        self.assertEqual(m.data["name"], "X")
        self.assertEqual(slept, [60])

    def test_retry_after_is_capped(self):
        slept = []

        def opener(url, timeout=None):
            err = urllib.error.HTTPError(url, 429, "Too Many Requests",
                                         {"retry-after": "86400"}, None)
            err.close()
            raise err

        self.assertIs(
            d.fetch_manifest(EXT + "X", opener=opener, sleep=slept.append),
            d.UNREACHABLE,
        )
        # Bounded: a day-long Retry-After must not hang sandbox creation.
        self.assertEqual(slept, [d.RETRY_AFTER_MAX, d.RETRY_AFTER_MAX])

    def test_missing_file_is_not_retried(self):
        calls = []

        def opener(url, timeout=None):
            calls.append(url)
            raise http_error(url, 401, "Unauthorized")

        self.assertIsNone(
            d.fetch_manifest(EXT + "X", opener=opener, sleep=lambda s: None)
        )
        self.assertEqual(len(calls), 2)  # extension.json, skin.json; no retries

    def test_non_gerrit_canonical_is_not_fetched(self):
        def opener(url, timeout=None):
            raise AssertionError("should not fetch")

        self.assertIsNone(d.fetch_manifest("gitlab:daniel/claudebox", opener=opener))


class DependencyKeysTests(unittest.TestCase):
    def test_reads_requires_extensions_and_skins(self):
        m = manifest(requires={"extensions": {"Echo": "*"}, "skins": {"Vector": "*"}})
        self.assertEqual(
            d.dependency_keys(m), [("extensions", "Echo"), ("skins", "Vector")]
        )

    def test_ignores_mediawiki_and_platform(self):
        # Version and PHP-ability constraints, with nothing to clone.
        m = manifest(requires={"MediaWiki": ">= 1.43", "platform": {"php": ">=8.1"}})
        self.assertEqual(d.dependency_keys(m), [])

    def test_dev_and_suggests_included_by_default(self):
        m = manifest(
            requires={"extensions": {"A": "*"}},
            **{"dev-requires": {"extensions": {"B": "*"}}},
        )
        m.data["suggests"] = {"extensions": {"C": "*"}}
        self.assertEqual(
            [k for _s, k in d.dependency_keys(m)], ["A", "B", "C"]
        )

    def test_no_dev_and_no_suggests(self):
        m = manifest(requires={"extensions": {"A": "*"}})
        m.data["dev-requires"] = {"extensions": {"B": "*"}}
        m.data["suggests"] = {"extensions": {"C": "*"}}
        self.assertEqual(
            [k for _s, k in d.dependency_keys(m, include_dev=False, include_suggests=False)],
            ["A"],
        )

    def test_deduplicates_across_fields(self):
        m = manifest(requires={"extensions": {"A": "*"}})
        m.data["suggests"] = {"extensions": {"A": "*"}}
        self.assertEqual(d.dependency_keys(m), [("extensions", "A")])

    def test_malformed_sections_are_ignored(self):
        m = manifest(requires={"extensions": ["A"]}, suggests="nope")
        self.assertEqual(d.dependency_keys(m), [])


class KeyToCanonicalTests(unittest.TestCase):
    def test_extension_and_skin_keys(self):
        self.assertEqual(d.key_to_canonical("extensions", "Echo"), EXT + "Echo")
        self.assertEqual(d.key_to_canonical("skins", "Vector"), VECTOR)

    def test_key_with_a_space_is_refused(self):
        # `requires: {"Abuse Filter": "*"}` is legal MediaWiki -- the key
        # is a credits name, not a directory -- and would otherwise
        # produce a nonsense project path.
        warnings = []
        self.assertIsNone(
            d.key_to_canonical("extensions", "Abuse Filter", warn=warnings.append)
        )
        self.assertIn("credits name", warnings[0])

    def test_key_with_a_slash_is_refused(self):
        self.assertIsNone(d.key_to_canonical("extensions", "a/b"))

    def test_empty_key_is_refused(self):
        self.assertIsNone(d.key_to_canonical("extensions", "  "))

    def test_override_wins(self):
        self.assertEqual(
            d.key_to_canonical("extensions", "Abuse Filter",
                               overrides={"Abuse Filter": EXT + "AbuseFilter"}),
            EXT + "AbuseFilter",
        )


class LinkNameTests(unittest.TestCase):
    def test_basename_wins_inside_extensions_and_skins(self):
        # 44 of 298 local manifests have `name` != directory; every
        # symlink in cananian's core is named after the directory.
        m = manifest(name="Abuse Filter")
        self.assertEqual(d.link_name(EXT + "AbuseFilter", m), "AbuseFilter")
        self.assertEqual(d.link_name(EXT + "cldr", manifest(name="CLDR")), "cldr")
        self.assertEqual(d.link_name(VECTOR, manifest(name="Vector")), "Vector")

    def test_name_field_used_outside_those_trees(self):
        # This is what puts mediawiki/services/parsoid at
        # extensions/Parsoid rather than extensions/parsoid.
        warnings = []
        self.assertEqual(
            d.link_name("gerrit:mediawiki/services/parsoid",
                        manifest(name="Parsoid"), warn=warnings.append),
            "Parsoid",
        )
        self.assertTrue(warnings)  # a surprising link name is visible

    def test_unusable_name_field_falls_back_to_basename(self):
        self.assertEqual(
            d.link_name("gerrit:mediawiki/services/thing", manifest(name="My Thing")),
            "thing",
        )

    def test_no_manifest_falls_back_to_basename(self):
        self.assertEqual(d.link_name("gerrit:mediawiki/services/thing"), "thing")

    def test_override_wins(self):
        self.assertEqual(
            d.link_name(EXT + "Cite", manifest(name="Cite"), overrides={EXT + "Cite": "Cite2"}),
            "Cite2",
        )


class LinkSectionTests(unittest.TestCase):
    def test_by_canonical_tree(self):
        self.assertEqual(d.link_section(EXT + "Cite"), "extensions")
        self.assertEqual(d.link_section(VECTOR), "skins")

    def test_core_is_never_linked(self):
        self.assertIsNone(d.link_section(CORE))

    def test_by_manifest_filename_outside_those_trees(self):
        self.assertEqual(
            d.link_section("gerrit:mediawiki/services/parsoid", manifest()), "extensions"
        )
        self.assertEqual(
            d.link_section("gerrit:foo/bar", manifest("skin.json")), "skins"
        )

    def test_no_manifest_and_no_tree_is_not_linked(self):
        self.assertIsNone(d.link_section("gerrit:mediawiki/vendor"))
        self.assertIsNone(d.link_section(None))


class WalkTests(unittest.TestCase):
    def setUp(self):
        self.warnings = []

    def fixture(self, manifests):
        return lambda canonical: manifests.get(canonical)

    def walk(self, roots, manifests, **kw):
        return d.walk(roots, self.fixture(manifests), warn=self.warnings.append, **kw)

    def test_manifest_pulls_in_core_and_core_pulls_in_vector(self):
        discovered, _origins = self.walk([EXT + "Cite"], {EXT + "Cite": manifest(name="Cite")})
        self.assertEqual(discovered, [CORE, VECTOR])

    def test_transitive(self):
        manifests = {
            EXT + "Translate": manifest(
                name="Translate",
                requires={"extensions": {"UniversalLanguageSelector": "*"}},
            ),
            EXT + "UniversalLanguageSelector": manifest(name="UniversalLanguageSelector"),
        }
        discovered, origins = self.walk([EXT + "Translate"], manifests)
        self.assertIn(EXT + "UniversalLanguageSelector", discovered)
        self.assertEqual(origins[EXT + "UniversalLanguageSelector"], EXT + "Translate")

    def test_core_and_vector_cycle_terminates(self):
        # Vector's skin.json depends on core; core implicitly depends on
        # Vector. Guaranteed cycle -- the visited set is not optional.
        manifests = {VECTOR: d.Manifest("skin.json", {"name": "Vector"})}
        discovered, _origins = self.walk([CORE], manifests)
        self.assertEqual(discovered, [VECTOR])

    def test_mutual_dependency_terminates(self):
        manifests = {
            EXT + "A": manifest(name="A", requires={"extensions": {"B": "*"}}),
            EXT + "B": manifest(name="B", requires={"extensions": {"A": "*"}}),
        }
        discovered, _origins = self.walk([EXT + "A"], manifests)
        self.assertEqual(discovered, [CORE, EXT + "B", VECTOR])

    def test_repo_without_a_manifest_is_a_leaf(self):
        # No manifest means no core, either -- a container directory or a
        # plain repo shouldn't drag MediaWiki in.
        discovered, _origins = self.walk(["gerrit:operations/puppet"], {})
        self.assertEqual(discovered, [])

    def test_roots_are_not_reported_as_discovered(self):
        manifests = {EXT + "Cite": manifest(name="Cite"), CORE: None}
        discovered, _origins = self.walk([EXT + "Cite", CORE], manifests)
        self.assertEqual(discovered, [VECTOR])

    def test_none_roots_are_ignored(self):
        discovered, _origins = self.walk([None, EXT + "Cite"], {EXT + "Cite": manifest()})
        self.assertEqual(discovered, [CORE, VECTOR])

    def test_no_suggests_narrows_the_closure(self):
        # CommunityRequests triples under `suggests`; this is what earns
        # --no-suggests its place.
        m = manifest(name="X", requires={"extensions": {"A": "*"}})
        m.data["suggests"] = {"extensions": {"B": "*", "C": "*"}}
        manifests = {EXT + "X": m}
        full, _o = self.walk([EXT + "X"], manifests)
        narrow, _o2 = self.walk([EXT + "X"], manifests, include_suggests=False)
        self.assertEqual(len(full), 5)  # A, B, C, core, Vector
        self.assertEqual(narrow, [CORE, EXT + "A", VECTOR])

    def test_unresolvable_key_warns_and_is_skipped(self):
        manifests = {EXT + "X": manifest(name="X", requires={"extensions": {"Abuse Filter": "*"}})}
        discovered, _origins = self.walk([EXT + "X"], manifests)
        self.assertEqual(discovered, [CORE, VECTOR])
        self.assertTrue(any("credits name" in w for w in self.warnings))

    def test_missing_dependency_manifest_warns(self):
        manifests = {EXT + "X": manifest(name="X", requires={"extensions": {"Ghost": "*"}})}
        discovered, _origins = self.walk([EXT + "X"], manifests)
        # Still mounted -- it's a real dependency even if we can't see its
        # manifest; the warning is about not being able to recurse.
        self.assertIn(EXT + "Ghost", discovered)
        self.assertTrue(any("no manifest found" in w for w in self.warnings))

    def test_name_mismatch_warns(self):
        # The key is a credits name, so a project whose manifest calls
        # itself something else is probably not the one asked for.
        manifests = {
            EXT + "X": manifest(name="X", requires={"extensions": {"Foo": "*"}}),
            EXT + "Foo": manifest(name="Something Else"),
        }
        self.walk([EXT + "X"], manifests)
        self.assertTrue(any("calls itself" in w for w in self.warnings))

    def test_self_dependency_is_ignored(self):
        manifests = {EXT + "X": manifest(name="X", requires={"extensions": {"X": "*"}})}
        discovered, _origins = self.walk([EXT + "X"], manifests)
        self.assertEqual(discovered, [CORE, VECTOR])


class OriginChainTests(unittest.TestCase):
    def test_chain_back_to_the_root(self):
        origins = {"c": "b", "b": "a"}
        self.assertEqual(d.origin_chain("c", origins, ["a"]), ["a", "b", "c"])

    def test_root_is_its_own_chain(self):
        self.assertEqual(d.origin_chain("a", {}, ["a"]), ["a"])

    def test_cycle_does_not_hang(self):
        # The closure genuinely contains cycles (core <-> Vector).
        self.assertEqual(d.origin_chain("a", {"a": "b", "b": "a"}, []), ["b", "a"])


class MakeManifestForTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with open(os.path.join(self.tmp.name, "extension.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "Local"}, f)

    def test_local_checkout_wins_over_the_network(self):
        def fetch(canonical, warn=None):
            raise AssertionError("should not fetch when a checkout exists")

        manifest_for = d.make_manifest_for(lambda c: self.tmp.name, fetch=fetch)
        self.assertEqual(manifest_for(EXT + "Local").data["name"], "Local")

    def test_falls_back_to_the_network_when_not_cloned(self):
        manifest_for = d.make_manifest_for(
            lambda c: None, fetch=lambda c, warn=None: manifest(name="Remote")
        )
        self.assertEqual(manifest_for(EXT + "X").data["name"], "Remote")

    def test_existing_checkout_without_a_manifest_is_not_fetched(self):
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)

        def fetch(canonical, warn=None):
            raise AssertionError("a cloned repo with no manifest has no manifest")

        manifest_for = d.make_manifest_for(lambda c: empty.name, fetch=fetch)
        self.assertIsNone(manifest_for(EXT + "X"))

    def test_unreachable_repos_are_collected_and_look_like_leaves_to_the_walk(self):
        unreachable = set()
        manifest_for = d.make_manifest_for(
            lambda c: None, fetch=lambda c, warn=None: d.UNREACHABLE,
            unreachable=unreachable,
        )
        # walk() can't act on the difference, so manifest_for still hands
        # it None -- but the caller can tell the user the closure is short.
        self.assertIsNone(manifest_for(EXT + "X"))
        self.assertEqual(unreachable, {EXT + "X"})

    def test_results_are_cached_including_misses(self):
        # Diamond dependencies are the norm: ULS shows up three ways in
        # CommunityRequests' closure.
        calls = []

        def fetch(canonical, warn=None):
            calls.append(canonical)
            return None

        manifest_for = d.make_manifest_for(lambda c: None, fetch=fetch)
        manifest_for(EXT + "X")
        manifest_for(EXT + "X")
        self.assertEqual(calls, [EXT + "X"])


if __name__ == "__main__":
    unittest.main()
