---
description: Audit a Wikimedia microservice or standalone application (Node.js, Go, Python, Rust) for production-exploitable vulnerabilities. Use when the engineer wants a security review of a service repo (mediawiki-services-*, wikifunctions function-orchestrator/-evaluator/-schemata, or any standalone app), optionally with pending merge/change requests layered on top. Interactive — gathers base repo(s), then merge/change requests, then supporting docs — all as publicly accessible URLs — then performs the audit. Focused on what an attacker can actually do today, not theoretical bugs. Discriminates between bugs and findings; downgrades anything gated by a validator or trust boundary that hasn't been shown bypassed. Written for WMF but generalizable to any AI agent auditing a service.
argument-hint: "[repo-url ...] (optional — the skill prompts for repos, MRs/CRs, and docs if omitted)"
allowed-tools:
  - Read
  - Glob
  - Grep
  - Bash
---

# WMF Service & Application Vulnerability Audit

The engineer is a **Wikimedia Foundation Security engineer**. Reports are for WMF Security / SRE, who care about what an attacker can do in production right now — not academic bugs. A long list of theoretical findings is worse than one verified, exploitable finding. Severity is grounded in *what the attacker actually gets*, not what the code technically does.

This skill is the microservice / standalone-application counterpart to `vuln-audit` (which is tuned for MediaWiki core, extensions, and skins). Reach for **this** skill when the target is:

- a **`mediawiki-services-*`** repo (Node.js/TypeScript, Go, or Python) — e.g. citoid, mobileapps, cxserver, wikifeeds, change-propagation, eventstreams, restbase, kartotherian, chromium-render, push-notifications, kask, the `servicelib-*` / `service-template-*` scaffolds;
- a **Wikifunctions** component — `function-orchestrator`, `function-evaluator`, `function-schemata`, `wikilambda-cli` (GitLab, under `repos/abstract-wiki/wikifunctions/`);
- a **Liftwing / ML inference** service (Python: KServe/FastAPI model servers, `machinelearning-liftwing-inference-services`, `machinetranslation`);
- any other **stand-alone service or application** written primarily in Node.js, Go, Python, or Rust.

Most of the methodology below is service-agnostic. The WMF-specific gates (what's public by design, the trust boundaries, the Gerrit/GitLab fetch recipes) are called out as such — for a non-WMF target, substitute that service's own ingress topology and auth model.

---

## Phase 0 — Gather inputs (interactive)

Collect three things, in this order, **as publicly accessible URLs**. If the engineer already supplied some via `$ARGUMENTS` or in the request, skip the corresponding prompt and confirm what you parsed. Ask one focused question at a time; wait for the answer before moving on.

### 0.1 Base repository or repositories

> **"Which repository (or repositories) should I audit? Paste one or more public URLs (GitHub, GitLab, or Gerrit). If you want a specific branch or tag, include it."**

Accept and normalise these forms:

- **GitHub** — `https://github.com/wikimedia/mediawiki-services-citoid`
  `git clone --depth 1 [--branch <ref>] https://github.com/wikimedia/<name>.git`
- **GitLab (wikimedia)** — `https://gitlab.wikimedia.org/repos/abstract-wiki/wikifunctions/function-evaluator`
  `git clone --depth 1 --recurse-submodules [--branch <ref>] https://gitlab.wikimedia.org/<group>/<name>.git`
  (Wikifunctions evaluators pull interpreters via submodules — always `--recurse-submodules` here.)
- **Gerrit** — `https://gerrit.wikimedia.org/g/mediawiki/services/<name>` or a `/r/plugins/gitiles/...` browse URL
  `git clone --depth 1 https://gerrit.wikimedia.org/r/mediawiki/services/<name>`

Clone each into a scratch dir (e.g. `/tmp/audit/<name>`). Record for every repo: the exact **commit SHA** you cloned (`git -C <dir> rev-parse HEAD`) and the branch/tag. The report cites these.

If a clone is denied by the sandbox network policy (`x-deny-reason` in the failure), **surface that to the engineer** — do not try to route around it. The WMF nono sandbox scopes egress; if `github.com` or a repo host isn't reachable, that is a config change the engineer makes (contact an org owner), not something to work around.

### 0.2 Merge requests / change requests to layer on top

> **"Are there any pending merge requests (GitLab MRs) or change requests (Gerrit CRs / GitHub PRs) I should review on top of the base? Paste the URLs, or say 'none'."**

The audit must reflect the code **as it would be after these land**, and pay special attention to the changed lines. Fetch and apply each onto the cloned base:

- **Gerrit change** — URL like `https://gerrit.wikimedia.org/r/c/<project>/+/<number>` (optionally `/<patchset>`). Resolve the ref and fetch it:
  ```bash
  # discover the current revision ref (Gerrit prefixes JSON with )]}' — strip it)
  curl -sL "https://gerrit.wikimedia.org/r/changes/<number>/revisions/current/review" \
    | sed "s/^)]}'//" | python3 -c "import sys,json;print(json.load(sys.stdin)['ref'] if False else '')" 2>/dev/null
  # simplest reliable path — fetch the change ref directly and check it out on top of base:
  git -C <dir> fetch https://gerrit.wikimedia.org/r/<project> refs/changes/<NN>/<number>/<patchset>
  git -C <dir> checkout FETCH_HEAD        # to audit the change in isolation
  # or: git -C <dir> merge --no-ff FETCH_HEAD   # to audit base+change together
  ```
  `<NN>` is the last two digits of `<number>` (Gerrit's sharded ref layout, e.g. change `1306884` → `refs/changes/84/1306884/3`).
- **GitLab MR** — URL like `https://gitlab.wikimedia.org/<project>/-/merge_requests/<iid>`. Fetch the head ref, or grab the diff:
  ```bash
  git -C <dir> fetch origin "merge-requests/<iid>/head:mr-<iid>" && git -C <dir> checkout mr-<iid>
  # raw diff for a quick read: curl -sL "<mr-url>.diff"
  ```
- **GitHub PR** — URL like `https://github.com/<org>/<repo>/pull/<n>`:
  ```bash
  git -C <dir> fetch origin "pull/<n>/head:pr-<n>" && git -C <dir> checkout pr-<n>
  # raw diff: curl -sL "<pr-url>.diff"
  ```

For each MR/CR, capture the diff (`git -C <dir> diff <base-sha>..HEAD`) and keep it handy — Phase 3's review is anchored on what these changes touch and what new reachable bytes they introduce.

### 0.3 Additional documentation

> **"Any additional documentation I should read first? Design docs, API/OpenAPI specs, Phabricator tasks, threat models, deployment/AppArmor configs, on-wiki pages — paste public URLs, or say 'none'."**

Fetch each (`curl -sL <url>`, or the appropriate MCP/tool). Use docs to establish the **intended** trust model and reachability — but treat them as claims to verify against the code, not ground truth. If a doc says "input X is validated upstream," you still confirm the validator exists and fires before you rely on it (see Hard rules).

Also always read what's already in the tree: `README.md`, `CLAUDE.md` / `AGENTS.md`, `SECURITY.md` / `PERFORMANCE.md`, the OpenAPI/Swagger spec (`spec.yaml` / `openapi.*`), `Blubberfile` / `.pipeline/`, `config*.yaml`, `helm`/`deployment-charts` references, and dependency manifests.

Once all three inputs are gathered, confirm the plan back to the engineer in one or two lines (repos + SHAs, MRs/CRs, docs) and proceed.

---

## Phase 1 — Establish the threat model

### 1.1 What is this service, and who can reach it?

Reachability is the single most important question for a microservice, and it differs sharply from MediaWiki. Determine, from the code + deployment config + docs:

- **Language & runtime.** Node.js (usually `service-runner` + `express` + `swagger-router`), Go (often `servicelib-golang` middleware + net/http), Python (FastAPI / Flask / KServe / aiohttp), Rust (Wikifunctions evaluator). Read the manifest: `package.json`, `go.mod`, `requirements.txt` / `pyproject.toml`, `Cargo.toml`.
- **Ingress class.** Is this service reachable by an anonymous external attacker, or only from inside the cluster?
  - **Public** — exposed through the REST gateway / `api-gateway` / RESTBase / `restrouter` at `*.wikimedia.org/api/rest_v1/...` or `*.wikimedia.org/w/rest.php/...`. Ordinary anonymous users hit it.
  - **Internal-only** — reachable only from other services (e.g. an evaluator only the orchestrator calls; a changeprop consumer). The attacker cannot send it bytes directly. An "unauthenticated endpoint" on an internal-only service is **not** a finding on its own — the finding is a way for an external actor to *reach* it (a public service that proxies to it with attacker-controlled input, an SSRF that lands on it, etc.).
  - Check `deployment-charts` / helm values, the `Blubberfile`, `.pipeline/`, and any `x-route-filters` / networkpolicy references to decide. When unsure, ask the engineer rather than assuming.
- **Entry points.** For Node: the OpenAPI/Swagger `spec.yaml` and `routes/*.js` (swagger-router mounts them). For Go: the `http.Handler` / mux registration. For Python: FastAPI routers / KServe handlers / Flask `@app.route`. List every route, its method, its auth requirement, and the request fields it reads (path, query, headers, body).

If you can't articulate **"the public byte/parameter X reaches this code at function Y via route Z,"** you don't yet have a finding's worth of context.

### 1.2 Default attacker model (unless the engineer says otherwise)

- **Attacker:** an ordinary anonymous or registered user hitting the public ingress, plus anyone who can get the public tier to make a request on their behalf (SSRF, callbacks, webhooks). For Wikifunctions, also: **an author of a function/implementation whose code the evaluator will run.**
- **Goal:** RCE / sandbox escape; SSRF into the cluster or cloud metadata; unauthenticated exfil of secrets or gated PII; auth/authz bypass; a DoS primitive an ordinary request can fire that exhausts a production resource (event-loop, worker pool, memory, DB/Cassandra connections, WASM fuel); cache poisoning; stored/reflected XSS that lands in a browser context.
- **Out of scope** (unless the engineer escalates): WMF operators and cluster insiders; other trusted first-party services abusing an internal-only API they're authorised to call; Kafka producers/consumers; datalake/hive users.

---

## What is public / expected by design (NOT a finding)

Don't flag these no matter how dramatic the phrasing:

- **OpenAPI/Swagger exposure.** `GET /?spec` and `GET /?doc` (swagger-ui) return the spec by design. `/robots.txt` (Disallow: /), `/_info`, `/_info/name|version|home`, and health/readiness probes are meant to be public.
- **Permissive CORS on a read-only public API.** The `service-runner` default is `access-control-allow-origin: *`. On an endpoint that is read-only, unauthenticated, and returns only public content, `*` is intentional. It becomes a finding **only** when the same origin reflects credentials/cookies, or exposes per-user / gated data (see trust boundaries).
- **SSRF-shaped services doing their job.** citoid fetches arbitrary user-supplied URLs to build citations; chromium-render/proton renders arbitrary pages to PDF; cxserver and machinetranslation call MT backends. *Fetching an external URL is the product.* The finding is a **failure of the egress guard** — reaching `169.254.169.254`, cluster-internal hosts, `file://`/`gopher://`, redirect-based bypasses, DNS rebinding — not the fetch itself.
- **Public wiki content echoed back.** Page content, revisions, usernames, and public log data surfaced by a service are already public via the MediaWiki API. Re-serving them is not info-disclosure.
- **Service version / dependency list** in `/_info` or headers — analogous to `Special:Version`. Not a finding.

## What is gated / trust-boundary protected (flag with care)

Real findings, but only when an **unauthorised** actor can reach them:

- **Service-to-service trust.** Many services trust their caller (the orchestrator trusts nothing from the user but the evaluator trusts the orchestrator's framing; changeprop trusts the eventgate schema). Don't model an exploit that assumes the attacker already sits on the internal network — unless a public path lets them inject there.
- **Secrets:** API tokens, `session`/JWT signing keys, OAuth client secrets, DB/Cassandra/Redis creds, Kafka creds, cloud credentials — in config, env, image layers, or logs. Leaks are real findings.
- **Gated PII / restricted data:** CheckUser data, suppressed/deleted revisions, email addresses, `oathauth` secrets, IP-correlation data on temp-account wikis. If a service can surface these to an unauthorised caller, that's a finding.
- **Cloud metadata / cluster-internal services** reachable via SSRF (see above).

---

## Severity rubric

Use these brackets. If a finding won't fit cleanly, weaken or strengthen the claim until it does.

- **Critical** — pre-auth RCE or WASM/interpreter **sandbox escape**; SSRF that reaches cloud metadata or a secret-bearing internal service and exfiltrates; unauthenticated mass exfil of gated PII or secrets; cache poisoning of a shared public response.
- **High** — post-auth RCE; auth/authz bypass giving cross-account or cross-tenant access; SSRF to arbitrary internal hosts (no secret proven yet); stored XSS firing in a privileged UI; a DoS primitive an ordinary request fires repeatedly that exhausts a production resource (event-loop block, worker/connection-pool exhaustion, memory blow-up, unbounded WASM fuel/time); secret disclosure via error/log.
- **Medium** — SSRF gated to a narrow set of hosts; ReDoS / algorithmic blow-up bounded by an existing size cap; reflected XSS gated by content-type or social engineering; CSRF with a real state-changing effect; prototype pollution reaching a non-trivial sink; open redirect on an auth flow; missing egress allowlist where the fetch target is only partly attacker-controlled.
- **Low** — defence-in-depth gaps; output-encoding sloppiness in a context that can't currently reach a sink; missing input validation when an upstream layer already validates; verbose errors that leak only non-sensitive internals.
- **Info / Not currently exploitable** — code-level bugs with no working trigger (guarded branch, hardcoded caller argument, upstream schema validation, size cap). Real, worth a defensive note, but not a live finding.

---

## Phase 2 — Methodology (work in order; don't skip ahead)

### 2.1 Map the framework's built-in protections *before* enumerating bugs

Know what the scaffold already does, so you don't file a "missing header" that the framework sets, or miss that a default is dangerously loose.

- **`service-runner` / `service-template-node` (Node):** `app.js` sets CORS (`conf.cors`, default `*`), a default CSP, `x-content-type-options: nosniff`, `x-frame-options: SAMEORIGIN`, `x-xss-protection`, disables `x-powered-by`, and caps JSON bodies (`conf.max_body_size`, default `100kb`). Confirm each is present and not overridden loosely in `config.yaml`. The HTTP client is usually `preq`/`node-fetch`/`axios` — that's your SSRF surface. `domino`/`cheerio` parsing is your HTML-injection / XSS surface.
- **`servicelib-golang` (Go):** shared `middleware/` (request-id, logging, recovery) and `logger/`. Check whether panics are recovered (a panic in a handler without recovery is a DoS), and whether the logger redacts auth headers.
- **Python (FastAPI/Flask/KServe):** note whether request bodies are size-limited, whether Pydantic models actually constrain inputs (or use `extra = allow`), and whether the ASGI server (uvicorn/gunicorn) sits behind a size/time-limiting proxy.
- **Rust (axum / actix-web / warp / hyper on tokio):** confirm the request-body limit and that it hasn't been raised or removed (axum ships a `DefaultBodyLimit`, ~2 MB; actix uses `JsonConfig::limit` / `PayloadConfig` — verify the *configured* value, don't assume the default). Check whether a handler can `panic!` on attacker input — in an async server a panic unwinds the task (or aborts the process under `panic = "abort"`), so it's the Rust analogue of a missing Go `recover()`. Note whether the crate sets security headers itself or leans on the ingress proxy, and whether the crate root declares `#![forbid(unsafe_code)]` (a strong positive signal; its absence means the `unsafe` audit in 2.2 matters more).

### 2.2 Enumerate sinks

For each reachable entry point, classify what the code does with attacker-influenced input. Cover the cross-cutting service classes **and** the language-specific footguns.

**Cross-cutting (all languages):**
- **SSRF / outbound requests** — any fetch whose URL, host, port, or scheme is influenced by input. Trace redirects (does the client follow them past the allowlist check?), check for `file:`/`gopher:`/`ftp:` schemes, DNS-rebinding gaps (validate-then-connect races), and whether `169.254.169.254` / `metadata.google.internal` / RFC-1918 / `localhost` are blocked.
- **Injection** — OS command (`child_process.exec`, `os/exec`, `subprocess` with `shell=True`), SQL/CQL (Cassandra query concatenation), NoSQL, LDAP, template injection (Jinja2/Nunjucks/Go `text/template` used for HTML), header/response splitting, log injection.
- **Deserialization** — Python `pickle`/`yaml.load` (non-safe)/`marshal`/`joblib`/`torch.load` of untrusted model or request data; Node prototype pollution via `JSON.parse` merges / `lodash.merge` / `Object.assign` into `__proto__`; Go `gob`; unsafe `yaml`/`toml` in any language.
- **Path traversal / arbitrary file access** — user input in filesystem paths, archive extraction (zip/tar slip), template/model file loading, static file serving.
- **XML/XXE** — any XML parser with external entities enabled; SVG/DOCX/EPUB ingestion.
- **Resource exhaustion / DoS** — unbounded loops or recursion over user data; regex on user input (**ReDoS** — catastrophic backtracking); missing timeouts on outbound calls; decompression/zip bombs; huge image/PDF/SVG inputs; synchronous CPU work blocking the Node event loop; unbounded concurrency or memory.
- **AuthN/AuthZ** — JWT/OAuth validation (algorithm confusion, `none`, missing `aud`/`iss`/`exp` checks, signature not verified); session/token handling; missing authz on state-changing or data-returning routes; IDOR (object IDs not scoped to the caller); trusting `X-Forwarded-*` / client-supplied identity headers.
- **Crypto/TLS** — disabled cert validation (`rejectUnauthorized: false`, `InsecureSkipVerify: true`, `verify=False`), weak/hardcoded keys, predictable tokens, secrets in code/config/logs.
- **Cache poisoning** — responses cached by a key that omits an input that changes the body; unkeyed `Vary`; user input reflected into cached content.

**Node.js specifics:** prototype pollution (the big one), `eval`/`Function`/`vm` on input, `child_process` with interpolation, regex from user input, ReDoS in validation libs, SSRF via `preq`/`request`/`axios` redirect following, `express` `trust proxy` misconfig, unsafe `res.redirect` (open redirect), YAML/`js-yaml` `load` vs `safeLoad`, `domino`/`cheerio` output re-serialised into HTML without escaping.

**Go specifics:** missing `recover()` (panic → crash → DoS), `text/template` where `html/template` is required (XSS), `exec.Command` with a shell, SSRF via `http.Client` with no `CheckRedirect` guard, integer/slice bounds, `math/rand` for tokens, goroutine leaks / unbounded goroutine spawn, missing request-body/`Server` timeouts (`ReadTimeout`/`ReadHeaderTimeout`), `filepath.Join` on user input without containment.

**Python specifics:** `pickle`/`yaml.load`/`torch.load`/`joblib.load` on untrusted data (**RCE**), `subprocess(..., shell=True)`, `os.system`, `eval`/`exec`, Jinja2 SSTI, `format`-string injection, `requests(..., verify=False)`, XXE via `lxml`/`xml.etree` default parsers, Pydantic models with `extra=allow` or missing constraints, unbounded model input causing OOM (Liftwing), ReDoS.

**Rust specifics:** Rust is memory-safe *by default*, so the profile differs — the highest-value targets are `unsafe`, panics, and unbounded allocation, not use-after-free everywhere. Look for:
- **`unsafe` reachable from input.** Audit every `unsafe` block that attacker-influenced data can reach: raw-pointer deref, `slice::get_unchecked`, `Vec::set_len`, `MaybeUninit`, and especially `std::mem::transmute` (type confusion). A soundness bug in `unsafe` driven by request bytes is memory corruption — potentially RCE, so it outranks most web-layer nits. FFI (`extern "C"`, `bindgen`, a linked C/C++ lib) is the other unsafe boundary: the memory-safety guarantee stops at the call, and length/lifetime assumptions are yours to prove.
- **Panics = DoS.** `unwrap()`, `expect()`, `panic!`, `unreachable!`, `todo!`, slice/array indexing `x[i]` (panics on out-of-bounds), and `n / 0` all abort the current task/thread on attacker input; under `panic = "abort"` they take the process down. Any `unwrap()` on a value derived from a request is a candidate DoS primitive — the direct analogue of a Go handler with no `recover()`.
- **Silent integer overflow in release.** Overflow checks are **on in debug, off in release**, where arithmetic *wraps* silently. A wrapped length/size/index (e.g. computing a buffer size or offset from a user field) can cause under-allocation or logic errors that a debug run won't reveal. Expect `checked_*` / `saturating_*` / `try_into()` around any size math on input; flag bare `+`/`*`/`as` casts on attacker-controlled integers.
- **`serde` deserialization of untrusted input.** Deeply nested JSON/CBOR can blow the stack (`serde_json` bounds recursion, but hand-written `Deserialize` impls and other formats may not); `bincode` / `rmp-serde` / `postcard` will happily pre-allocate from an attacker-supplied length field (memory-exhaustion DoS). Check for `#[serde(deny_unknown_fields)]` where the type is a security boundary, untagged/`#[serde(flatten)]` enums on untrusted data, and any length-prefixed binary format decoded before a size cap.
- **SSRF via `reqwest` / `hyper` / `ureq`.** `reqwest`'s default redirect policy *follows up to 10 hops*, so the same validate-only-URL-#1 allowlist bypass as Node/Go applies — look for `redirect::Policy::none()` or a custom policy that re-checks each hop, plus post-DNS-resolution blocking of link-local/RFC-1918/metadata IPs.
- **Injection.** SQL via `sqlx`/`diesel`/`tokio-postgres` is parameterized *when you use the query macros or bind params* — a raw `sqlx::query(&format!("… {user} …"))` is injection. Command execution via `std::process::Command` does **not** invoke a shell by default (args are passed directly, which is safer), so the finding is the reintroduced shell: `Command::new("sh").arg("-c").arg(user_input)` or piping input through `bash`.
- **Path traversal, Rust-flavoured.** `Path::join` **replaces** the base when the argument is absolute (`base.join("/etc/passwd")` == `/etc/passwd`, not nested), and `..` components aren't normalized away — so user-controlled path segments need `canonicalize()` + a prefix check, not just a leading-slash test.
- **Disabled TLS verification.** `reqwest`'s `danger_accept_invalid_certs(true)` / `danger_accept_invalid_hostnames(true)`, or a `rustls`/`native-tls` `ServerCertVerifier` that accepts everything (the `dangerous()` APIs). Prefer `OsRng`/`getrandom` for tokens; `rand::thread_rng()` is CSPRNG-backed but confirm nothing security-sensitive uses a seedable/`SmallRng` path.
- **Blocking the async executor.** Synchronous work inside an `async fn` on tokio — `std::fs`, `std::thread::sleep`, `Mutex` held across `.await`, or CPU-bound loops without `spawn_blocking` — starves the runtime and is a DoS in the same shape as blocking the Node event loop. Also note `std::sync::Mutex` **poisons on panic**: one panicking handler that held the lock makes every later `.lock().unwrap()` panic, turning a single crash into a cascading outage.
- **Secret leakage.** `#[derive(Debug)]` on a struct holding a key/token, then `tracing`/`log`/`dbg!` printing it, leaks it to logs. Look for `secrecy`/`zeroize` on key material and for `Display`/`Debug` impls that don't redact.
- **What *not* to reflexively flag:** the `regex` crate is guaranteed linear-time (no catastrophic backtracking), so classic ReDoS usually does **not** apply — unless the service uses `fancy-regex` (backtracking) or compiles an attacker-supplied pattern without `RegexBuilder::size_limit`/`dfa_size_limit`. Rust's ownership model also rules out most data races, so don't invent memory-safety findings in safe code; concentrate on the `unsafe`, panic, overflow, and allocation surfaces above.

### 2.3 Overlay the merge/change requests

Now re-read the diffs captured in Phase 0.2 with the sink map in hand:

- For every changed hunk, ask: does it **introduce a new reachable byte**, **remove/weaken a validator**, **add a new sink**, or **change a trust assumption**? A three-line diff that deletes an allowlist check is a bigger deal than 500 lines of new tests.
- Check the change *in context* — a call that's safe in the base may become reachable because the MR wires a new route to it, and vice-versa.
- Note any new dependencies the MR adds (`package.json`/`go.mod`/`requirements.txt` deltas) and screen them (2.4).

### 2.4 Screen dependencies (supply chain)

Enumerate direct + transitive deps and check for known-vulnerable versions and suspicious packages. Where the toolchain is available in-sandbox: `npm audit --omit=dev`, `go list -json -deps ./... | ...` / `govulncheck ./...`, `pip-audit` / `safety`, and for Rust `cargo audit` (RustSec advisory DB) / `cargo deny check` (advisories + license + banned/duplicate crates), with `cargo geiger` to quantify the `unsafe` surface. Confirm a committed `Cargo.lock` and flag yanked crates. If the tools aren't available or the network is scoped, list the manifest versions and flag anything obviously stale/pinned to a known-CVE release, but don't invent CVE IDs — say "verify against the advisory DB."

### 2.5 Build a working trigger before filing

This is the discipline that separates a finding from a code-review note. For each candidate:

1. Write a concrete attacker workflow: "anonymous client sends `<METHOD> <route>` with `<fields>`, and observes `<effect>`."
2. Confirm every field is unblocked at the ingress (route exists, method allowed, field read, not gated by auth the attacker lacks, not hardcoded by an internal caller).
3. Confirm every byte survives the framework's validators (OpenAPI param schema, Pydantic model, size cap, schema validation) — demonstrate the bypass, don't assume it.
4. **Where feasible, run it.** Many WMF services run locally via `npm start` / `service-runner`, `go run`/`go test`, `pytest`, `cargo run`/`cargo test` (and `cargo clippy` surfaces security-relevant lints for free), or the documented Docker image (`service-runner docker-start`, or the published evaluator images for Wikifunctions). Start the service and fire real requests with `curl`, then observe the effect. In the WMF nono sandbox, plain `bin/claude` can't reach localhost web ports — the engineer opens them per-invocation with `bin/claude --local-web` (or `--chrome` for the chrome-devtools MCP, needed for anything browser-side: CORS, cookies, SameSite, XSS landing). If those aren't available, say so and fall back to precise static tracing.
5. If you can't trigger it or construct a concrete path, **move it to "Not currently exploitable" with a one-line reason.** Do not promote it to a finding.

For **DoS** candidates specifically, measure: show the input size / request rate and the resource it exhausts (event-loop stall in ms, memory, worker count, WASM fuel/time), and check it isn't already bounded by an existing cap.

### 2.6 Apply WMF exclusions

Re-run every surviving finding through the "public / expected by design" and "gated by trust boundary" filters above. Drop or reclassify documented behaviour and anything only an insider/authorised caller can reach.

---

## Wikifunctions addendum (function-evaluator / -orchestrator / -schemata)

The evaluator **executes user-written code** (Python via RustPython, JavaScript via QuickJS) inside a `wasmtime` **WASM sandbox**, in production under Kubernetes + AppArmor with disk and network denied. Security explicitly outranks performance here. Adjust the model:

- **Primary attacker = a function/implementation author** whose code the evaluator runs. The whole point is to run hostile code safely, so the threat model is **sandbox escape and resource abuse**, not "the input is untrusted" (it always is).
- **Sandbox-escape surface:** the host↔guest boundary — WASI imports, host functions exposed to the guest, `wasmtime` config (is `fuel`/epoch-interruption enabled and enforced? memory limits via a `ResourceLimiter`? are filesystem/network capabilities truly withheld?), interpreter-specific escapes (RustPython/QuickJS bugs reachable from user code), and anything that lets guest bytes influence host-side parsing/serialisation. The host is **Rust**, so apply the Rust specifics from 2.2 to it directly: any `unsafe` host function, `unwrap()` on guest-derived data (DoS), or `serde` decode of a guest-produced result is part of this boundary.
- **Resource exhaustion:** missing or too-high fuel/time/memory bounds; output-size limits (a function returning a giant Z-object); recursion/stack; concurrency at the orchestrator level.
- **Validation boundary (`function-schemata`):** ZObject (Z1..Z-types) validation — can a malformed or type-confused Z-object bypass a check, cause the orchestrator to mis-dispatch, or reach the evaluator in a state it doesn't expect? Check both orchestrator-side and evaluator-side validation; don't assume the other side validated.
- **Orchestrator↔evaluator trust boundary:** the orchestrator is the evaluator's only legitimate caller. A finding needs a *user-reachable* path (via the public function-call API through MediaWiki/WikiLambda) to inject the malicious framing — not an assumption that the attacker can call the evaluator directly.
- Read `PERFORMANCE.md` / `SECURITY.md` and the `fuzz/` harnesses first — they document the intended model and where the maintainers already look.

---

## Phase 3 — Write the report

Save to Markdown in the repo root (default `<service>-vulns.md`; for multi-repo audits, one file per repo or a combined file with per-repo sections). Use exactly this structure:

```markdown
# <Service> Security Review

**Target:** <repo URL> @ <commit SHA> (branch/tag)
**Changes reviewed:** <MR/CR URLs, or "none — base only">
**Docs consulted:** <URLs, or "none">
**Reviewer:** Claude (production-focused service audit)
**Date:** <ISO date>

## Methodology / Scope

<Threat model in 2-3 sentences: who the attacker is, which ingress they reach
(public via REST gateway vs internal-only), what they're trying to achieve.
State what's out of scope (operators, trusted first-party callers, insider network access).>

<Files / routes reviewed in detail — bulleted list. Note verification level:
which findings were run live vs traced statically, and why.>

## Summary

| # | Severity | Type | Title |
|---|----------|------|-------|
| 1 | High | SSRF | <one-line title> |
| 2 | ... | ... | ... |

<If no Critical/High findings, say so explicitly. Don't pad the table.>

## Findings

### [HIGH] 1 — <title>

- **Type:** <SSRF / RCE / sandbox escape / authz bypass / DoS / deserialization / prototype pollution / ...>
- **Location:** `<path>:<line>` (and ranges)
- **Introduced/affected by:** <base, or MR/CR #N if the change is what makes it live>
- **Code:** <short quoted excerpt>
- **Description:** <what the bug is, in the code>
- **Trigger / impact:** <concrete attacker workflow: exact request(s) → measurable effect>
- **Why it's real, not theoretical:** <one paragraph: every field unblocked at the
  ingress, every byte past the validators, the observed effect — with file:line refs
  and, where run, the command + result>
- **Recommendation:** <concrete fix with a code sketch>

(repeat per finding, ordered by severity)

## Not currently exploitable

<Real code-level bugs with no working trigger today. Per bug: title, location,
what the bug is, why it's not reachable now (cite the validator / cap / gate with
file:line), and the defensive fix worth applying anyway.>

## Reviewed without finding

<Bulleted notes on the code paths you examined and cleared — documents audit
coverage so a follow-up review sees what was already checked.>

## Dependencies

<Notable dependency findings, or "screened, nothing actionable". Cite versions;
point to the advisory DB rather than inventing CVE IDs.>

## Recommendations beyond the findings

<Process-level: fuzzing, egress allowlists, size/time caps, dependency pinning,
authz middleware, sandbox-config hardening, etc.>
```

---

## Hard rules

- **No padding.** One real finding → one row. Seven "findings" of which six are theoretical waste Security / SRE time and erode trust in the reviewer.
- **No severity inflation.** A bug that needs an unconfirmed validator/sandbox bypass is not Medium — it's "Not currently exploitable" until the bypass is demonstrated.
- **Verify the defense, not just its presence.** When you claim a mitigation blocks an attack (the egress allowlist stops the SSRF, the CORS config isn't credentialed, the schema rejects the payload, fuel limits stop the loop), demonstrate it: run the trigger and observe success, apply/enable the mitigation, re-run and observe failure. A code-reading argument is one round short of a real finding. The "follow the framework convention" reflex is *especially* prone to this — `service-runner`, KServe, and `net/http` all have load-bearing defaults that may not hold in your threat model.
- **Name the ingress.** "An attacker who controls `X`" → name the route and field that takes `X` from a request. For an internal-only service, name the *public* path that reaches it. No named ingress → drop the finding.
- **Establish reachability before severity.** An unauthenticated endpoint on an internal-only service is not a finding by itself. A permissive CORS `*` on a read-only public API is not a finding by itself.
- **Don't flag public/expected-by-design behaviour** — swagger spec, `/_info`, robots.txt, SSRF-shaped services fetching URLs (flag the guard failure, not the fetch), re-served public wiki content.
- **No findings whose remediation is "operators must not misconfigure X."** That's not an attacker capability.
- **Don't invent CVEs or advisories.** Cite versions and point to the advisory DB; verify before asserting a package is vulnerable.
- **Don't suggest workarounds that escape the sandbox.** If a domain/path/port is denied, surface it to the engineer (it's a network-policy change), don't route around it.
- **Reflect the reviewed state honestly.** Report the exact SHA and which MRs/CRs were layered on; distinguish base findings from change-introduced ones.

## When to delegate to a sub-agent

If the target is large (multiple repos, multiple languages, or > ~3000 LOC) and the engineer wants a thorough sweep, delegate the **enumeration** phase (2.1–2.4) to the `agent-skills:security-auditor` sub-agent with these rules baked into the prompt. Keep the **verification** phase (2.5 — building and running triggers) in the main loop: it needs judgement about which candidates are worth chasing and interaction with the running service / engineer's environment.

For small single-repo, single-language targets (< ~1000 LOC), do it inline.

## Common WMF service pitfalls (from the ecosystem)

- **`service-runner` CORS defaults to `*`.** Fine for read-only public content; a finding only when credentials or gated data ride the same origin. Don't reflexively flag `*`.
- **The framework sets security headers for you** (`x-content-type-options`, `x-frame-options`, CSP). Don't file "missing header X" without confirming it isn't already set in `app.js` and not overridden in `config.yaml`.
- **SSRF-by-design services** (citoid, chromium-render/proton, cxserver, machinetranslation) — the finding is a *guard bypass* (metadata IP, internal host, redirect, scheme, DNS rebinding), never the existence of the fetch.
- **`preq`/`axios`/`http.Client` redirect following** frequently defeats a validate-the-first-URL allowlist. Check the redirect handler, not just the initial URL check.
- **Internal-only vs public** is decided by `deployment-charts`/helm/networkpolicy, not by the presence of auth in the code. Confirm before scoping.
- **Python ML services (Liftwing):** untrusted `pickle`/`torch.load`/`joblib` is RCE; unbounded model input is OOM DoS. These outrank most web-layer nits.
- **Wikifunctions:** the target is sandbox escape and fuel/memory/time bounds, not "input is untrusted." A finding needs a user-reachable path through the orchestrator; don't assume direct evaluator access.
- **Go handlers without `recover()`** turn a single crafted request into a crash-loop DoS; missing `ReadHeaderTimeout` is a slowloris primitive.
- **Rust:** don't reflexively file ReDoS (the `regex` crate is linear-time) or memory-safety bugs in *safe* code — the real Rust surfaces are `unwrap()`/indexing panics (DoS), silent integer wrap in release builds, unbounded `serde`/`bincode` allocation, and every `unsafe`/FFI block reachable from input. The Wikifunctions evaluator *host* is Rust, so its host↔guest boundary (wasmtime `Store`/`ResourceLimiter` config, `unsafe` host functions exposed to the guest) is where a sandbox escape would live.

## Input recap

`$ARGUMENTS` may pre-supply one or more base repo URLs; if present, skip the Phase-0.1 prompt and confirm what you parsed, then still prompt for MRs/CRs (0.2) and docs (0.3) unless those were supplied too. If `$ARGUMENTS` is empty, run the full Phase-0 interview. Default threat model: "ordinary anonymous or registered user on the public ingress" (plus "function author" for Wikifunctions) unless the engineer specifies otherwise.
