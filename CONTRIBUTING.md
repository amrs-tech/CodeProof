# Contributing

Use Python 3.12+ and the dependency lock. Keep changes targeted and explain their rationale. Add meaningful tests for changes to guardrails, retry limits, validation behavior, or intake.

Before committing, run Ruff lint/format and pytest. Use a dedicated PostgreSQL/pgvector test database and include Docker checks when available. Disclose skipped checks. Fragile demos and fixtures are excluded from project lint; submitted source uses isolated analysis rules.

Never weaken a guardrail to make a demonstration pass. Reports must distinguish static from behavioral evidence. Preserve submitted tests, keep source/model content untrusted, and make execution finite.

Use environment variables for credentials. Never commit `.env`, submitted archives, local data, or database files. Commit and push coherent repository updates after successful local verification, keep CI green, and record remaining limitations.
