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

## Local verification evidence

- Final local suite: **143 passed, 4 skipped**, with **89% application statement coverage**.
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
- No live paid model request was made. Provider integration is tested with HTTP-level mock responses, and model proposals require configured credentials plus sandbox evidence before acceptance.
- The standard sandbox supports stdlib unittest under `tests/`, without installing target dependencies. Other test frameworks/runtime profiles need future work.
- Context uses deterministic lexical hashing, not neural semantic embeddings.
- Static fixes are conservative textual improvements. Static acceptance does not establish behavioral correctness, and passing existing tests is not a formal proof.
- This is a single-user local app. Broader deployment needs tenancy, authentication/authorization, stronger worker isolation, and operational controls.

## How to review locally

Follow the README to start the app and database, create the demo ZIP, submit it, inspect action rationale and validation, and download the report and patch. Configure an OpenAI-compatible provider and build the optional sandbox to exercise substantive remediation. Every unresolved or rejected finding stays visible.

## Additional provider and reporting update

The follow-up adds native Gemini `generateContent` alongside OpenAI-compatible Chat Completions. With `auto` selection, a configured Gemini key and model take priority; otherwise OpenAI uses `gpt-6-luna` with medium reasoning by default. The approved Gemini example is `gemini-3.8-flash`. Provider/model choices remain configurable, and explicit selections never borrow another provider's key. Gemini Live is rejected because its audio Live interface lacks the structured outputs required for guarded edits; quota exhaustion stops within the existing call budget instead of silently switching models or providers.

The source form now has optional provider, model, run-only key and OpenAI effort controls. Base URLs remain operator settings. Per-run settings are resolved once and held in a locked memory vault; PostgreSQL receives only provider/model/effort/credential-source metadata. Browser key inputs clear on submission, provider changes and navigation. Worker success, failure and lost ownership release key references, and shutdown clears pending settings. Queued BYOK runs fail after restart if their key is unavailable. Older queued jobs without provider metadata stay keyless, so adding an environment key cannot silently enable old reviews. Credential-like model IDs and credential echoes in provider output are rejected; errors never expose provider response bodies.

HTML report downloads are available beside Markdown, JSON and the accepted patch. The standalone document presents outcome metrics, provenance, findings and rationale, action/rollback records, validation evidence, stop reasons, limitations and accepted changes. Full evidence remains inspectable in expandable sections. Inline responsive/print styling works offline; dynamic values are escaped, scripts and remote assets are absent, and downloads carry a restrictive content security policy.

Follow-up verification used mocked provider HTTP responses and a real local PostgreSQL/pgvector database. It covers both protocol formats, GPT-6 Luna reasoning, malformed/refused/truncated responses, quota stopping, credential boundaries, independent per-run settings, restart recovery and HTML escaping/downloads. Ruff lint/format, JavaScript syntax and Git whitespace checks passed. Browser QA verified provider defaults, report rendering at desktop and narrow widths without overflow, and an actual HTML download from a persisted review. The expanded local suite passed **228 tests**, with **4 Docker checks skipped locally** and approximately **92% statement coverage**. Docker execution remains covered by GitHub Actions.

No live model request was made for this update. Live credential and remediation validation is deliberately deferred until the user configures `.env` and gives the requested go signal. The current local preview disables provider calls during that wait. Existing environment values were preserved when adding Gemini configuration fields.
