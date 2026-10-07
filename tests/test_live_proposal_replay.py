"""Replay actual paid-provider proposals through guarded LangGraph and real Docker tests.

No credentials or live network requests are used by these tests. Capture evidence is
checked in; runtime acceptance still requires the same sandbox as every model edit.
"""

import json
import os
import time
from pathlib import Path

import httpx
import pytest

from codeproof.config import Settings
from codeproof.engine import run_review
from codeproof.sandbox import run_tests


@pytest.mark.sandbox
@pytest.mark.skipif(
    os.environ.get("CODEPROOF_TEST_SANDBOX") != "1", reason="Docker sandbox required"
)
@pytest.mark.parametrize("fixture_name", ["openai", "fallback"])
def test_live_model_proposal_replay_passes_isolated_behavioral_checks(
    tmp_path, monkeypatch, fixture_name
):
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/live_proposals" / f"{fixture_name}.json").read_text()
    )
    tmp_path.chmod(0o755)
    (tmp_path / "app.py").write_bytes(fixture["source"].encode("utf-8"))
    (tmp_path / "app.py").chmod(0o644)
    (tmp_path / "tests").mkdir(mode=0o755)
    tests = tmp_path / "tests/test_app.py"
    tests.write_text(
        "import unittest\nfrom app import append_item\n\n"
        "class ExistingTests(unittest.TestCase):\n"
        "    def test_explicit_collection(self):\n"
        "        items = []\n"
        "        self.assertIs(append_item(1, items), items)\n"
        "        self.assertEqual(items, [1])\n"
    )
    tests.chmod(0o644)
    settings = Settings(
        _env_file=None,
        sandbox_enabled=True,
        llm_api_key="fixture-openai",
        llm_model="gpt-6-luna",
        llm_provider="gemini" if fixture_name == "fallback" else "openai",
        gemini_api_key="fixture-gemini" if fixture_name == "fallback" else "",
        gemini_model="gemini-3.8-flash" if fixture_name == "fallback" else "",
        llm_fallback_enabled=fixture_name == "fallback",
    )
    requests = []
    proposal = fixture["proposal"]
    structured = {
        "edits": [{key: proposal[key] for key in ("path", "sha256", "content")}],
        "rationale": proposal["rationale"],
    }

    def response(request):
        requests.append(request.url.host)
        if request.url.host == "generativelanguage.googleapis.com":
            raise httpx.ReadTimeout("Simulated observed Gemini timeout", request=request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps(structured)}}
                ]
            },
        )

    original_client = httpx.Client
    monkeypatch.setattr(
        "codeproof.llm.httpx.Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(response), **kwargs),
    )
    report = run_review(tmp_path, settings)
    assert report["status"] == "improved", report
    assert report["metrics"]["accepted_changes"] == 1
    assert report["provider"]["provider"] == "openai"
    assert report["metrics"]["provider_calls"] == fixture["provider_calls"]
    assert (tmp_path / "app.py").read_bytes() == proposal["content"].encode("utf-8")
    if fixture_name == "fallback":
        assert (
            report["provider"]["fallback_history"]
            == fixture["provider_metadata"]["fallback_history"]
        )
        assert requests == ["generativelanguage.googleapis.com"] * 2 + ["api.openai.com"]
    else:
        assert requests == ["api.openai.com"]
    assert [item["phase"] for item in report["validation"] if item["kind"] == "sandbox_tests"] == [
        "baseline",
        "candidate",
    ]
    # The trusted extra regression establishes the improvement separately from existing tests.
    tests.write_text(
        tests.read_text() + "    def test_independent_default_calls(self):\n"
        "        self.assertEqual(append_item('first'), ['first'])\n"
        "        self.assertEqual(append_item('second'), ['second'])\n"
    )
    accepted_source = (tmp_path / "app.py").read_bytes()
    try:
        improved = run_tests(tmp_path, settings, time.monotonic() + 30)
        assert improved["passed"] and improved["tests"] == 2, improved
        (tmp_path / "app.py").write_bytes(fixture["source"].encode("utf-8"))
        original = run_tests(tmp_path, settings, time.monotonic() + 30)
        assert (
            not original["passed"]
            and original["tests"] == 2
            and original["failures"] == 1
            and original["errors"] == 0
        ), original
    finally:
        (tmp_path / "app.py").write_bytes(accepted_source)
    assert (tmp_path / "app.py").read_bytes() == proposal["content"].encode("utf-8")
