import json
import weakref
from threading import Event
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from test_api import MemoryStore, zip_bytes

from codeproof.app import create_app
from codeproof.config import Settings
from codeproof.providers import (
    ProviderConfigurationError,
    RunSettingsVault,
    provider_health,
    recover_run_settings,
    resolve_provider,
)
from codeproof.worker import work


def config(**kwargs):
    return Settings(_env_file=None, **kwargs)


def test_default_openai_model_effort_and_legacy_blank_model():
    for model in ("gpt-6-luna", ""):
        resolved, metadata = resolve_provider(config(llm_api_key="openai-test", llm_model=model))
        assert resolved.llm_model == "gpt-6-luna"
        assert resolved.llm_reasoning_effort == "medium"
        assert metadata == {
            "provider": "openai",
            "model": "gpt-6-luna",
            "reasoning_effort": "medium",
            "credential_source": "environment",
        }


def test_auto_requires_both_gemini_key_and_model_and_explicit_provider_wins():
    base = config(llm_api_key="openai-test", gemini_api_key="gemini-test")
    assert resolve_provider(base)[1]["provider"] == "openai"
    both = base.model_copy(update={"gemini_model": "gemini-3.8-flash"})
    resolved, metadata = resolve_provider(both)
    assert metadata["provider"] == "gemini"
    assert metadata["reasoning_effort"] is None
    assert resolved.llm_api_key == "openai-test"
    assert resolved.llm_fallback_enabled
    assert metadata["fallback"]["provider"] == "openai"
    assert metadata["fallback"]["model"] == "gpt-6-luna"
    assert resolve_provider(both, "openai")[0].gemini_api_key == ""
    assert (
        resolve_provider(both.model_copy(update={"llm_provider": "openai"}))[1]["provider"]
        == "openai"
    )


def test_byok_is_run_scoped_without_mutating_environment_settings():
    original = config(llm_api_key="environment-test", gemini_api_key="other-test")
    first, public = resolve_provider(original, "openai", "gpt-6-luna", "run-test", "high")
    second, _ = resolve_provider(original)
    assert first.llm_api_key == "run-test"
    assert first.gemini_api_key == ""
    assert second.llm_api_key == "environment-test"
    assert original.llm_api_key == "environment-test"
    assert public["credential_source"] == "byok"
    assert "run-test" not in json.dumps(public)
    assert "run-test" not in repr(first)
    assert "other-test" not in repr(original)
    assert not first.llm_fallback_enabled


def test_explicit_and_disabled_policies_do_not_retain_backup_credentials():
    both = config(
        llm_api_key="openai-test", gemini_api_key="gemini-test", gemini_model="gemini-3.8-flash"
    )
    for settings, provider, key in (
        (both, "gemini", ""),
        (both, "gemini", "byok-test"),
        (both.model_copy(update={"llm_fallback_enabled": False}), "", ""),
        (both.model_copy(update={"llm_provider": "gemini"}), "", ""),
    ):
        resolved, metadata = resolve_provider(settings, provider, api_key=key)
        assert not resolved.llm_fallback_enabled
        assert resolved.llm_api_key == ""
        assert "fallback" not in metadata


def test_fallback_health_and_recovery_preserve_recorded_destinations():
    initial = config(
        llm_api_key="openai-test",
        llm_model="gpt-6-luna",
        gemini_api_key="gemini-test",
        gemini_model="gemini-3.8-flash",
    )
    health = provider_health(initial)
    assert health["fallback_configured"]
    assert health["fallback_provider"] == "openai"
    _, metadata = resolve_provider(initial)
    changed = initial.model_copy(update={"llm_model": "new-model", "gemini_model": "gemini-new"})
    recovered = recover_run_settings(changed, metadata)
    assert recovered.llm_fallback_enabled
    assert recovered.llm_model == "gpt-6-luna"
    assert recovered.gemini_model == "gemini-3.8-flash"
    disabled = recover_run_settings(
        changed.model_copy(update={"llm_fallback_enabled": False}), metadata
    )
    assert not disabled.llm_fallback_enabled
    pinned = {key: value for key, value in metadata.items() if key != "fallback"}
    assert not recover_run_settings(changed, pinned).llm_fallback_enabled
    bad = {**metadata, "fallback": {**metadata["fallback"], "provider": "gemini"}}
    with pytest.raises(ProviderConfigurationError, match="invalid"):
        recover_run_settings(initial, bad)


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider": "unknown"},
        {"model": "gpt-6-luna"},
        {"api_key": "secret-value"},
        {"provider": "openai"},
        {"provider": "openai", "api_key": "secret-value", "model": "bad?secret=value"},
        {"provider": "openai", "api_key": "secret-value", "model": "m" * 129},
        {"provider": "openai", "api_key": "secret-value", "reasoning_effort": "unbounded"},
        {"provider": "gemini", "api_key": "secret-value", "model": "gemini-3.8-live"},
        {"provider": "openai", "api_key": "secret-value\nheader"},
        {"provider": "openai", "api_key": "ésecret-value"},
        {"provider": "openai", "api_key": "secret-value" * 500},
    ],
)
def test_invalid_overrides_have_fixed_secret_free_errors(overrides):
    with pytest.raises(ProviderConfigurationError) as error:
        resolve_provider(config(), **overrides)
    assert "secret-value" not in str(error.value)


def test_missing_gemini_key_never_uses_openai_key():
    with pytest.raises(ProviderConfigurationError):
        resolve_provider(config(llm_api_key="openai-test"), "gemini")


def test_model_field_cannot_persist_a_provider_credential():
    key = "sk-proj-" + "example" * 6
    original = config(llm_api_key=key, llm_model=key)
    assert key not in json.dumps(provider_health(original))
    with pytest.raises(ProviderConfigurationError, match="credential"):
        resolve_provider(config(), "openai", key, key)
    with pytest.raises(ProviderConfigurationError, match="credential"):
        resolve_provider(original)


def test_health_has_safe_provider_status_and_rejects_live():
    status = provider_health(config(llm_api_key="openai-test"))
    assert status["selected_model"] == "gpt-6-luna"
    assert status["provider_configured"]
    live = provider_health(config(gemini_api_key="gemini-test", gemini_model="gemini-3.8-live"))
    assert not live["provider_configured"]
    assert "Flash" in live["provider_notice"]
    assert "gemini-test" not in json.dumps(live)


def test_queued_credentials_are_consumed_once_and_discarded_at_shutdown():
    vault = RunSettingsVault()
    resolved, _ = resolve_provider(config(), "gemini", api_key="run-test")
    vault.put("first", resolved)
    assert vault.pop("first") is resolved
    assert vault.pop("first") is None
    vault.put("second", resolved)
    vault.clear()
    assert vault.pop("second") is None


def test_restart_does_not_replay_byok_with_environment_key():
    with pytest.raises(ProviderConfigurationError, match="expired"):
        recover_run_settings(
            config(llm_api_key="env-test"),
            {
                "provider": "openai",
                "credential_source": "byok",
                "model": "gpt-6-luna",
            },
        )
    restored = recover_run_settings(
        config(llm_api_key="env-test"),
        {
            "provider": "openai",
            "credential_source": "environment",
            "model": "custom",
            "reasoning_effort": "low",
        },
    )
    assert restored.llm_model == "custom"
    assert restored.llm_reasoning_effort == "low"
    offline = recover_run_settings(config(llm_api_key="env-test"), {"credential_source": "none"})
    assert offline.llm_api_key == offline.gemini_api_key == ""
    preserved = recover_run_settings(
        config(llm_provider="openai", llm_api_key="env-test"),
        {
            "provider": "gemini",
            "model": "gemini-3.8-flash",
            "credential_source": "none",
        },
    )
    assert preserved.llm_provider == "gemini"
    assert preserved.gemini_model == "gemini-3.8-flash"
    assert preserved.llm_api_key == preserved.gemini_api_key == ""
    legacy = recover_run_settings(config(llm_api_key="env-test"), {})
    assert legacy.llm_api_key == legacy.gemini_api_key == ""
    with pytest.raises(ProviderConfigurationError, match="invalid"):
        recover_run_settings(config(llm_api_key="env-test"), {"credential_source": "unknown"})


def test_api_byok_never_enters_persisted_or_public_run(tmp_path):
    store = MemoryStore()
    app = create_app(config(data_dir=tmp_path), store, False)
    with TestClient(app) as http:
        response = http.post(
            "/api/runs",
            files={"file": ("demo.zip", zip_bytes())},
            data={
                "model_provider": "gemini",
                "model_api_key": "run-only-test",
                "model_name": "gemini-3.8-flash",
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        assert response.json()["source"]["provider"]["credential_source"] == "byok"
        for data in (
            store.runs,
            response.json(),
            http.get("/api/runs").json(),
            http.get(f"/api/runs/{run_id}").json(),
            http.get("/api/health").json(),
        ):
            assert "run-only-test" not in json.dumps(data)
        assert app.state.run_settings.pop(run_id).gemini_api_key == "run-only-test"


def test_api_rejects_live_before_creating_workspace(tmp_path):
    store = MemoryStore()
    with TestClient(create_app(config(data_dir=tmp_path), store, False)) as http:
        response = http.post(
            "/api/runs",
            files={"file": ("demo.zip", zip_bytes())},
            data={
                "model_provider": "gemini",
                "model_api_key": "run-only-test",
                "model_name": "gemini-3.8-live",
            },
        )
        assert response.status_code == 422
        assert "run-only-test" not in response.text
        assert not store.runs
        assert not list(tmp_path.iterdir())


def test_worker_uses_run_settings_and_fails_expired_byok(tmp_path, monkeypatch):
    stop = Event()
    store = Mock()
    workspace = tmp_path / "source"
    workspace.mkdir()
    metadata = {"provider": "gemini", "model": "gemini-3.8-flash", "credential_source": "byok"}
    run = {"id": "run", "workspace": str(workspace), "source": {"provider": metadata}}
    store.claim_run.return_value = run
    store.worker_lock_healthy.return_value = True
    store.finish_run.side_effect = lambda *_args: stop.set()
    store.fail_run.side_effect = lambda *_args: stop.set()
    review = Mock(return_value={"status": "reviewed"})
    monkeypatch.setattr("codeproof.engine.run_review", review)
    vault = RunSettingsVault()
    resolved, _ = resolve_provider(config(data_dir=tmp_path), "gemini", api_key="run-only-test")
    vault.put("run", resolved)
    work(store, config(data_dir=tmp_path), stop, vault)
    assert review.call_args.args[1].gemini_api_key == "run-only-test"
    assert store.finish_run.call_args.args[1]["provider"] == metadata
    assert vault.pop("run") is None
    stop.clear()
    review.reset_mock()
    work(store, config(data_dir=tmp_path, llm_api_key="env-test"), stop, vault)
    review.assert_not_called()
    assert "expired" in store.fail_run.call_args.args[1]


def test_failed_run_releases_byok_before_idle_poll(tmp_path, monkeypatch):
    stop = Event()
    store = Mock()
    store.worker_lock_healthy.return_value = True
    workspace = tmp_path / "source"
    workspace.mkdir()
    vault = RunSettingsVault()
    resolved, metadata = resolve_provider(config(data_dir=tmp_path), "openai", api_key="run-test")
    reference = weakref.ref(resolved)
    vault.put("run", resolved)
    del resolved
    run = {"id": "run", "workspace": str(workspace), "source": {"provider": metadata}}
    claimed = False

    def claim():
        nonlocal claimed
        if not claimed:
            claimed = True
            return run
        assert reference() is None, "Failed provider key retained while worker is idle"
        stop.set()
        return None

    def fail_review(_workspace, run_config, **_kwargs):
        assert run_config.llm_api_key == "run-test"
        raise ValueError("test failure")

    store.claim_run.side_effect = claim
    monkeypatch.setattr("codeproof.engine.run_review", fail_review)
    work(store, config(data_dir=tmp_path), stop, vault)
    assert stop.is_set(), "Worker stopped before proving failed-run credential cleanup"
    store.fail_run.assert_called_once()
