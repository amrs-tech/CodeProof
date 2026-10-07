# CodeProof

An autonomous repository review and remediation agent built with **LangGraph**, **PostgreSQL**, and **pgvector**. Submit a source ZIP or a public GitHub repository, inspect findings and rationale, and download a patch containing accepted improvements.

CodeProof edits an isolated copy. It never pushes to a submitted repository. This first release analyzes and remediates Python; coverage limits for other languages are reported explicitly.

## Capabilities

- Establish a syntax, Ruff, and custom AST baseline before editing.
- Detect fragile implementations such as mutable defaults, swallowed exceptions, undefined names, and dynamic execution, alongside maintenance debt.
- Apply a small allowlist of conservative textual fixes automatically.
- Use OpenAI-compatible or native Gemini models for targeted source replacements. Substantive model edits require passing baseline and candidate tests in a restricted Docker sandbox.
- Reject unexpected paths, stale source hashes, new findings, removed definitions, new imports, test edits, suppressed checks, duplicate proposals, and oversized changes. Failed validation restores the prior bytes.
- Stop at per-finding attempts, total attempts, provider calls, time limits, or lack of progress.
- Persist the queue, progress, findings, validation evidence, rationale, and reports in PostgreSQL. Use pgvector to retrieve related source chunks within the same run.

The offline index uses deterministic 384-dimensional lexical feature hashing. It provides token-similarity retrieval without another model service; it is **not a neural semantic embedding model**.

## Quick local start

Install Docker with Compose, then:

```sh
cp .env.example .env
docker compose up --build
```

On PowerShell, use `Copy-Item .env.example .env`. Open [CodeProof](http://localhost:8000). PostgreSQL and the UI bind to loopback. Startup initializes the schema and required vector extension.

Static review and conservative fixes work without an API key. Set provider credentials in `.env`, then restart the native app or recreate the Compose app service. OpenAI-compatible providers must support `/chat/completions` with strict JSON-schema responses. Gemini uses its native `generateContent` API.

## Model configuration and own keys

| Setting | Default / purpose |
| --- | --- |
| `CODEPROOF_LLM_PROVIDER` | `auto`: Gemini when both its key and model are configured, otherwise OpenAI-compatible. Set `openai` or `gemini` to choose explicitly. |
| `CODEPROOF_LLM_API_KEY` | OpenAI-compatible API key; blank disables this provider. |
| `CODEPROOF_LLM_MODEL` | `gpt-6-luna`; a legacy blank value also resolves to this default. |
| `CODEPROOF_LLM_REASONING_EFFORT` | `medium`; applied to supported OpenAI reasoning models, omitted for other compatible models. |
| `CODEPROOF_LLM_BASE_URL` | `https://api.openai.com/v1`; custom endpoints are operator configuration. |
| `CODEPROOF_GEMINI_API_KEY` | Gemini API key; blank disables this provider. |
| `CODEPROOF_GEMINI_MODEL` | Set `gemini-3.8-flash` or another compatible Gemini text model. The sample environment includes Flash. |
| `CODEPROOF_GEMINI_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta` |

The UI's optional **Model settings** let you choose a provider, override its model, and enter your own key for one review (BYOK). An empty key uses only that selected provider's environment key. OpenAI defaults to medium reasoning; Gemini uses its model's native thinking settings. Browser users cannot override server endpoint URLs. The CodeProof access token is separate from a model API key.

Run-only keys stay in server memory until the worker consumes them, and are released after completion or failure. They are never saved in PostgreSQL, reports, source files, browser storage, or application logs. The input clears immediately on submission. Queued BYOK reviews lose their key on server restart and fail with a request to resubmit, instead of silently using an environment key. A report records only provider, model, applicable effort, and credential source.

Gemini Live/audio models do not support the structured code-edit responses required here and are rejected before review submission. Quota/rate-limit errors receive at most two transport attempts per proposal, also counted against the overall call budget. There is no automatic switch to Live or another provider: choose another configured provider for a new review. See [Google's Live model documentation](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live) and [OpenAI's GPT-6 Luna documentation](https://developers.openai.com/api/docs/models/gpt-6-luna).

The Compose app has no Docker daemon access. For substantive remediation with behavioral checks, run the app natively with a local Docker sandbox.

## Native app and behavioral verification

Requirements: Python 3.12+, PostgreSQL 15+ with pgvector 0.8+, and Docker when behavioral verification is enabled.

```sh
python -m venv .venv
# Linux/macOS:
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
# Windows:
.venv\Scripts\python.exe -m pip install -r requirements.lock
.venv\Scripts\python.exe -m pip install --no-deps -e .
```

Set `CODEPROOF_DATABASE_URL` to a dedicated database. The initial migration needs permission to create `vector`, or an administrator must create it first. To run only PostgreSQL through Compose, use `docker compose up -d db`.

```sh
docker build -f Dockerfile.sandbox -t codeproof-sandbox:local .
```

Set `CODEPROOF_SANDBOX_ENABLED=true` in `.env`, configure the model, then start one worker:

```sh
# Linux/macOS:
.venv/bin/python -m uvicorn codeproof.app:app --host 127.0.0.1 --port 8000 --workers 1
# Windows:
.venv\Scripts\python.exe -m uvicorn codeproof.app:app --host 127.0.0.1 --port 8000 --workers 1
```

The sandbox runs fixed stdlib `unittest` discovery against `tests/`. It installs no submitted dependencies and runs no repository-defined commands. At least one non-skipped test must pass. Projects using pytest, third-party dependencies, other test locations, or different runtimes need a separately designed sandbox profile; model edits are held for review in this version.

## Try the demo

```sh
python scripts/create_demo_zip.py
```

Upload `data/fragile-python.zip`. Without a model, CodeProof removes a redundant f-string and flags the shared mutable default. With a compatible model and working sandbox, it can propose and validate a targeted correction. Model output is not guaranteed; unsuccessful proposals remain visible in the report. Reports show findings up to the configured limit and disclose that coverage limit.

GitHub input accepts `owner/repository` or `https://github.com/owner/repository`, fetching its public default branch without credentials. Private repositories must be provided as a ZIP. Defaults limit inputs to 20 MiB compressed, 50 MiB expanded, 2,000 files, and 512 KiB per file. Credential files, source-control internals, and common generated directories are omitted. Obvious credential literals are withheld from model transmission and the vector index; detection is heuristic.

## Reports and API

The UI presents findings, attempts, accepted/rejected edits, rationale, progress, validation evidence, stop reasons, limitations, and recent runs. Download a presentable standalone HTML report, Markdown, JSON, or a unified diff. HTML includes readable findings and rationale, validation evidence, provider metadata, accepted changes, and print styling; it opens offline without scripts or external assets. Repository/model text is escaped before inclusion.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Database readiness and provider/sandbox configuration |
| `POST /api/runs` | Multipart `file` ZIP or `repository`; exactly one. Optional `model_provider`, `model_name`, `model_api_key`, `model_reasoning_effort` overrides. Select an explicit provider when sending overrides. |
| `GET /api/runs` | Recent runs |
| `GET /api/runs/{id}` | Progress and persisted report |
| `GET /api/runs/{id}/report?format=markdown` | Report (`html` and `json` also supported) |
| `GET /api/runs/{id}/patch` | Accepted changes |

Set `CODEPROOF_API_TOKEN` to require `Authorization: Bearer ...`. The UI has an optional token field retained only in memory. Use CodeProof locally; deployment beyond loopback requires a separate authentication, authorization, and tenancy design. See [SECURITY.md](SECURITY.md).

## Verification and standards

The dependency lock records verified versions. Install it before the package. `pyproject.toml` declares supported development ranges.

```sh
python -m ruff check .
python -m ruff format --check .
python -m pytest --cov=codeproof --cov-report=term-missing
```

Set `CODEPROOF_TEST_DATABASE_URL` to a dedicated **test database** for actual PostgreSQL/pgvector integration coverage. Tests clean their own records and may mark interrupted jobs failed; never use an active app database. Set `CODEPROOF_TEST_SANDBOX=1` after building the sandbox image for Docker execution tests. Unavailable integrations are skipped and disclosed.

Automated tests isolate the operator's `.env` and provider credentials; provider HTTP calls use mocked responses. Live provider validation is a separate, explicitly started check after credentials are configured.

GitHub Actions runs lint/format, tests with PostgreSQL/pgvector, Docker sandbox checks, and an app image build. See [architecture](docs/ARCHITECTURE.md), [implementation report](docs/IMPLEMENTATION_REPORT.md), and [contributing](CONTRIBUTING.md).
