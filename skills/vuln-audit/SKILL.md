---
description: Audit a Wikimedia component (extension, library, service) for production-exploitable vulnerabilities. Use when the engineer wants a security review of WMF code (CTF challenges, hardening passes, pre-deploy reviews) — focused on what an attacker can actually do today, not theoretical bugs. Discriminates between bugs and findings; downgrades anything that needs an upstream-validation bypass that hasn't been demonstrated.
argument-hint: "[component-path or extension-name]"
allowed-tools:
  - Read
  - Glob
  - Grep
  - Bash
---

# WMF Production-Focused Vulnerability Audit

The engineer is a **Wikimedia Foundation Security engineer**. Reports are for WMF Security / SRE, who care about what an attacker can do in production right now — not academic bugs. A long list of theoretical findings is worse than one verified, exploitable finding. Severity is grounded in *what the attacker actually gets*, not what the code technically does.

## Target threat model (default unless the engineer says otherwise)

- **Attacker:** an ordinary anonymous or registered Wikimedia user (no special permissions). For some components, also consider a sysop, bot operator, or interface-admin, but only if the engineer asks.
- **Reachable surface:** anything the attacker can invoke from a public ingress — the edit UI, the action / REST APIs, page rendering, search, login, file upload, JS-loaded resources, the diff view, special pages, the OAuth flow, etc.
- **Goal of the attacker:** RCE, unauthenticated data exfil, cross-account compromise, CSRF/XSS that lands cookies, working DoS primitive on PHP-FPM workers, abuse-tool bypass.
- **Out-of-scope adversaries** (unless the engineer says otherwise): WMF operators, WMF cluster insiders, Kafka producers/consumers, datalake users with hive access.

## What is public by design (NOT a finding)

These are public on every wiki by default. Don't flag them as info-disclosure no matter how dramatic the phrasing.

- **Usernames** (registered and IP). `Special:ListUsers`, `Special:Log`, page history, RecentChanges, and the user API all expose them. Username-existence oracles in login/signup are not findings.
- **Page content, page history, edit metadata.** Every revision (oldid, parent, timestamp, summary, actor) is queryable via the API. Even on private wikis, "public-by-design" is the per-wiki default.
- **Config var names in error messages.** `$wgFooBar` strings are public — they're documented on mediawiki.org. Mentioning a config key in a stack trace is not info-disclosure.
- **MediaWiki version, extension list, skin list.** Exposed via `Special:Version` and the siteinfo API. Don't flag.
- **Cross-origin POSTs to `intake-analytics`** and query-param schema/stream selection — these are by design.
- **Log data already published on `Special:Log`** (delete, block, rights, etc.) being repeated elsewhere (firehose, recentchanges feed) is friction-reduction, not data exposure. The (page, actor) pair was always public.

## What is gated / trust-boundary protected (flagging needs care)

These ARE worth findings, but only when an unauthorised actor can reach them. Operator/insider misuse is out of scope unless the engineer escalates.

- **Kafka topics, EventStreams internals.** Only trusted operators read/produce. Don't model exploit chains that assume Kafka consumer rights.
- **`$wgSecretKey`, session tokens, CSRF tokens, OAuth client secrets.** Leaks of these are real findings.
- **Replica PII** (CheckUser data, suppressed/deleted revisions, email addresses, `oathauth` secrets, abuse-filter private filters).
- **IP-correlation data on wikis with temp accounts enabled** (ru.wikipedia, etc.). Reframe IP-leak findings as registered-editor / temp-correlation issues if temp accounts are on.
- **Private wikis** (officewiki, etc.). The default `view` permission is restricted; anonymous attackers can't read.

## Severity rubric

Use these brackets. If you can't fit a finding cleanly, you probably need to weaken or strengthen the claim until it fits.

- **Critical** — pre-auth RCE; unauthenticated mass data exfil of gated PII; cluster-wide compromise.
- **High** — post-auth RCE; cross-account takeover; stored XSS that fires on a high-privilege page (admin tools, login); a working DoS primitive an ordinary user can fire repeatedly that exhausts a production resource (PHP-FPM workers, DB connections, mediawiki-config).
- **Medium** — real CSRF with cookie effect; confused-deputy with measurable impact; reflected XSS gated by content type or social-engineering; algorithmic complexity that hurts but is bounded by existing limits; bypass of an abuse tool (AbuseFilter, CheckUser block, rate limit) under realistic conditions.
- **Low** — defence-in-depth gaps; output-encoding sloppiness in contexts that can't currently land in a sink; missing input validation when an upstream layer already validates.
- **Info / Not currently exploitable** — code-level bugs (UB, signed overflow, dead branches) without a working trigger; theoretical paths gated by `$wgMaxArticleSize`, hardcoded caller arguments, or upstream sanitisation.

## Methodology

Work the steps in order. Don't skip ahead.

### 1. Establish what `$ARGUMENTS` actually is

Open the component. Identify:
- The language and runtime (PHP, C++ PHP extension, JS, Python service, etc.).
- The entry points reachable from a public Wikimedia ingress. Where is this called from? `grep` core/extensions for callers. The README/extension.json/composer.json/setup.py is the starting point.

If you can't articulate **"the public byte/parameter X reaches this code at function Y via path Z,"** you don't yet have a finding's worth of context.

### 2. Map upstream validators

Before you call any C-level / PHP-level bug "reachable", check what MediaWiki does to the input before it gets here. Common validators that *change the reachable byte set*:

- **UTF-8 validation on save.** `Content::isValid`, `TextContent::isValid`, `EditPage::internalAttemptSave`, `ApiEditPage::execute`, `mb_check_encoding($text, 'UTF-8')`. Stored revision text is well-formed UTF-8. Don't claim a "raw byte X in a saved revision triggers Y" finding without showing how X survives this. Test with the API and confirm the bytes round-trip.
- **`$wgMaxArticleSize`** (default 2 MiB on WMF). Inputs above this are rejected at save time.
- **Title validation.** `Title::newFromText` rejects bytes that don't form valid titles. Page-name attacks have a narrow reachable surface.
- **Wikitext sanitisation in the parser.** Most attacker-controlled bytes go through `Parser` / `Sanitizer` before HTML output. The output side has its own validators (`Sanitizer::removeHTMLgarbage`, `Sanitizer::escapeHtmlAllowEntities`, attribute whitelisting).
- **HTML construction** via `Html::element`, `Xml::element`, `OutputPage::addHTML`. These escape correctly when used. Findings exist when the caller string-concatenates around them, not when they're used.
- **Type / length checks** in the action API (`ApiBase::PARAM_TYPE`, `PARAM_MAX`).
- **`Title::isValid`, `User::isUsableName`, rate limits in `User::pingLimiter`**.

For C/C++ extensions called from MediaWiki (wikidiff2, luasandbox, wikidiff2, etc.), the relevant question is: *which MediaWiki caller passes user-controlled bytes to this entry point, and what does the caller sanitise first?*

### 3. Enumerate sinks in the component

For each entry point, classify what the code does to the input:
- **Memory-safety sinks** (C / C++): buffer indexing, pointer arithmetic, integer math used for sizes, allocator calls, recursion depth, `memcpy`/`memmove` with attacker-influenced length.
- **Complexity sinks**: nested loops over user data, regex backtracking, recursive algorithms without depth caps, joins without indexes, anything `O(N²)` over attacker-sized N.
- **Output sinks**: HTML/JSON/SQL construction. Look for string concatenation, `printf`-style formatting, `echo`, manual escaping, JSON encoders that don't validate UTF-8.
- **Authentication / authorisation sinks**: any branch on `$user`, `$session`, `getPermissionManager()`, cookie parsing, token generation/validation.
- **Side-effect sinks**: filesystem writes, network calls (especially attacker-controllable destination), shelling out, `eval`, `unserialize`, dynamic class loading.

### 4. For each candidate, build a working trigger before filing

This is the discipline that separates a finding from a code review note. For each candidate vulnerability:

1. Write a concrete attacker workflow: "anonymous user POSTs to `<API endpoint>` with parameters `<X, Y, Z>`, then GETs `<URL>`, and observes `<effect>`."
2. Confirm every parameter is unblocked at the ingress. If the parameter is `numContextLines` and it's hardcoded in `DifferenceEngine`, the finding is dead unless you find a different ingress that takes it.
3. Confirm every byte makes it past upstream validators. If the trigger requires invalid UTF-8 in stored revision text, demonstrate that MediaWiki saves invalid UTF-8 — don't assume.
4. Where feasible, run the trigger against the engineer's local wiki (per `CLAUDE.md`'s "Local development" section if present). Confirm the observable effect actually happens.
5. If the trigger fails or you can't construct one, **move the candidate to "Not currently exploitable" with a one-line reason** — do not promote it to a finding.

### 5. Apply WMF-specific exclusions

Before finalising, re-run each finding through the "public by design" and "gated by trust boundary" filters above. Drop or re-classify anything that's actually documented behaviour.

### 6. Write the report

Save to a Markdown file in the repo root (default: `<component>-vulns.md`). Use exactly this structure:

```markdown
# <Component> Security Review

**Target:** <path> (branch / version)
**Reviewer:** Claude (CTF-style audit)
**Date:** <ISO date>

## Methodology / Scope

<Threat model in 2-3 sentences. Who is the attacker, what ingresses they can reach,
what they're trying to achieve. Note that anything requiring operator / Kafka /
private-wiki access is out of scope.>

<Files reviewed in detail — bulleted list.>

## Summary

| # | Severity | Type | Title |
|---|----------|------|-------|
| 1 | High | <type> | <one-line title> |
| 2 | ... | ... | ... |

<If no Critical / High findings, say so explicitly. Don't pad the table.>

## Findings

### [HIGH] 1 — <title>

- **Type:** <category>
- **Location:** `<path>:<line>` (and ranges)
- **Code:** <quoted excerpt>
- **Description:** <what the bug is, in the code>
- **Trigger / impact:** <step-by-step attacker workflow ending in a measurable effect>
- **Why it's real, not theoretical:** <one paragraph confirming the trigger passes
  every upstream gate, with file:line references>
- **Recommendation:** <concrete fix with a code sketch>

(repeat for each finding)

## Not currently exploitable

<Real code-level bugs with no working trigger today. One subsection per bug:
title, location, what the bug is, why it's not reachable from a Wikimedia
ingress today (cite the upstream validator and its file:line), and the defensive
fix worth applying anyway.>

## Reviewed without finding

<Bulleted notes covering the code paths you looked at and concluded were
clean. This documents audit coverage so a follow-up review can see what was
already checked.>

## Recommendations beyond the findings

<Process-level recommendations: fuzzing, sanitiser builds, MediaWiki-side
defence-in-depth caps, etc.>
```

## Hard rules

- **No padding.** If you only have one real finding, the summary table has one row. Headline reports with seven "findings" of which six are theoretical waste Security / SRE time and erode trust in the reviewer.
- **No severity inflation.** A bug that needs an unconfirmed upstream-validation bypass is not Medium. It's "Not currently exploitable" until the bypass is demonstrated.
- **No "raw bytes in saved content" claims without a demonstrated save path.** MediaWiki's UTF-8 validation is the default. Show it bypassed or downgrade the finding.
- **No "user could control X" claims without naming the ingress.** "An attacker who controls `numContextLines`" → name the endpoint that takes `numContextLines` as a request parameter. If none, drop the finding.
- **No flagging public-by-design facts.** Username existence, config var names in errors, public log data, version / extension list — not findings.
- **No findings whose remediation is "operator must not misconfigure X".** That's not an attacker capability.
- **Don't suggest workarounds that escape the nono sandbox.** If a path / domain is denied, surface that to the engineer.

## When to delegate to a sub-agent

If the component is large (multiple files, multiple languages, or > ~3000 LOC) and the engineer wants a thorough sweep, delegate the enumeration phase to the `agent-skills:security-auditor` sub-agent with these rules baked into the prompt. Keep the verification phase (step 4 — building working triggers) in the main loop, since that requires interacting with the engineer's local wiki and judgement about which candidates are worth chasing.

For small components (single file, single language, < ~1000 LOC), do it inline.

## Common WMF pitfalls (from past audits)

- Wikidiff2: complexity caps are off by default on the line-level diff. Word-level cap exists. UTF-8 decoder is weak but MediaWiki's save-side validation neutralises it in practice.
- Hooks.php fallback sampling rates in EventLogging instrumentation are **not** the production rate. Production rate lives in `mediawiki-config/wmf-config/InitialiseSettings.php`. Quote that, not the Hooks.php fallback.
- Client-side `mw.track('stats.*')` names must end in `_total`, `_seconds`, or `_distribution` (WikimediaEvents throws TypeError otherwise — that's a footgun for instrumentation, not a security finding).
- ConfirmEdit sub-extensions are being slimmed but their loads are still required for VE / UI attributes. Don't propose removing them.
- For wikis with temp accounts enabled (ru.wikipedia, etc.), reframe IP-leak findings as registered-editor / temp-correlation issues, not anonymous-IP issues.

## Input

`$ARGUMENTS` — the component path or extension name to audit. Default to scanning the current working directory if empty. If the engineer didn't specify a threat model, default to "ordinary anonymous or registered Wikimedia user".
