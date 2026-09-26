#!/usr/bin/env python3
"""Unit tests for wmf_sbx_resolve.py -- run with:
  python3 -m unittest discover -s sbx/tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.resolve as r  # noqa: E402

EXAMPLE_CONFIG = os.path.join(
    os.path.dirname(__file__), "..", "reference", "example-repos.yaml"
)


class SplitSchemeTests(unittest.TestCase):
    def test_with_scheme(self):
        self.assertEqual(r.split_scheme("gerrit:mediawiki/core"), ("gerrit", "mediawiki/core"))

    def test_without_scheme(self):
        self.assertEqual(r.split_scheme("mediawiki/core"), (None, "mediawiki/core"))

    def test_bare_name_without_scheme(self):
        self.assertEqual(r.split_scheme("Cite"), (None, "Cite"))


class MatchRuleTests(unittest.TestCase):
    def test_exact_literal_match(self):
        self.assertEqual(
            r.match_rule("gerrit:mediawiki/core", "gerrit:mediawiki/core"), {}
        )

    def test_exact_literal_mismatch(self):
        self.assertIsNone(
            r.match_rule("gerrit:mediawiki/core", "gerrit:mediawiki/core2")
        )

    def test_exact_literal_scheme_mismatch(self):
        self.assertIsNone(
            r.match_rule("gerrit:mediawiki/core", "gitlab:mediawiki/core")
        )

    def test_name_capture(self):
        self.assertEqual(
            r.match_rule(
                "gerrit:mediawiki/extensions/{name}",
                "gerrit:mediawiki/extensions/Cite",
            ),
            {"name": "Cite"},
        )

    def test_name_capture_wrong_prefix(self):
        self.assertIsNone(
            r.match_rule(
                "gerrit:mediawiki/extensions/{name}",
                "gerrit:mediawiki/skins/Vector",
            )
        )

    def test_name_capture_requires_at_least_one_segment(self):
        self.assertIsNone(r.match_rule("gerrit:{name}", "gerrit:"))

    def test_double_star_matches_zero_segments(self):
        self.assertEqual(
            r.match_rule("gerrit:**/{name}", "gerrit:Example"),
            {"name": "Example"},
        )

    def test_double_star_matches_many_segments(self):
        self.assertEqual(
            r.match_rule("gerrit:**/{name}", "gerrit:mediawiki/extensions/Cite"),
            {"name": "Cite"},
        )

    def test_double_star_with_after_segments(self):
        self.assertEqual(
            r.match_rule(
                "gerrit:**/extensions/{name}",
                "gerrit:mediawiki/extensions/Cite",
            ),
            {"name": "Cite"},
        )
        self.assertIsNone(
            r.match_rule(
                "gerrit:**/extensions/{name}",
                "gerrit:mediawiki/skins/Cite",
            )
        )

    def test_more_than_one_double_star_rejected(self):
        with self.assertRaises(ValueError):
            r.match_rule("gerrit:**/**/{name}", "gerrit:a/b/c")

    def test_analytics_more_specific_than_catchall(self):
        canonical = "gerrit:analytics/refinery"
        specific = r.match_rule("gerrit:analytics/{name}", canonical)
        catchall = r.match_rule("gerrit:**/{name}", canonical)
        self.assertEqual(specific, {"name": "refinery"})
        self.assertEqual(catchall, {"name": "refinery"})
        self.assertGreater(
            r.specificity("gerrit:analytics/{name}"), r.specificity("gerrit:**/{name}")
        )


class SpecificityAndRankingTests(unittest.TestCase):
    RULES = [
        {"match": "gerrit:**/{name}", "path": "~/Wikimedia/{name}"},
        {"match": "gerrit:analytics/{name}", "path": "~/Wikimedia/analytics-{name}"},
        {"match": "gerrit:mediawiki/extensions/{name}", "path": "~/Wikimedia/Extensions/{name}"},
        {"match": "gerrit:mediawiki/core", "path": "~/Wikimedia/core"},
    ]

    def test_ranking_most_specific_first(self):
        ranked = r.matching_rules("gerrit:mediawiki/extensions/Cite", self.RULES)
        self.assertEqual(
            [rule["match"] for rule, _ in ranked],
            ["gerrit:mediawiki/extensions/{name}", "gerrit:**/{name}"],
        )

    def test_exact_rule_beats_everything(self):
        ranked = r.matching_rules("gerrit:mediawiki/core", self.RULES)
        self.assertEqual(ranked[0][0]["match"], "gerrit:mediawiki/core")

    def test_analytics_beats_catchall(self):
        ranked = r.matching_rules("gerrit:analytics/refinery", self.RULES)
        self.assertEqual(
            [rule["match"] for rule, _ in ranked],
            ["gerrit:analytics/{name}", "gerrit:**/{name}"],
        )

    def test_no_match(self):
        self.assertEqual(r.matching_rules("gitlab:foo/bar", self.RULES), [])


class ResolveDirectoryTests(unittest.TestCase):
    RULES = [
        {"match": "gerrit:**/{name}", "path": "/w/{name}"},
        {"match": "gerrit:analytics/{name}", "path": "/w/analytics-{name}"},
    ]

    def test_existing_directory_at_longest_match_wins(self):
        existing = {"/w/analytics-refinery"}
        path, rule, needs_clone = r.resolve_directory(
            "gerrit:analytics/refinery", self.RULES, exists=lambda p: p in existing
        )
        self.assertEqual(path, "/w/analytics-refinery")
        self.assertFalse(needs_clone)
        self.assertEqual(rule["match"], "gerrit:analytics/{name}")

    def test_falls_back_to_looser_existing_directory(self):
        # cananian's example: an old flat "refinery" checkout should still
        # be found even though the specific analytics-{name} rule wins for
        # a *new* clone.
        existing = {"/w/refinery"}
        path, rule, needs_clone = r.resolve_directory(
            "gerrit:analytics/refinery", self.RULES, exists=lambda p: p in existing
        )
        self.assertEqual(path, "/w/refinery")
        self.assertFalse(needs_clone)
        self.assertEqual(rule["match"], "gerrit:**/{name}")

    def test_new_clone_uses_longest_match_even_if_neither_exists(self):
        path, rule, needs_clone = r.resolve_directory(
            "gerrit:analytics/refinery", self.RULES, exists=lambda p: False
        )
        self.assertEqual(path, "/w/analytics-refinery")
        self.assertTrue(needs_clone)
        self.assertEqual(rule["match"], "gerrit:analytics/{name}")

    def test_no_matching_rule_is_an_error(self):
        with self.assertRaises(r.ResolutionError):
            r.resolve_directory("gitlab:foo/bar", self.RULES, exists=lambda p: False)

    def test_gitreview_mismatch_is_skipped_even_if_directory_exists(self):
        # /w/analytics-refinery exists but its .gitreview says it's really
        # some other project -- someone repurposed/renamed the directory --
        # so we should fall through to the looser rule, exactly like a
        # plain missing directory.
        existing = {"/w/analytics-refinery", "/w/refinery"}
        gitreviews = {"/w/analytics-refinery": "some/other/project"}
        path, rule, needs_clone = r.resolve_directory(
            "gerrit:analytics/refinery", self.RULES,
            exists=lambda p: p in existing,
            gitreview_project=lambda p: gitreviews.get(p),
        )
        self.assertEqual(path, "/w/refinery")
        self.assertFalse(needs_clone)
        self.assertEqual(rule["match"], "gerrit:**/{name}")

    def test_gitreview_match_is_accepted(self):
        existing = {"/w/analytics-refinery"}
        path, rule, needs_clone = r.resolve_directory(
            "gerrit:analytics/refinery", self.RULES,
            exists=lambda p: p in existing,
            gitreview_project=lambda p: "analytics/refinery",
        )
        self.assertEqual(path, "/w/analytics-refinery")
        self.assertFalse(needs_clone)

    def test_missing_gitreview_file_is_trusted_as_before(self):
        existing = {"/w/analytics-refinery"}
        path, rule, needs_clone = r.resolve_directory(
            "gerrit:analytics/refinery", self.RULES,
            exists=lambda p: p in existing,
            gitreview_project=lambda p: None,
        )
        self.assertEqual(path, "/w/analytics-refinery")
        self.assertFalse(needs_clone)

    def test_gitreview_check_only_applies_to_gerrit(self):
        rules = [{"match": "gitlab:**/{name}", "path": "/w/{name}"}]
        path, rule, needs_clone = r.resolve_directory(
            "gitlab:foo/bar", rules,
            exists=lambda p: True,
            gitreview_project=lambda p: "totally/unrelated",
        )
        self.assertEqual(path, "/w/bar")
        self.assertFalse(needs_clone)

    def test_all_candidates_mismatched_but_none_exist_falls_through_to_new_clone(self):
        # Every candidate's .gitreview would mismatch if any existed, but
        # none actually exist -- that's a plain, safe needs_clone.
        path, rule, needs_clone = r.resolve_directory(
            "gerrit:analytics/refinery", self.RULES,
            exists=lambda p: False,
            gitreview_project=lambda p: "some/other/project",
        )
        self.assertEqual(path, "/w/analytics-refinery")
        self.assertTrue(needs_clone)
        self.assertEqual(rule["match"], "gerrit:analytics/{name}")

    def test_most_specific_candidate_exists_but_mismatched_is_an_error(self):
        # cananian's real case: ~/Wikimedia/Extensions/Parsoid is a symlink
        # to ~/Wikimedia/Parsoid, so *both* candidate paths exist but both
        # have the wrong .gitreview -- this must never be reported as
        # needs_clone, since that path is already occupied by something
        # else.
        existing = {"/w/analytics-refinery", "/w/refinery"}
        with self.assertRaises(r.ResolutionError) as ctx:
            r.resolve_directory(
                "gerrit:analytics/refinery", self.RULES,
                exists=lambda p: p in existing,
                gitreview_project=lambda p: "some/other/project",
            )
        self.assertIn("/w/analytics-refinery", str(ctx.exception))
        self.assertIn("some/other/project", str(ctx.exception))


class GitreviewProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def _write_gitreview(self, contents):
        with open(os.path.join(self.tmpdir, ".gitreview"), "w", encoding="utf-8") as f:
            f.write(contents)

    def test_reads_project_line(self):
        self._write_gitreview("[gerrit]\nhost=gerrit.wikimedia.org\nproject=mediawiki/extensions/Cite.git\n")
        self.assertEqual(r.gitreview_project(self.tmpdir), "mediawiki/extensions/Cite")

    def test_strips_dotgit_suffix_only_when_present(self):
        self._write_gitreview("project=mediawiki/core\n")
        self.assertEqual(r.gitreview_project(self.tmpdir), "mediawiki/core")

    def test_missing_file_returns_none(self):
        self.assertIsNone(r.gitreview_project(self.tmpdir))

    def test_file_without_project_line_returns_none(self):
        self._write_gitreview("[gerrit]\nhost=gerrit.wikimedia.org\n")
        self.assertIsNone(r.gitreview_project(self.tmpdir))


class ResolveCanonicalPathTests(unittest.TestCase):
    def test_explicit_scheme_used_as_is(self):
        self.assertEqual(
            r.resolve_canonical_path("gerrit:mediawiki/extensions/Cite"),
            "gerrit:mediawiki/extensions/Cite",
        )

    def test_gitlab_scheme_used_as_is_no_lookup(self):
        def fail_search(_):
            raise AssertionError("gitlab: input should not trigger a Gerrit search")

        self.assertEqual(
            r.resolve_canonical_path("gitlab:daniel/claudebox", search=fail_search),
            "gitlab:daniel/claudebox",
        )

    def test_full_path_without_scheme_defaults_to_gerrit(self):
        self.assertEqual(
            r.resolve_canonical_path("mediawiki/extensions/Cite"),
            "gerrit:mediawiki/extensions/Cite",
        )

    def test_bare_name_unambiguous(self):
        def fake_search(substring):
            self.assertEqual(substring, "Cite")
            return [
                "mediawiki/extensions/Cite",
                "mediawiki/extensions/CiteDrawer",
                "mediawiki/extensions/CiteThisPage",
                "mediawiki/extensions/SemanticCite",
            ], set()

        self.assertEqual(
            r.resolve_canonical_path("Cite", search=fake_search),
            "gerrit:mediawiki/extensions/Cite",
        )

    def test_bare_name_no_match(self):
        with self.assertRaises(r.ResolutionError):
            r.resolve_canonical_path("Nonexistent", search=lambda s: ([], set()))

    def test_bare_name_ambiguous(self):
        def fake_search(substring):
            return ["foo/Widget", "bar/Widget"], set()

        with self.assertRaises(r.ResolutionError) as ctx:
            r.resolve_canonical_path("Widget", search=fake_search)
        self.assertIn("foo/Widget", str(ctx.exception))
        self.assertIn("bar/Widget", str(ctx.exception))

    def test_archived_sole_match_still_resolves_and_warns(self):
        # analytics/asana-stats has no live namesake -- it must still
        # resolve by bare name (excluding it outright was a regression),
        # but the caller gets a chance to warn about it.
        def fake_search(substring):
            return ["analytics/asana-stats"], {"analytics/asana-stats"}

        warnings = []
        result = r.resolve_canonical_path(
            "asana-stats", search=fake_search, warn=warnings.append
        )
        self.assertEqual(result, "gerrit:analytics/asana-stats")
        self.assertEqual(len(warnings), 1)
        self.assertIn("ARCHIVED", warnings[0])
        self.assertIn("analytics/asana-stats", warnings[0])

    def test_non_archived_match_does_not_warn(self):
        def fake_search(substring):
            return ["mediawiki/services/parsoid"], set()

        warnings = []
        r.resolve_canonical_path("parsoid", search=fake_search, warn=warnings.append)
        self.assertEqual(warnings, [])

    def test_prefers_non_archived_over_case_insensitive_archived_match(self):
        # cananian's real case: bare "Parsoid" case-insensitively matches
        # both the archived extension and the live service -- the live,
        # non-archived one wins with no ambiguity error and no warning.
        # Pass the full gerrit:... path if you specifically want the
        # archived one.
        def fake_search(substring):
            return (
                ["mediawiki/extensions/Parsoid", "mediawiki/services/parsoid"],
                {"mediawiki/extensions/Parsoid"},
            )

        warnings = []
        result = r.resolve_canonical_path(
            "Parsoid", search=fake_search, warn=warnings.append
        )
        self.assertEqual(result, "gerrit:mediawiki/services/parsoid")
        self.assertEqual(warnings, [])

    def test_ambiguous_among_non_archived_candidates_only(self):
        # Two non-archived candidates and one archived one all
        # case-insensitively match -- the archived one is dropped from
        # consideration entirely, but the remaining two are still
        # genuinely ambiguous.
        def fake_search(substring):
            return (
                ["foo/Widget", "bar/widget", "baz/Widget"],
                {"baz/Widget"},
            )

        with self.assertRaises(r.ResolutionError) as ctx:
            r.resolve_canonical_path("Widget", search=fake_search)
        self.assertIn("foo/Widget", str(ctx.exception))
        self.assertIn("bar/widget", str(ctx.exception))
        self.assertNotIn("baz/Widget", str(ctx.exception))

    def test_warn_is_optional(self):
        def fake_search(substring):
            return ["analytics/asana-stats"], {"analytics/asana-stats"}

        # No warn= given (defaults to None) -- must not raise.
        result = r.resolve_canonical_path("asana-stats", search=fake_search)
        self.assertEqual(result, "gerrit:analytics/asana-stats")


class GerritSearchTests(unittest.TestCase):
    """gerrit_search() itself, mocking urllib.request.urlopen."""

    def _fake_response(self, payload):
        body = ")]}'\n" + json.dumps(payload)
        cm = mock.MagicMock()
        cm.__enter__.return_value.read.return_value = body.encode("utf-8")
        return cm

    def test_returns_all_names_and_flags_archived(self):
        payload = {
            "mediawiki/extensions/Parsoid": {"description": "[ARCHIVED] old wrapper"},
            "mediawiki/services/parsoid": {"description": "The Parsoid service"},
        }
        with mock.patch.object(r.urllib.request, "urlopen", return_value=self._fake_response(payload)) as m:
            names, archived = r.gerrit_search("parsoid")
        self.assertEqual(sorted(names), sorted(payload.keys()))
        self.assertEqual(archived, {"mediawiki/extensions/Parsoid"})
        self.assertIn("&d", m.call_args[0][0])

    def test_missing_description_is_not_treated_as_archived(self):
        payload = {"mediawiki/core": {}}
        with mock.patch.object(r.urllib.request, "urlopen", return_value=self._fake_response(payload)):
            names, archived = r.gerrit_search("core")
        self.assertEqual(names, ["mediawiki/core"])
        self.assertEqual(archived, set())


class ExactRuleCanonicalsTests(unittest.TestCase):
    def test_maps_exact_rules_only(self):
        rules = [
            {"match": "gerrit:mediawiki/core", "path": "/w/core"},
            {"match": "gerrit:mediawiki/services/parsoid", "path": "/w/Parsoid"},
            {"match": "gerrit:mediawiki/extensions/{name}", "path": "/w/Extensions/{name}"},
            {"match": "gerrit:**/{name}", "path": "/w/{name}"},
        ]
        mapping = r.exact_rule_canonicals(rules)
        self.assertEqual(
            mapping,
            {
                os.path.realpath("/w/core"): "gerrit:mediawiki/core",
                os.path.realpath("/w/Parsoid"): "gerrit:mediawiki/services/parsoid",
            },
        )

    def test_expands_user_and_realpaths(self):
        rules = [{"match": "gerrit:mediawiki/core", "path": "~/Wikimedia/core"}]
        mapping = r.exact_rule_canonicals(rules)
        self.assertEqual(
            mapping,
            {os.path.realpath(os.path.expanduser("~/Wikimedia/core")): "gerrit:mediawiki/core"},
        )

    def test_empty_rules(self):
        self.assertEqual(r.exact_rule_canonicals([]), {})


class ReverseResolveTests(unittest.TestCase):
    RULES = [
        {"match": "gerrit:mediawiki/core", "path": "/w/core"},
        {"match": "gerrit:mediawiki/services/parsoid", "path": "/w/Parsoid"},
        {"match": "gerrit:mediawiki/extensions/{name}", "path": "/w/Extensions/{name}"},
        {"match": "gerrit:**/{name}", "path": "/w/{name}"},
    ]

    def test_gitreview_authoritative_with_wildcard_rule_match(self):
        # /w/Cite has no exact rule, but its .gitreview + the
        # extensions/{name} rule's own expansion agree on this exact path.
        canonical, rule = r.reverse_resolve(
            "/w/Extensions/Cite", self.RULES,
            exists=lambda p: True,
            gitreview_project=lambda p: "mediawiki/extensions/Cite",
        )
        self.assertEqual(canonical, "gerrit:mediawiki/extensions/Cite")
        self.assertEqual(rule["match"], "gerrit:mediawiki/extensions/{name}")

    def test_gitreview_authoritative_but_no_rule_expands_to_this_path(self):
        # A real checkout whose .gitreview names a project no configured
        # rule would ever place here -- still identified via .gitreview,
        # but with no rule to report (the "<no rule>" CLI case).
        canonical, rule = r.reverse_resolve(
            "/w/some-oddly-named-checkout", self.RULES,
            exists=lambda p: True,
            gitreview_project=lambda p: "mediawiki/extensions/Cite",
        )
        self.assertEqual(canonical, "gerrit:mediawiki/extensions/Cite")
        self.assertIsNone(rule)

    def test_gitreview_wins_over_what_the_directory_location_would_suggest(self):
        # cananian's real case: ~/Wikimedia/Extensions/Parsoid is a symlink
        # to the live parsoid *service* checkout -- its .gitreview says
        # mediawiki/services/parsoid, not mediawiki/extensions/Parsoid, and
        # that's what must be reported, even though this path sits exactly
        # where the extensions/{name} rule would expect the (archived)
        # extension to live.
        canonical, rule = r.reverse_resolve(
            "/w/Extensions/Parsoid", self.RULES,
            exists=lambda p: True,
            gitreview_project=lambda p: "mediawiki/services/parsoid",
        )
        self.assertEqual(canonical, "gerrit:mediawiki/services/parsoid")
        self.assertIsNone(rule)  # no rule expands to /w/Extensions/Parsoid for this canonical

    def test_no_gitreview_falls_back_to_exact_rule(self):
        canonical, rule = r.reverse_resolve(
            "/w/core", self.RULES, exists=lambda p: True, gitreview_project=lambda p: None
        )
        self.assertEqual(canonical, "gerrit:mediawiki/core")
        self.assertEqual(rule["match"], "gerrit:mediawiki/core")

    def test_no_gitreview_and_wildcard_only_match_is_unidentified(self):
        # /w/Skins isn't an exact-rule path, and with no .gitreview to
        # disambiguate, a "**"/{name} rule can't be safely inverted.
        canonical, rule = r.reverse_resolve(
            "/w/Skins", self.RULES, exists=lambda p: True, gitreview_project=lambda p: None
        )
        self.assertIsNone(canonical)
        self.assertIsNone(rule)

    def test_missing_directory_raises(self):
        with self.assertRaises(r.ResolutionError):
            r.reverse_resolve("/w/nope", self.RULES, exists=lambda p: False)

    def test_expands_user(self):
        rules = [{"match": "gerrit:mediawiki/core", "path": "~/Wikimedia/core"}]
        canonical, rule = r.reverse_resolve(
            "~/Wikimedia/core", rules, exists=lambda p: True, gitreview_project=lambda p: None
        )
        self.assertEqual(canonical, "gerrit:mediawiki/core")
        self.assertEqual(rule["match"], "gerrit:mediawiki/core")


def _projects(*paths):
    return [{"path_with_namespace": p} for p in paths]


class FakeGitlab:
    """A `get` for gitlab_search/gitlab_project: {url-substring: JSON}.
    The first key found in the URL wins; anything else is an empty page."""
    def __init__(self, table):
        self.table = table
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        for key, data in self.table.items():
            if key in url:
                return data, ""
        return [], ""


class GitlabSearchTests(unittest.TestCase):
    def test_first_group_with_a_hit_wins_and_marks_archived(self):
        get = FakeGitlab({
            "archived=true": _projects("repos/a/wmf-claude-old"),
            "/groups/repos/": _projects("repos/a/wmf-claude-old", "repos/b/wmf-claude"),
        })
        names, archived = r.gitlab_search("wmf-claude", get=get)
        self.assertEqual(names, ["repos/a/wmf-claude-old", "repos/b/wmf-claude"])
        self.assertEqual(archived, {"repos/a/wmf-claude-old"})
        self.assertFalse(any("toolforge-repos" in u for u in get.urls))

    def test_falls_through_to_next_group(self):
        get = FakeGitlab({"archived=true": [],
                          "/groups/toolforge-repos/": _projects("toolforge-repos/xtools")})
        names, archived = r.gitlab_search("xtools", get=get)
        self.assertEqual(names, ["toolforge-repos/xtools"])
        self.assertEqual(archived, set())

    def test_no_hits_anywhere(self):
        self.assertEqual(r.gitlab_search("nope", get=FakeGitlab({})), ([], set()))

    def test_follows_pagination(self):
        def get(url):
            page = int(url.rsplit("page=", 1)[1])
            return _projects(f"repos/p{page}"), "2" if page == 1 else ""
        self.assertEqual(r._gitlab_paths("http://x?a=b", get), ["repos/p1", "repos/p2"])

    def test_get_maps_404_to_none_and_other_errors_to_resolution_error(self):
        import io
        import urllib.error

        def fail(code):
            return urllib.error.HTTPError("u", code, "m", {}, io.BytesIO(b""))
        with mock.patch.object(r.urllib.request, "urlopen", side_effect=fail(404)):
            self.assertEqual(r._gitlab_get("u"), (None, ""))
        with mock.patch.object(r.urllib.request, "urlopen", side_effect=fail(401)):
            with self.assertRaises(r.ResolutionError):
                r._gitlab_get("u")
        with mock.patch.object(r.urllib.request, "urlopen", side_effect=OSError("down")):
            with self.assertRaises(r.ResolutionError):
                r._gitlab_get("u")


class GitlabFallbackTests(unittest.TestCase):
    def test_bare_name_falls_back_to_gitlab_when_gerrit_has_no_match(self):
        result = r.resolve_canonical_path(
            "wmf-claude", search=lambda s: ([], set()),
            gitlab=lambda s: (["repos/product-safety-and-integrity/wmf-claude",
                               "repos/x/wmf-claude-extras"], set()))
        self.assertEqual(result, "gitlab:repos/product-safety-and-integrity/wmf-claude")

    def test_gerrit_match_never_consults_gitlab(self):
        def boom(s):
            raise AssertionError("GitLab must not be queried")
        result = r.resolve_canonical_path(
            "core", search=lambda s: (["mediawiki/core"], set()), gitlab=boom)
        self.assertEqual(result, "gerrit:mediawiki/core")

    def test_gitlab_ambiguity_and_archived_warning(self):
        with self.assertRaises(r.ResolutionError):
            r.resolve_canonical_path(
                "tool", search=lambda s: ([], set()),
                gitlab=lambda s: (["repos/a/tool", "toolforge-repos/tool"], set()))
        warnings = []
        result = r.resolve_canonical_path(
            "tool", search=lambda s: ([], set()), warn=warnings.append,
            gitlab=lambda s: (["repos/a/tool"], {"repos/a/tool"}))
        self.assertEqual(result, "gitlab:repos/a/tool")
        self.assertIn("ARCHIVED", warnings[0])

    def test_no_match_anywhere_names_both_forges(self):
        with self.assertRaisesRegex(r.ResolutionError, "Gerrit or GitLab"):
            r.resolve_canonical_path(
                "zzz", search=lambda s: ([], set()), gitlab=lambda s: ([], set()))

    def test_without_gitlab_the_message_stays_gerrit_only(self):
        with self.assertRaisesRegex(r.ResolutionError, "No Gerrit project"):
            r.resolve_canonical_path("zzz", search=lambda s: ([], set()))


class GitlabUpstreamTests(unittest.TestCase):
    def test_follows_forks_to_the_root(self):
        table = {
            "cscott/wmf-claude": {"forked_from_project": {"path_with_namespace": "bob/wmf-claude"}},
            "bob/wmf-claude": {"forked_from_project": {"path_with_namespace": "repos/x/wmf-claude"}},
            "repos/x/wmf-claude": {"forked_from_project": None},
        }
        self.assertEqual(r.gitlab_upstream("cscott/wmf-claude", project=table.get),
                         "repos/x/wmf-claude")

    def test_mr_default_target_self_stops_the_walk(self):
        table = {"a/f": {"mr_default_target_self": True,
                         "forked_from_project": {"path_with_namespace": "repos/x/f"}}}
        self.assertEqual(r.gitlab_upstream("a/f", project=table.get), "a/f")

    def test_unknown_project_and_cycles_mean_a_project_in_the_chain(self):
        self.assertEqual(r.gitlab_upstream("a/f", project=lambda p: None), "a/f")
        table = {"a/f": {"forked_from_project": {"path_with_namespace": "b/f"}},
                 "b/f": {"forked_from_project": {"path_with_namespace": "a/f"}}}
        self.assertIn(r.gitlab_upstream("a/f", project=table.get), {"a/f", "b/f"})

    def test_gitlab_project_quotes_the_path(self):
        get = FakeGitlab({"repos%2Fx%2Fy": {"id": 1}})
        self.assertEqual(r.gitlab_project("repos/x/y", get=get), {"id": 1})


class CanonicalFromUrlTests(unittest.TestCase):
    def test_recognised_forms(self):
        for url in (
            "https://gitlab.wikimedia.org/repos/x/y.git",
            "https://gitlab.wikimedia.org/repos/x/y",
            "git@gitlab-ssh.wikimedia.org:repos/x/y.git",
            "ssh://git@gitlab-ssh.wikimedia.org:2222/repos/x/y.git",
            " https://gitlab.wikimedia.org/repos/x/y/\n",
        ):
            self.assertEqual(r.canonical_from_url(url), "gitlab:repos/x/y", url)

    def test_other_hosts_and_junk(self):
        for url in ("https://github.com/a/b", "", None,
                    "https://gerrit.wikimedia.org/r/mediawiki/core"):
            self.assertIsNone(r.canonical_from_url(url))

    def test_git_origin_url(self):
        class Done:
            returncode, stdout = 0, "https://gitlab.wikimedia.org/a/b.git\n"
        seen = {}

        def run(argv, **kw):
            seen.update(argv=argv, **kw)
            return Done()
        self.assertEqual(r.git_origin_url("/w/b", run=run), "https://gitlab.wikimedia.org/a/b.git")
        self.assertEqual(seen["argv"], ["git", "-C", "/w/b", "remote", "get-url", "origin"])
        self.assertEqual(seen["stdin"], r.subprocess.DEVNULL)
        Done.returncode = 1
        self.assertIsNone(r.git_origin_url("/w/b", run=run))


class ReverseResolveOriginTests(unittest.TestCase):
    RULES = [{"match": "gitlab:repos/**/{name}", "path": "/w/{name}"}]

    def test_origin_url_identifies_a_gitlab_checkout(self):
        canonical, rule = r.reverse_resolve(
            "/w/wmf-claude", self.RULES, exists=lambda p: True,
            gitreview_project=lambda p: None,
            origin_url=lambda p: "https://gitlab.wikimedia.org/repos/psi/wmf-claude.git")
        self.assertEqual(canonical, "gitlab:repos/psi/wmf-claude")
        self.assertEqual(rule["match"], "gitlab:repos/**/{name}")

    def test_unknown_origin_is_unresolved(self):
        self.assertEqual(
            r.reverse_resolve("/w/x", self.RULES, exists=lambda p: True,
                              gitreview_project=lambda p: None,
                              origin_url=lambda p: "https://github.com/a/x"),
            (None, None))


@unittest.skipUnless(r.yaml is not None, "PyYAML not installed")
class LoadConfigTests(unittest.TestCase):
    def test_loads_example_config(self):
        config = r.load_config(EXAMPLE_CONFIG)
        self.assertIn("rules", config)
        self.assertIn("extra_dependencies", config)
        matches = [rule["match"] for rule in config["rules"]]
        self.assertIn("gerrit:mediawiki/core", matches)
        self.assertIn("gerrit:mediawiki/extensions/{name}", matches)
        self.assertIn("gerrit:analytics/{name}", matches)
        self.assertIn("gerrit:**/{name}", matches)
        self.assertIn("gitlab:**/{name}", matches)

    def test_missing_config_file_returns_empty(self):
        config = r.load_config("/nonexistent/path/repos.yaml")
        self.assertEqual(
            config,
            {"rules": [], "extra_dependencies": {}, "extra_environment": {}, "extra_packages": []},
        )

    def test_loads_extra_environment_and_packages_defaults(self):
        config = r.load_config(EXAMPLE_CONFIG)
        self.assertEqual(config["extra_environment"], {})
        self.assertEqual(config["extra_packages"], [])

    def test_end_to_end_with_example_config(self):
        def fake_search(substring):
            return ["mediawiki/extensions/" + substring], set()

        canonical, path, rule, needs_clone = r.resolve(
            "Cite", config_path=EXAMPLE_CONFIG, search=fake_search, exists=lambda p: False
        )
        self.assertEqual(canonical, "gerrit:mediawiki/extensions/Cite")
        self.assertEqual(path, os.path.expanduser("~/Wikimedia/Extensions/Cite"))
        self.assertTrue(needs_clone)

        canonical, path, rule, needs_clone = r.resolve(
            "gerrit:analytics/refinery", config_path=EXAMPLE_CONFIG, exists=lambda p: False
        )
        self.assertEqual(path, os.path.expanduser("~/Wikimedia/analytics-refinery"))


if __name__ == "__main__":
    unittest.main()
