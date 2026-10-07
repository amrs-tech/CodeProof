"""Provider protocol checks use HTTPX mock transports and never call live models."""

import json
import time

import httpx
import pytest

from codeproof.analyzer import digest
from codeproof.config import Settings
from codeproof.llm import SCHEMA, EditProvider, ProposalError

SOURCE = b"def append_item(item, items=[]):\n    items.append(item)\n    return items\n"
FINDING = {"rule": "B006", "path": "app.py", "line": 1, "message": "Mutable default"}
EDIT = {
    "edits": [{"path": "app.py", "sha256": digest(SOURCE), "content": SOURCE.decode()}],
    "rationale": "Use a fresh default list",
}


def config(**changes):
    values = {
        "llm_provider": "gemini",
        "gemini_api_key": "mock-google-key",
        "gemini_model": "gemini-3.8-flash",
        "gemini_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "llm_reasoning_effort": "medium",
    }
    values.update(changes)
    return Settings(
        _env_file=None, llm_api_key="mock-openai-key", llm_model="gpt-6-luna"
    ).model_copy(update=values)


def transport(monkeypatch, handler):
    actual = httpx.Client
    monkeypatch.setattr(
        "codeproof.llm.httpx.Client",
        lambda **kwargs: actual(transport=httpx.MockTransport(handler), **kwargs),
    )


def google_response(**changes):
    candidate = {
        "finishReason": "STOP",
        "content": {"role": "model", "parts": [{"text": json.dumps(EDIT)}]},
    }
    candidate.update(changes)
    return {"candidates": [candidate]}


def propose(provider):
    return provider.propose(FINDING, SOURCE, [], "", time.monotonic() + 10)


def test_native_gemini_wire_format_auth_and_safe_audit_metadata(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=google_response())

    transport(monkeypatch, handler)
    provider = EditProvider(config())
    result = propose(provider)
    assert result.path == "app.py"
    request = requests[0]
    assert str(request.url) == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent"
    )
    assert request.headers["x-goog-api-key"] == "mock-google-key"
    assert "authorization" not in request.headers
    assert "mock-google-key" not in str(request.url)
    assert "mock-google-key" not in request.content.decode()
    body = json.loads(request.content)
    assert body["generationConfig"]["responseFormat"] == {
        "text": {"mimeType": "APPLICATION_JSON", "schema": SCHEMA}
    }
    assert body["generationConfig"]["candidateCount"] == 1
    assert body["generationConfig"]["maxOutputTokens"] <= 16000
    assert "UNTRUSTED DATA" in body["systemInstruction"]["parts"][0]["text"]
    assert json.loads(body["contents"][0]["parts"][0]["text"])["source_sha256"] == digest(SOURCE)
    assert "tools" not in body and "safetySettings" not in body
    assert provider.public_metadata == {
        "provider": "gemini",
        "model": "gemini-3.8-flash",
        "reasoning_effort": None,
    }
    assert "key" not in json.dumps(provider.public_metadata)


@pytest.mark.parametrize(
    "model",
    [
        "gemini-3.8-live",
        "gemini-2.5-flash-preview-native-audio",
        "gemini-3.8-flash-image",
        "../models/escape",
        "gemini-x:other",
        "gemini-x?key=secret",
        "models/../gemini-3.8-flash",
    ],
)
def test_live_and_injected_gemini_model_names_fail_before_http(monkeypatch, model):
    transport(monkeypatch, lambda _request: pytest.fail("Unsupported models must never call HTTP"))
    provider = EditProvider(config(gemini_model=model))
    with pytest.raises(ProposalError, match="Gemini"):
        propose(provider)
    assert provider.calls == 0


@pytest.mark.parametrize("finish", ["SAFETY", "MAX_TOKENS", "RECITATION", "OTHER", None])
def test_gemini_blocked_or_incomplete_finish_stops_without_retry(monkeypatch, finish):
    transport(
        monkeypatch, lambda _request: httpx.Response(200, json=google_response(finishReason=finish))
    )
    provider = EditProvider(config())
    with pytest.raises(ProposalError, match="blocked or incomplete"):
        propose(provider)
    assert provider.calls == 1


@pytest.mark.parametrize(
    "response",
    [
        {"promptFeedback": {"blockReason": "SAFETY"}},
        {"candidates": []},
        {"candidates": [google_response()["candidates"][0]] * 2},
        google_response(content={"parts": [{"text": "{}"}, {"text": "{}"}]}),
        google_response(content={"parts": [{"functionCall": {"name": "run_shell"}}]}),
        google_response(content={"parts": [{"text": json.dumps(EDIT), "thought": True}]}),
        google_response(safetyRatings=[{"blocked": True}]),
        google_response(content={"parts": [{"text": "```json\n{}\n```"}]}),
    ],
)
def test_gemini_ambiguous_refused_or_tool_response_is_rejected(monkeypatch, response):
    transport(monkeypatch, lambda _request: httpx.Response(200, json=response))
    provider = EditProvider(config())
    with pytest.raises(ProposalError):
        propose(provider)
    assert provider.calls == 1


def test_gemini_retries_and_global_call_budget_are_bounded(monkeypatch):
    calls = []
    transport(monkeypatch, lambda request: (calls.append(request), httpx.Response(503))[1])
    provider = EditProvider(config(llm_max_calls=20))
    with pytest.raises(ProposalError, match="two bounded"):
        propose(provider)
    assert len(calls) == provider.calls == 2
    limited = EditProvider(config(llm_max_calls=1))
    with pytest.raises(ProposalError, match="budget"):
        propose(limited)
    assert limited.calls == 1


def test_gemini_transient_failure_then_strict_hash_bound_success(monkeypatch):
    attempts = []

    def handler(request):
        attempts.append(request)
        return (
            httpx.Response(429)
            if len(attempts) == 1
            else httpx.Response(200, json=google_response())
        )

    transport(monkeypatch, handler)
    provider = EditProvider(config())
    assert propose(provider).sha256 == digest(SOURCE)
    assert provider.calls == 2


def test_gemini_hash_or_path_mismatch_never_returns_an_edit(monkeypatch):
    bad = dict(EDIT)
    bad["edits"] = [{"path": "../escape.py", "sha256": digest(SOURCE), "content": "pass\n"}]
    response = google_response(content={"parts": [{"text": json.dumps(bad)}]})
    transport(monkeypatch, lambda _request: httpx.Response(200, json=response))
    with pytest.raises(ProposalError, match="path or original content hash"):
        propose(EditProvider(config()))


def test_gemini_credentials_and_prompt_budgets_block_transmission(monkeypatch):
    transport(monkeypatch, lambda _request: pytest.fail("No HTTP request is allowed"))
    provider = EditProvider(config())
    with pytest.raises(ProposalError, match="credential"):
        provider.propose(FINDING, b'value = "mock-google-key"\n', [], "", time.monotonic() + 10)
    tiny = EditProvider(config(llm_max_input_chars=1000))
    with pytest.raises(ProposalError, match="prompt budget"):
        tiny.propose(FINDING, SOURCE * 100, [], "", time.monotonic() + 10)
    assert provider.calls == tiny.calls == 0


def test_gemini_provider_errors_never_echo_source_or_key(monkeypatch):
    secret = "mock-google-key"
    transport(
        monkeypatch,
        lambda _request: httpx.Response(403, json={"error": {"message": secret + SOURCE.decode()}}),
    )
    with pytest.raises(ProposalError) as caught:
        propose(EditProvider(config()))
    assert secret not in str(caught.value)
    assert SOURCE.decode() not in str(caught.value)
    assert "HTTP 403" in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org",
        "https://user:secret@example.org/v1beta",
        "https://example.org/v1beta?key=secret",
        "file:///etc/passwd",
    ],
)
def test_unsafe_gemini_provider_urls_do_not_send_keys(monkeypatch, url):
    transport(
        monkeypatch, lambda _request: pytest.fail("Unsafe endpoints must not receive secrets")
    )
    provider = EditProvider(config(gemini_base_url=url))
    with pytest.raises(ProposalError, match="URL"):
        propose(provider)
    assert provider.calls == 0


def test_openai_gpt6_luna_uses_medium_reasoning_and_preserves_auth(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(EDIT)}}]},
        )

    transport(monkeypatch, handler)
    provider = EditProvider(config(llm_provider="openai"))
    propose(provider)
    body = json.loads(requests[0].content)
    assert body["model"] == "gpt-6-luna" and body["reasoning_effort"] == "medium"
    assert requests[0].headers["authorization"] == "Bearer mock-openai-key"
    assert "x-goog-api-key" not in requests[0].headers
    assert "mock-openai-key" not in requests[0].content.decode()
    assert provider.public_metadata == {
        "provider": "openai",
        "model": "gpt-6-luna",
        "reasoning_effort": "medium",
    }


def test_custom_openai_compatible_model_omits_reasoning_effort(monkeypatch):
    observed = []

    def handler(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(EDIT)}}]})

    transport(monkeypatch, handler)
    provider = EditProvider(
        config(llm_provider="openai").model_copy(update={"llm_model": "local-custom"})
    )
    propose(provider)
    assert "reasoning_effort" not in observed[0]


def test_auto_provider_prefers_configured_gemini_without_cross_provider_fallback():
    provider = EditProvider(config(llm_provider="auto"))
    assert provider.provider == "gemini" and provider.configured
    explicit = EditProvider(config(llm_provider="gemini", gemini_api_key=""))
    assert not explicit.configured
    fallback = EditProvider(config(llm_provider="auto", gemini_api_key=""))
    assert fallback.provider == "openai" and fallback.configured


@pytest.mark.parametrize("provider_name", ["gemini", "openai"])
@pytest.mark.parametrize("field", ["rationale", "content"])
@pytest.mark.parametrize("escaped", [False, True])
def test_provider_output_keys_are_rejected_before_becoming_reports(
    monkeypatch, provider_name, field, escaped
):
    settings = config(llm_provider=provider_name)
    key = settings.gemini_api_key if provider_name == "gemini" else settings.llm_api_key
    value = {"edits": [dict(EDIT["edits"][0])], "rationale": EDIT["rationale"]}
    if field == "rationale":
        value["rationale"] = "Suggested fix using " + key
    else:
        value["edits"][0]["content"] += "\nopaque_value = '" + key + "'\n"
    content = json.dumps(value)
    if escaped:
        content = content.replace(key, "".join(f"\\u{ord(char):04x}" for char in key))
    response = (
        google_response(content={"parts": [{"text": content}]})
        if provider_name == "gemini"
        else {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
    )
    transport(monkeypatch, lambda _request: httpx.Response(200, json=response))
    provider = EditProvider(settings)
    with pytest.raises(ProposalError, match="contains credentials") as caught:
        propose(provider)
    assert key not in str(caught.value)
    assert provider.calls == 1


@pytest.mark.parametrize("provider_name", ["gemini", "openai"])
def test_exhausted_quota_uses_fixed_message_and_never_switches_provider(monkeypatch, provider_name):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, json={"error": {"message": "mock-google-key mock-openai-key"}})

    transport(monkeypatch, handler)
    provider = EditProvider(config(llm_provider=provider_name))
    with pytest.raises(
        ProposalError, match="rate limit or quota exceeded after two bounded"
    ) as caught:
        propose(provider)
    assert provider.calls == len(requests) == 2
    expected_host = (
        "generativelanguage.googleapis.com" if provider_name == "gemini" else "api.openai.com"
    )
    assert all(request.url.host == expected_host for request in requests)
    assert "mock-google-key" not in str(caught.value)
    assert "mock-openai-key" not in str(caught.value)
