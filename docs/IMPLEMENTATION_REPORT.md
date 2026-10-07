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

- Full suite: **127 passed, 4 skipped**, with **89% application statement coverage**.
- Actual PostgreSQL 15 with pgvector 0.8.2: extension initialization, vector retrieval, run isolation, queue capacity, concurrent job claims, worker ownership, and persisted reports verified.
- End-to-end API: a submitted ZIP produced an accepted deterministic change, persisted its report, and served both report and patch downloads.
- Browser demonstration: `fragile-python.zip` produced one statically validated improvement and retained the mutable-default finding as unresolved. The interface showed the actual evidence and limitations; patch download completed successfully.
- Live public GitHub intake: `pallets/itsdangerous` was fetched at commit `672971d66a2ef9f85151e53283113f33d642dabd`. It ingested 50 files, analyzed 15 Python files, and reported 11 review findings without forcing unsupported edits.
- Ruff lint, Ruff formatting, JavaScript syntax, dependency consistency, and Git whitespace validation passed.
- Guardrail coverage includes traversal/link/collision/bomb archives, credential exclusion, unsafe provider URLs, malformed schemas, stale hashes, prompt-injection-like input, rollback, duplicated proposals, retry ceilings, malformed HTTP headers, and control-plane aborts.

The four skipped checks require actual Docker execution: substantive model-edit acceptance, behavioral regression rollback, runtime isolation controls, and hanging-test timeout/cleanup. Docker is not installed on this Windows host. GitHub Actions builds the sandbox and enables these tests with a real PostgreSQL/pgvector service; its result must be recorded after the push.

## Practical limits

- Python remediation is supported; other source languages are accepted with an explicit coverage limit.
- No live paid model request was made. Provider integration is tested with HTTP-level mock responses, and model proposals require configured credentials plus sandbox evidence before acceptance.
- The standard sandbox supports stdlib unittest under `tests/`, without installing target dependencies. Other test frameworks/runtime profiles need future work.
- Context uses deterministic lexical hashing, not neural semantic embeddings.
- Static fixes are conservative textual improvements. Static acceptance does not establish behavioral correctness, and passing existing tests is not a formal proof.
- This is a single-user local app. Broader deployment needs tenancy, authentication/authorization, stronger worker isolation, and operational controls.

## How to review locally

Follow the README to start the app and database, create the demo ZIP, submit it, inspect action rationale and validation, and download the report and patch. Configure an OpenAI-compatible provider and build the optional sandbox to exercise substantive remediation. Every unresolved or rejected finding stays visible.
