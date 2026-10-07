"""Resolve providers without persisting credentials or sharing overrides between runs."""

import re
from threading import Lock

from codeproof.config import Settings
from codeproof.llm import EditProvider, contains_credentials

DEFAULT_OPENAI_MODEL = "gpt-6-luna"
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _sensitive_model(config: Settings, model: str, override_key: str = "") -> bool:
    return contains_credentials(model) or any(
        key and (model == key or (len(key) >= 8 and key in model))
        for key in (config.llm_api_key, config.gemini_api_key, override_key)
    )


class ProviderConfigurationError(ValueError):
    """Messages are fixed text; submitted credentials never enter exceptions."""


class RunSettingsVault:
    """Pending settings exist only in memory; claim and shutdown remove references."""

    def __init__(self):
        self._entries: dict[str, Settings] = {}
        self._lock = Lock()

    def put(self, run_id: str, config: Settings) -> None:
        with self._lock:
            self._entries[run_id] = config

    def pop(self, run_id: str) -> Settings | None:
        with self._lock:
            return self._entries.pop(run_id, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def provider_metadata(config: Settings, credential_source: str = "environment") -> dict:
    provider = config.llm_provider
    if provider == "auto":
        provider = "gemini" if config.gemini_api_key and config.gemini_model else "openai"
    model = (
        config.gemini_model or DEFAULT_GEMINI_MODEL
        if provider == "gemini"
        else config.llm_model or DEFAULT_OPENAI_MODEL
    )
    key = config.gemini_api_key if provider == "gemini" else config.llm_api_key
    if _sensitive_model(config, model):
        model = "(invalid model configuration)"
    effort = EditProvider(
        config.model_copy(
            update={
                "llm_provider": provider,
                "llm_model": model if provider == "openai" else config.llm_model,
            }
        )
    ).public_metadata["reasoning_effort"]
    return {
        "provider": provider,
        "model": model,
        "reasoning_effort": effort,
        "credential_source": credential_source if key else "none",
    }


def _validate_model(provider: str, model: str) -> None:
    if not MODEL_ID.fullmatch(model):
        raise ProviderConfigurationError(
            "Model must be an identifier of at most 128 letters, digits, dots, underscores or hyphens"
        )
    if provider == "gemini":
        if not model.startswith("gemini-"):
            raise ProviderConfigurationError("Choose a Gemini text-generation model identifier")
        if any(
            part in model.lower().split("-")
            for part in ("live", "audio", "tts", "image", "embedding")
        ):
            raise ProviderConfigurationError(
                "Gemini Live/audio models do not support guarded JSON edits. Choose a Flash model."
            )


def resolve_provider(
    config: Settings,
    provider: str = "",
    model: str = "",
    api_key: str = "",
    reasoning_effort: str = "",
) -> tuple[Settings, dict]:
    provider, model = provider.strip(), model.strip()
    api_key, reasoning_effort = api_key.strip(), reasoning_effort.strip()
    if provider and provider not in {"openai", "gemini"}:
        raise ProviderConfigurationError("Choose OpenAI-compatible or Gemini")
    if not provider and (model or api_key or reasoning_effort):
        raise ProviderConfigurationError("Select a provider before supplying model overrides")
    if len(api_key) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in api_key):
        raise ProviderConfigurationError(
            "Provider key must contain at most 4096 printable characters"
        )
    if reasoning_effort and reasoning_effort not in EFFORTS:
        raise ProviderConfigurationError("Unsupported reasoning effort")
    selected = provider_metadata(config)
    chosen = provider or selected["provider"]
    chosen_model = model or (
        (config.gemini_model or DEFAULT_GEMINI_MODEL)
        if chosen == "gemini"
        else (config.llm_model or DEFAULT_OPENAI_MODEL)
    )
    if _sensitive_model(config, chosen_model, api_key):
        raise ProviderConfigurationError("Model must be a model identifier, not a credential")
    _validate_model(chosen, chosen_model)
    key = api_key or (config.gemini_api_key if chosen == "gemini" else config.llm_api_key)
    if provider and not key:
        raise ProviderConfigurationError(
            "Configure this provider's key in .env or enter a run-only key"
        )
    updates = {
        "llm_provider": chosen,
        "llm_reasoning_effort": reasoning_effort or config.llm_reasoning_effort,
        # Only the chosen provider credential is retained in the pending review.
        "llm_api_key": key if chosen == "openai" else "",
        "gemini_api_key": key if chosen == "gemini" else "",
        "llm_model": chosen_model if chosen == "openai" else config.llm_model,
        "gemini_model": chosen_model if chosen == "gemini" else config.gemini_model,
    }
    resolved = config.model_copy(update=updates)
    return resolved, provider_metadata(resolved, "byok" if api_key else "environment")


def recover_run_settings(config: Settings, metadata: dict) -> Settings:
    if not metadata:
        # Old queue rows have no auditable provider choice; do not enable newly added keys.
        return config.model_copy(update={"llm_api_key": "", "gemini_api_key": ""})
    if metadata.get("credential_source") not in {"byok", "environment", "none"}:
        raise ProviderConfigurationError("Saved provider configuration is invalid; submit again")
    if metadata.get("credential_source") == "byok":
        raise ProviderConfigurationError("Run-only provider key expired; submit the review again")
    if metadata.get("credential_source") == "none":
        offline = config.model_copy(update={"llm_api_key": "", "gemini_api_key": ""})
        if not metadata.get("provider"):
            return offline
        provider = metadata["provider"]
        model = metadata.get("model") or (
            DEFAULT_GEMINI_MODEL if provider == "gemini" else DEFAULT_OPENAI_MODEL
        )
        _validate_model(provider, model)
        return offline.model_copy(
            update={
                "llm_provider": provider,
                "gemini_model" if provider == "gemini" else "llm_model": model,
                "llm_reasoning_effort": metadata.get("reasoning_effort")
                or config.llm_reasoning_effort,
            }
        )
    return resolve_provider(
        config,
        metadata.get("provider", ""),
        metadata.get("model", ""),
        reasoning_effort=metadata.get("reasoning_effort") or "",
    )[0]


def provider_health(config: Settings) -> dict:
    selected = provider_metadata(config)
    entries = []
    notice = None
    for provider, label in (("openai", "OpenAI-compatible"), ("gemini", "Gemini")):
        snapshot = provider_metadata(config.model_copy(update={"llm_provider": provider}))
        configured = snapshot["credential_source"] != "none" and config.llm_max_calls > 0
        if provider == "gemini" and not config.gemini_model:
            configured = False
        try:
            _validate_model(provider, snapshot["model"])
        except ProviderConfigurationError as exc:
            configured = False
            if selected["provider"] == provider:
                notice = str(exc)
        entries.append({"id": provider, "label": label, "configured": configured, **snapshot})
    return {
        "provider_configured": next(
            entry["configured"] for entry in entries if entry["id"] == selected["provider"]
        ),
        "selected_provider": selected["provider"],
        "selected_model": selected["model"],
        "providers": entries,
        "provider_notice": notice,
    }
