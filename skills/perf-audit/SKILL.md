---
description: Audit a Wikimedia component (extension, library, service, frontend module) for production-relevant performance regressions against the WMF backend and frontend performance practices. Use when the engineer wants a focused perf review — pre-deploy hardening, post-incident regression hunt, or routine pre-merge check. Discriminates between measured regressions and untested concerns; downgrades anything without a number behind it.
argument-hint: "[component-path or extension-name] [backend|frontend|both]"
allowed-tools:
  - Read
  - Glob
  - Grep
  - Bash
---

# WMF Performance Audit

The engineer is a **Wikimedia Foundation engineer** preparing code for production. Reports are read by the patch author, their reviewer, and (for hot-path or cross-cutting changes) performance-expert colleagues across the Foundation — especially Principal engineers, who are the standing destination for performance review requests now that there is no dedicated Performance Team. They care about what actually slows wikis down today on the production cluster — not theoretical inefficiencies. A long list of "could be faster" notes is worse than one measured regression with a fix.

## Rules the audit enforces

A finding must trace back to a rule on one of these pages (or to a measurable production signal). If it doesn't, it's a stylistic preference and should be dropped.

- **Performance budgeting** — <https://www.mediawiki.org/wiki/Performance_budgeting>. Canonical source for current numeric budgets. Quote it; do not invent budgets in this skill or assert numbers from memory if they conflict with the live page.
- **Backend performance practices** — <https://wikitech.wikimedia.org/wiki/MediaWiki_Engineering/Guides/Backend_performance_practices>
- **Frontend performance practices** — <https://wikitech.wikimedia.org/wiki/MediaWiki_Engineering/Guides/Frontend_performance_practices>

## Architectural context (not a source of findings)

Useful background for understanding *why* the perf rules are shaped the way they are — async work and statelessness imply jobification and cacheability, clear boundaries imply sensible cache keys, etc. Do **not** file findings whose only basis is one of these pages: design-quality concerns (separation of concerns, dependency direction, coupling, layering) are out of scope for this skill. A perf-audit report that flags "violates principle X" without a measurement defeats the discipline this skill exists to enforce.

- **Architectural principles** — <https://www.mediawiki.org/wiki/Wikimedia_Engineering_Architecture_Principles>
- **Architecture guidelines** — <https://www.mediawiki.org/wiki/Architecture_guidelines>

## Scope selection

`$ARGUMENTS` may include `backend`, `frontend`, or `both` after the component path. If unspecified, ask the engineer once — these are qualitatively different audits and producing the union creates noise.

- **Backend audit** — PHP request handlers, hooks, service classes, DB queries, jobs, maintenance scripts, caching layers, REST/API handlers.
- **Frontend audit** — ResourceLoader modules, JS bundle shape, CSS critical path, OutputPage additions, render-blocking resources, image loading, mobile behaviour.
- **Both** — only for full-stack features (e.g. a new special page with its own JS module). Run the phases separately and produce two report sections.

## Universal tax: new ResourceLoader modules

A new ResourceLoader module is **not free**. Even if it's small, even if it's lazy-loaded, even if it ships only to logged-in users:

- It enters the dependency graph that every page-view computes against.
- Its existence costs cache space in the startup module manifest, which is fetched by every browser visiting any wiki.
- Its load-on-demand still requires a request to ResourceLoader, which serialises behind whatever else is loading.
- Multiply by Wikimedia traffic: a 1 KB module loaded on 1% of page views worldwide is still tens of millions of bytes per day shipped from the cluster.

Treat *adding* a new module as a decision needing the same scrutiny as adding a new DB table. Always check whether the new code can live in an existing module (extending its `packageFiles` or adding to its `dependencies`), be inlined into the consuming module, or be deferred behind a user gesture without a new module at all. **A new module needs an explicit rationale in the finding's "Production impact" section** — "we just added one because it felt cleaner" is not enough.

This rule is in the report regardless of scope: if the patch introduces a new module, flag it.

## Performance budgets

The numbers that matter live on the **Performance budgeting** page (linked above). Pull the current budgets from there at audit time — they shift as the cluster shifts. Do **not** hard-code budget numbers in findings; quote the page.

What the budgeting page covers, at a glance, so you know what to look up:

- Web-request latency targets (p50 / p75 / p99), differentiated by cached vs. uncached.
- DB-query budgets (per-request count, per-query latency, row-read caps).
- ResourceLoader module budgets (bytes added to startup, bytes loaded per page).
- Front-end metrics: First Contentful Paint, Largest Contentful Paint, CLS, TBT — and the device / network classes those budgets assume (mid-range Android, 3G).
- Memory and CPU budgets for jobs and maintenance scripts.

When the budgeting page is silent on something, fall back to the practice pages and to whatever Grafana / Phatality data the engineer can produce. **Do not invent a budget**; if there is no published number, frame the finding in terms of relative impact (delta vs. current code) rather than absolute thresholds.

## Severity rubric

- **P0 — Production regression.** Measured slowdown on a hot path that would page SRE or trip a published SLO (parser timeouts, DB connection exhaustion, FPM worker pile-up, ResourceLoader cache stampede).
- **P1 — Will-bite.** Will degrade noticeably at WMF scale: unindexed query against a large table, N+1 in a per-render hook, sync HTTP in the request path, render-blocking JS added to default modules, DOM growth proportional to unbounded user input, a new RL module added without explicit rationale.
- **P2 — Worth-fixing.** Measurable inefficiency that won't trip alerts today but is wrong by the guideline (e.g. `WANCache` used without `getWithSetCallback`, `SELECT *`, missing `LIMIT` on a query that happens to be small today, render-blocking 1 KB of CSS).
- **P3 — Defence-in-depth / nit.** Style-of-the-guide notes that don't move the needle today (missing `static` on closures, an extra `count()` call inside a small loop). Often best left as a comment, not a finding.
- **Not currently a regression.** Code that *looks* concerning but is gated by an upstream limit (e.g. `$wgMaxArticleSize`, a hardcoded caller arg, a non-hot caller). Document with the gate and move on.

## Methodology

Work the steps in order. Don't skip ahead.

### 1. Establish what `$ARGUMENTS` is and how it's reached

- Identify entry points (request handlers, hook handlers, service-wired classes, JS modules, CSS files).
- For each entry point, determine the **call frequency in production**: every page view, every edit, every save, every login, on a special page only, lazily on user gesture, etc. The same code on a per-render path vs. a once-per-session path has wildly different findings.
- If you can't say **"this code runs on path X, called Y times per request, on Z% of page views,"** you don't yet have the context to grade severity.

### 2. Map the upstream guards

Before claiming a hot-path regression, check what already throttles or short-circuits the call.

- **Caching layers ahead of PHP**: Varnish/ATS edge cache, ParserCache, `OutputPage::tryParserCache`, message cache. A function that runs "on every page view" may actually run on cache misses only.
- **Job queue offload**: `DeferredUpdates`, `JobQueueGroup::push`. Work scheduled here is not in the user's request path.
- **Lazy-loading boundaries**: ResourceLoader `mw.loader.using()`, position: bottom, `dependencies`. JS in a lazy module is not on the critical path (but the module's *existence* still costs — see the universal-tax rule).
- **User-gesture gating**: features behind a click handler are off the initial render path.
- **`$wgMaxArticleSize`, `$wgMaxImageArea`, action API `PARAM_MAX`** etc. — caps that bound the input size.

If the work is already deferred / cached / gated, downgrade the finding accordingly. Don't file "this loop is O(N²) where N is page content" if the parser already capped N upstream.

### 3. Enumerate the perf sinks

For each entry point, classify what the code does that could regress performance.

**Backend sinks:**
- **DB**: every `select*` / `update` / `insert` / `delete` / `newSelectQueryBuilder` call. For each: which connection (DB_REPLICA / DB_PRIMARY), which table, which index, what row count, called in a loop?
- **Cache**: every `WANObjectCache` / `BagOStuff` / `APCu` call. Is it `getWithSetCallback` (correct) or manual `get` + `set` (race-prone)? Is the key versioned? What's the TTL?
- **HTTP / network**: every `HttpRequestFactory`, `MultiHttpClient`, shell-out, external service call. Is it in the request path or in a job?
- **Compute**: nested loops over user data, regex on large strings, JSON encode/decode of large structures, deep recursion, `array_map` over a result set when a SQL `GROUP BY` would do.
- **Memory**: `iterateRecursive`, loading whole result sets, building giant strings, deep cloning.
- **Hooks**: handlers on hot hooks (`ParserBeforeInternalParse`, `BeforePageDisplay`, `OutputPageBeforeHTML`, `GetPreferences`) are charged to every request that fires them.

**Frontend sinks:**
- **New ResourceLoader modules**: see the universal-tax rule above. Always flag.
- **ResourceLoader module shape**: bundle size (minified, gzipped), `dependencies` (does it pull in the world?), `position` (top = render-blocking), `targets`, `group`.
- **Critical-path additions**: `OutputPage::addModules` for a module that's not strictly needed above-the-fold; `addInlineScript`; `addMeta` for blocking redirects.
- **CSS**: selector specificity, descendant selectors on `*`, layout-triggering properties (`width`, `height`, `top`, `left`) animated without `transform`, large background images inlined as data: URIs.
- **JS runtime**: layout thrash (interleaved read/write of layout properties), synchronous XHR, large JSON parse on main thread, unbounded `addEventListener` registration without removal.
- **Images / media**: missing `loading="lazy"`, missing `srcset`, oversized originals served to small viewports.
- **DOM**: emitting one element per row of user input without pagination / virtualization.

### 4. For each candidate, produce a measurement before filing

This is the discipline that separates a finding from a guideline-quoting note. For each candidate:

1. State the regression as a number with units: *"this query reads ~50,000 rows because there's no index on `bt_address` for the WHERE clause"*, *"this hook adds ~120 ms p99 per page view because it makes a synchronous HTTP call to the Wikidata API"*, *"this module adds 18 KB of minified JS to the default page load"*, *"this patch adds a new RL module loaded on every desktop page view"*.
2. Source the number. Acceptable sources, in order of preference:
   - **Run `EXPLAIN` / `EXPLAIN ANALYZE`** for DB findings against the engineer's local wiki (per `CLAUDE.md`).
   - **Measure** with PHP request profiling (`MW_DEBUG`, ExcimerProfiler if available), browser DevTools (via chrome-devtools MCP), `mw.track('stats.*')` traces, or production Grafana/Phatality data the engineer has shared.
   - **Compute** from code-visible quantities: bundle size from the file, row reads from the query shape, hook frequency from caller count via codesearch.
   - **Cite** a documented rule from the guideline pages above (e.g. "the backend guide says sync HTTP in the request path is forbidden — this is one"). This is the weakest acceptable source; pair it with at least a back-of-envelope estimate.
3. If you can't produce a number from one of those four sources, **move the candidate to "Not measured" with a one-line reason** — do not promote it to a finding.

### 5. Apply WMF-specific exclusions

Before finalising, re-check each finding against:

- Is the code path actually reached at WMF scale, or only in dev environments?
- Is the regression bigger than the existing variance on that path? Adding 1 ms to a 500 ms-p99 path is noise.
- Is there an existing cache / job queue / deferred update that already absorbs the cost? If you missed it in step 2, drop the finding.
- For frontend findings: does the module ship to production by default, or only behind a beta flag / user opt-in?

### 6. Write the report

Save to a Markdown file in the repo root (default: `<component>-perf.md`). Use exactly this structure:

```markdown
# <Component> Performance Review

**Target:** <path> (branch / version)
**Scope:** <backend | frontend | both>
**Reviewer:** Claude (perf audit)
**Date:** <ISO date>

## Methodology / Scope

<Threat model in 2-3 sentences. Which entry points were reviewed, what call
frequency they have in production, what guides the audit references.>

<Files reviewed in detail — bulleted list.>

## Summary

| # | Severity | Type | Title |
|---|----------|------|-------|
| 1 | P1 | DB | <one-line title> |
| 2 | ... | ... | ... |

<If no P0 / P1 findings, say so explicitly. Don't pad the table.>

## Findings

### [P1] 1 — <title>

- **Type:** <DB / cache / HTTP / compute / memory / hook / new-RL-module / RL-module-shape / CSS / JS / DOM / image>
- **Location:** `<path>:<line>` (and ranges)
- **Code:** <quoted excerpt>
- **What's wrong:** <which guideline rule this violates, with link to the guide
  section or budgeting page if available>
- **Measurement:** <the number — row count, ms, KB, hit rate — and how it was
  obtained (EXPLAIN output / DevTools trace / computed estimate)>
- **Production impact:** <what happens at WMF scale: SLO bucket affected,
  expected p50 / p99 movement, dollar / FPM-worker cost if calculable. For new
  RL modules, the rationale for why a new module is justified over extending
  an existing one.>
- **Recommendation:** <concrete fix with a code sketch; cite the cache layer /
  query shape / module shape to switch to>

(repeat for each finding)

## Not measured

<Candidates that looked concerning but you couldn't produce a number for. One
line each: what it is, why measurement wasn't feasible (no local repro, no
production signal, depends on data shape unknown to you). The engineer can
decide whether to chase any of these.>

## Reviewed without finding

<Bulleted notes on code paths examined and concluded clean. Documents audit
coverage so a follow-up review knows what's already been checked.>

## Recommendations beyond the findings

<Process-level: add a perf test, add a `mw.track('stats.*')` counter, add an
`EXPLAIN` check to CI, set up a Grafana panel. Note here if the patch is
sufficiently cross-cutting or hot-path that you'd recommend the engineer
request a review from a Principal engineer or other performance-expert
colleague before merge.>
```

## Hard rules

- **No padding.** One real finding means a one-row summary table. A report with seven "findings" of which six are guideline quotes without numbers is worse than a report with one measured finding.
- **No severity inflation.** A guideline violation without a measurement is P3 or "Not measured" — never P1. P0 / P1 require a number tied to a production-visible impact.
- **No "this might be slow" claims.** *Might* is what step 4 is for. Either produce the measurement or move the candidate to "Not measured".
- **No invented budgets.** Quote the Performance budgeting page or measure a delta. Do not assert "p99 should be under X ms" if X isn't on the page.
- **Every new RL module gets flagged.** The universal-tax rule fires regardless of how small the module is.
- **Verify the fix is actually faster.** When you propose a fix, sketch what the new measurement would be and why (smaller bundle, indexed query, deferred to job queue). A fix that swaps one guideline violation for another is not a fix.
- **Don't recommend caching as a default fix.** Cache is correct *when the cost of staleness is acceptable and the hit rate will be high*. A cache around a per-user, per-request call with TTL 60s adds complexity without speedup. Show the hit-rate model before recommending a cache.
- **Don't recommend removing existing caches** without checking what protects the path today.
- **No "rewrite in vanilla JS" / "rewrite without jQuery" findings** unless the rewrite produces a measured win. Style preferences from the guide are P3 at best.
- **Don't suggest workarounds that escape the nono sandbox.** If a path / domain is denied for measurement, surface that to the engineer.

## When to delegate to a sub-agent

If the component is large (> ~3000 LOC, or both backend + frontend) and the engineer wants a thorough sweep, delegate the enumeration phase (step 3) to the `mediawiki-explore` sub-agent with these rules baked into the prompt. Keep the measurement phase (step 4) in the main loop — measurements require interacting with the engineer's local wiki and judgement about which candidates are worth the cycles.

For small components (single module / single class / < ~1000 LOC), do it inline.

## When to escalate to a human reviewer

After the audit, recommend the engineer request a performance review from a Principal engineer or other performance-expert colleague when:

- The patch lands on a parser-hot or page-render-hot path.
- The patch adds a new RL module that will ship by default.
- The patch introduces a new DB index, schema change, or query against a large core table.
- The audit produced any P0 finding, or more than one P1.
- The patch is in a component without prior perf review (new extension going to prod, first WMF deploy of an upstream library).

Surface this recommendation in the "Recommendations beyond the findings" section of the report. The skill doesn't try to *be* a Principal-engineer review — it tries to make that review cheap and well-prepared.

## Common WMF pitfalls (from past audits)

- **`WANObjectCache::get` followed by `set`** is the race-prone pattern. The guideline says `getWithSetCallback` — it serializes regeneration and handles tombstoning. Manual get/set is a P2 by default.
- **`Database::select` inside a `foreach`** is the canonical N+1. Replace with one `IN ( ... )` query or `newSelectQueryBuilder()->where( [ 'col' => $ids ] )`.
- **`mw.loader.using()` inside a `mw.loader.using()`** serialises module loads. Combine the dependency lists.
- **`OutputPage::addInlineScript`** disables ResourceLoader caching for that snippet. Almost always a P2 unless the snippet *must* run before the next paint and is < ~100 bytes.
- **`ParserOutput::setExtensionData` of large payloads** bloats ParserCache. ParserCache is finite cluster-wide; large extensions ejecting other entries is a cache-stampede risk.
- **`Job::run` with synchronous HTTP fan-out** can saturate the runner. Use `MultiHttpClient` with concurrency or split into per-target jobs.
- **`addModules` for a module that contains only CSS** should be `addModuleStyles` — gets the CSS into the head without blocking on JS.
- **A new `extension.json` `ResourceModules` entry** in a patch that "just adds a button" is the universal-tax footgun — almost always the new code belongs in an existing module.

## Input

`$ARGUMENTS` — the component path or extension name, optionally followed by `backend`, `frontend`, or `both`. Defaults to scanning the current working directory. If scope is unspecified, ask the engineer once; don't default to "both" silently.
