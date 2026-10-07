# Implementation and verification report

Date: 7 October 2026

## Starting point and result

The repository began with one README and no application. This update implements a usable local review/remediation system, with ZIP/public-GitHub intake, a responsive interface, real LangGraph orchestration, mandatory PostgreSQL/pgvector persistence, bounded execution, guarded edits, and detailed reports. Submitted repositories are edited only in isolated copies; the result is a patch for the user to review.

## Changes and rationale

| Area | Action | Rationale |
| --- | --- | --- |
| Orchestration | Scan, select, propose, validate, and report nodes with conditional retry transitions | Make decisions explicit, finite, and testable |
| Detection | Isolated Ruff rules and custom AST checks | Detect fragile implementations and debt without executing uploaded code |
| Remediation | Conservative textual fixes plus optional structured model replacements | Demonstrate autonomous improvements while requiring stronger evidence for substantive edits |
| Validation | Syntax checks, baseline comparison, targeted finding removal, and required sandbox tests for model edits | Reject new static problems and behavior regressions; accurately disclose coverage |
| Rollback | Restore rejected candidate bytes in a finally block | Keep failed and interrupted attempts from becoming accepted output |
| Stop policy | Per-finding/global attempt limits, provider-call limit, graph limit, timeouts, duplicate/no-progress stops | Prevent infinite retries and bound resource consumption |
| Source intake | Safe streamed ZIP extraction and public GitHub downloads pinned to an immutable commit SHA | Prevent path escapes, archive bombs, ambient credential use, and ambiguous provenance |
| Context | Run-scoped lexical vectors in a required pgvector column | Make retrieval useful locally without an external embedding service or cross-run source leakage |
| Storage | Durable job queue, progress, JSONB reports, atomic claims/capacity, advisory worker ownership | Avoid duplicate processing and preserve an audit trail across restarts |
| UI | Source selection, progress, recent runs, findings/actions, evidence, limitations, authenticated downloads | Give users a clear explanation of what changed and why |
| Standards | Version lock, lint/format settings, integration tests, CI, Docker setup, architecture/security/contribution documents | Make the project reproducible and reviewable |

## Initial implementation verification

- Initial implementation suite: **143 passed, 4 skipped**, with **89% application statement coverage**. These are historical counts; the current update results appear below.
- Actual PostgreSQL 15 with pgvector 0.8.2: extension initialization, vector retrieval, run isolation, queue capacity, concurrent job claims, worker ownership, and persisted reports verified.
- End-to-end API: a submitted ZIP produced an accepted deterministic change, persisted its report, and served both report and patch downloads.
- Browser demonstration: `fragile-python.zip` produced one statically validated improvement and retained the mutable-default finding as unresolved. The interface showed the actual evidence and limitations; patch download completed successfully.
- Live public GitHub intake: `pallets/itsdangerous` was fetched at commit `672971d66a2ef9f85151e53283113f33d642dabd`. It ingested 50 files, analyzed 15 Python files, and reported 11 review findings without forcing unsupported edits.
- Ruff lint, Ruff formatting, JavaScript syntax, dependency consistency, and Git whitespace validation passed.
- Guardrail coverage includes traversal/link/collision/bomb archives, credential exclusion, unsafe provider URLs, malformed schemas, stale hashes, prompt-injection-like input, rollback, duplicated proposals, retry ceilings, malformed HTTP headers, and control-plane aborts.

The four locally skipped checks require actual Docker execution: substantive model-edit acceptance, behavioral regression rollback, runtime isolation controls, and hanging-test timeout/cleanup. Docker is not installed on this Windows host. [GitHub Actions verified the first implementation commit](https://github.com/amrs-tech/CodeProof/actions/runs/37635542801) successfully with these tests enabled, a real PostgreSQL/pgvector service, and successful sandbox/application image builds.

The final audit also hardens stopped-worker readiness/submission/polling behavior, applicable patches for unterminated source lines and unusual line characters, and atomic candidate publication/rollback. Tests reproduce exact bytes when applying generated patches with Git and cover failed temporary writes, mode preservation, resolved-finding status at the attempt limit, and invalid sandbox count metadata. A trusted Docker regression test demonstrates that separate default calls no longer share a mutable list and confirms that the original source fails that check. The current workflow status is available in [repository verification runs](https://github.com/amrs-tech/CodeProof/actions/workflows/verify.yml).

## Practical limits

- Python remediation is supported; other source languages are accepted with an explicit coverage limit.
- Provider integration has HTTP-level mock coverage. Model proposals require configured credentials plus sandbox evidence before acceptance; any live-provider evidence is recorded separately below.
- The standard sandbox supports stdlib unittest under `tests/`, without installing target dependencies. Other test frameworks/runtime profiles need future work.
- Context uses deterministic lexical hashing, not neural semantic embeddings.
- Static fixes are conservative textual improvements. Static acceptance does not establish behavioral correctness, and passing existing tests is not a formal proof.
- This is a single-user local app. Broader deployment needs tenancy, authentication/authorization, stronger worker isolation, and operational controls.

## How to review locally

Follow the README to start the app and database, create the demo ZIP, submit it, inspect action rationale and validation, and download the report and patch. Configure Gemini and/or OpenAI-compatible credentials and build the optional sandbox to exercise substantive remediation. Every unresolved or rejected finding stays visible.

## Additional provider and reporting update

The follow-up adds native Gemini `generateContent` alongside OpenAI-compatible Chat Completions. With `auto` selection, a configured Gemini key and model take priority; otherwise OpenAI uses `gpt-6-luna` with medium reasoning by default. The approved Gemini example is `gemini-3.8-flash`. Provider/model choices remain configurable, and explicit selections never borrow another provider's key. Gemini Live is rejected because its audio Live interface lacks the structured outputs required for guarded edits.

The authorized availability policy now permits one Gemini-to-OpenAI fallback for automatic environment reviews when the backup is already configured and `CODEPROOF_LLM_FALLBACK_ENABLED=true`. After bounded retries, authentication/model availability (401/403/404), quota/rate limits (429), service failures (5xx), network failures, and timeouts can trigger the transition. OpenAI then remains selected for the rest of that review. Explicit provider/model choices and BYOK reviews stay pinned. Refusals, malformed or unsafe output, edit guard failures, time limits, and budget exhaustion never trigger a provider switch. All failed and backup calls share the original global call budget, and the review deadline is unchanged.

The source form now has optional provider, model, run-only key and OpenAI effort controls. Base URLs remain operator settings. Per-run settings are resolved once and held in a locked memory vault; PostgreSQL receives only provider/model/effort/credential-source metadata. Browser key inputs clear on submission, provider changes and navigation. Worker success, failure and lost ownership release key references, and shutdown clears pending settings. Queued BYOK runs fail after restart if their key is unavailable. Older queued jobs without provider metadata stay keyless, so adding an environment key cannot silently enable old reviews. Credential-like model IDs and credential echoes in provider output are rejected; errors never expose provider response bodies.

HTML report downloads are available beside Markdown, JSON and the accepted patch. The standalone document presents outcome metrics, requested and actual provider/model provenance, fixed fallback reasons/history and shared call counts, findings and rationale, action/rollback records, validation evidence, stop reasons, limitations and accepted changes. Markdown includes the same readable provider audit. Full evidence remains inspectable in expandable sections. Inline responsive/print styling works offline; dynamic values are escaped, scripts and remote assets are absent, and downloads carry a restrictive content security policy.

The earlier provider/reporting follow-up used mocked provider HTTP responses and a real local PostgreSQL/pgvector database. It covered both protocol formats, GPT-6 Luna reasoning, malformed/refused/truncated responses, quota stopping, credential boundaries, independent per-run settings, restart recovery and HTML escaping/downloads. Ruff lint/format, JavaScript syntax and Git whitespace checks passed. Browser QA verified provider defaults, report rendering at desktop and narrow widths without overflow, and an actual HTML download from a persisted review. That historical suite passed **228 tests**, with **4 Docker checks skipped locally** and approximately **92% statement coverage**; the current update has the larger suite reported below.

The previous provider/reporting update used no live model requests. The user subsequently authorized bounded live credential and remediation validation with Gemini first and OpenAI as a backup. Existing environment values are preserved when adding configuration fields.

## Authorized live provider verification

The following checks used real provider responses on 7 October 2026. Their source was the small mutable-default example in `app.py`; they did not execute submitted code or apply a substantive change locally.

| Check | Observed result | Calls and evidence |
| --- | --- | --- |
| Direct OpenAI | `gpt-6-luna`, medium reasoning, returned a structured replacement that uses `None` and creates a fresh list per call. The proposal passed the schema/hash and Python edit guards. | One provider call; safe evidence in `.test-artifacts/live-verification/openai.json`. |
| Direct native Gemini | `gemini-3.8-flash` did not produce a successful guarded proposal within two bounded attempts. | Two provider calls; safe evidence in `.test-artifacts/live-verification/gemini.json`. |
| Automatic Gemini-first policy | Requested `gemini-3.8-flash`. Two native generation requests timed out; one sticky transition selected OpenAI `gpt-6-luna`, which returned a guarded proposal for the same finding. | Three shared provider calls: two Gemini attempts and one OpenAI attempt. Fixed fallback reason: `timeout`; safe evidence in `.test-artifacts/live-verification/fallback.json`. |
| Gemini model availability | A separate model-metadata GET returned HTTP 200 and advertised `generateContent` support. | This proves the model metadata was accessible; it does not establish successful Gemini generation. |
| Final Gemini diagnostic | One documented diagnostic used low thinking, a 4096-token output cap and no `candidateCount`. Its generation request timed out after 60.38 seconds; the single-call budget stopped the check. | One provider call; safe evidence in `.test-artifacts/live-verification/gemini-low.json`. No further manual Gemini diagnostics are scheduled. |

The fallback used only the already configured server environment credentials. Explicit provider/model selections and BYOK reviews remain pinned and cannot activate that backup. Requested and actual providers/models, the fixed timeout reason, the transition at call two, and total call count three remain visible in the HTML, Markdown and JSON audit. The transition did not reset calls, attempts, or the review deadline.

These live results establish real OpenAI structured-proposal compatibility and real bounded Gemini-to-OpenAI failover. They do **not** establish successful native Gemini generation or behavioral acceptance of the returned replacement. Docker execution is unavailable on this Windows host, so local evidence stops at proposal guards. Current CI replay of the captured candidate through isolated baseline/candidate tests is pending and will be reported separately when complete. Production keeps provider-default Gemini thinking settings and omits the unsupported `candidateCount` field. Transport retry backoff is bounded to one second by default, with numeric `Retry-After` capped at two seconds; all waits share the review deadline.

Production output allowances scale with source size, with a minimum of 4096 tokens for Gemini and 1024 for OpenAI-compatible requests, capped at 16000 tokens. The larger Gemini floor leaves room for native thinking and the complete structured source replacement, instead of assigning a tiny output allowance to small files. This changes the output budget without overriding Gemini's default thinking settings; it does not establish that Gemini generation succeeded in the recorded diagnostics.

## Current update verification

- Full local suite: **285 passed, 6 Docker checks skipped**, **291 collected**, with **92% application statement coverage**.
- Real PostgreSQL 15 with pgvector 0.8.2 integration passed, including source intake, durable queue/run handling, vector context, worker ownership, and ZIP-to-report HTML/Markdown downloads.
- Mocked-provider tests cover one sticky Gemini-to-OpenAI fallback, a shared call budget, pinned selections and BYOK boundaries, and rejection of unsafe/malformed/refused outputs without fallback.
- Browser QA confirmed the Gemini 3.8 Flash primary/OpenAI GPT-6 Luna backup display, default-setting disclosure, pinned explicit/BYOK policy, and a completed HTML report download from a previously persisted `fragile-python` review. The UI continued to disclose static-only runtime evidence because Docker is unavailable locally.
- Ruff lint and formatting, JavaScript syntax, and Git whitespace checks passed.

The first verification attempt could not reach local PostgreSQL from the restricted network context. The full rerun with authorized local database access passed. Automated provider tests remained mocked; the separately authorized live provider results are recorded above.

The six Docker checks are skipped locally because Docker is unavailable on this host. The current push's GitHub Actions result and captured-candidate behavioral replay remain pending. Earlier successful CI runs establish historical sandbox coverage, not completion of these current six checks.
