"""Tests must never inherit an operator's paid provider credentials."""

import pytest

from codeproof.config import Settings


@pytest.fixture(autouse=True)
def isolated_provider_environment(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for field in (
        "LLM_PROVIDER",
        "LLM_API_KEY",
        "LLM_MODEL",
        "LLM_REASONING_EFFORT",
        "LLM_BASE_URL",
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "GEMINI_BASE_URL",
        "API_TOKEN",
        "SANDBOX_ENABLED",
    ):
        monkeypatch.delenv(f"CODEPROOF_{field}", raising=False)
