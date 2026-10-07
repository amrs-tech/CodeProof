import json
import os

import pytest

from codeproof.analyzer import AnalysisError, digest, safe_path
from codeproof.config import Settings
from codeproof.engine import _guard_candidate, run_review
from codeproof.errors import RunAborted
from codeproof.llm import EditProvider, Proposal, ProposalError

FRAGILE = b"def append_item(item, items=[]):\n    items.append(item)\n    return items\n"
FIXED = (
    b"def append_item(item, items=None):\n    if items is None:\n        items = []\n"
    b"    items.append(item)\n    return items\n"
)


def configured(**overrides):
    return Settings(
        _env_file=None,
        llm_api_key="test-local",
        llm_model="test-model",
        sandbox_enabled=True,
        **overrides,
    )


def source_repo(tmp_path, source=FRAGILE):
    (tmp_path / "app.py").write_bytes(source)
    (tmp_path / "tests").mkdir()
    return tmp_path


def passing_tests(*_args):
    return {"kind": "sandbox_tests", "passed": True, "tests": 2, "details": "2 tests passed"}


def proposal(content):
    return Proposal("app.py", digest(FRAGILE), content.decode(), "Remove a shared mutable default")


def test_real_safe_fix_returns_patch_and_honest_validation(tmp_path):
    source_repo(tmp_path, b"def greeting():\n    return f'hello'\n")
    report = run_review(tmp_path, Settings(_env_file=None))
    assert report["status"] == "improved"
    assert report["metrics"]["accepted_changes"] == 1
    assert (tmp_path / "app.py").read_text() == "def greeting():\n    return 'hello'\n"
    assert "-    return f'hello'" in report["patch"]
    assert not any(item["kind"] == "sandbox_tests" for item in report["validation"])
    assert any("do not prove behavioral" in text for text in report["limitations"])
    json.dumps(report)


def test_successful_model_fix_requires_baseline_and_candidate_tests(tmp_path, monkeypatch):
    source_repo(tmp_path)
    calls = []

    def sandbox(workspace, *_args):
        calls.append((workspace / "app.py").read_bytes())
        return passing_tests()

    monkeypatch.setattr("codeproof.engine.run_tests", sandbox)
    monkeypatch.setattr(EditProvider, "propose", lambda *_args: proposal(FIXED))
    report = run_review(tmp_path, configured())
    assert report["status"] == "improved"
    assert calls == [FRAGILE, FIXED]
    assert (tmp_path / "app.py").read_bytes() == FIXED
    assert report["findings"][0]["rule"] == "B006"
    assert report["findings"][0]["status"] == "resolved"
    assert [item["phase"] for item in report["validation"] if item["kind"] == "sandbox_tests"] == [
        "baseline",
        "candidate",
    ]


def test_disabled_sandbox_never_requests_substantive_edit(tmp_path, monkeypatch):
    source_repo(tmp_path)
    monkeypatch.setattr(EditProvider, "propose", lambda *_args: pytest.fail("Model must not run"))
    settings = configured()
    settings.sandbox_enabled = False
    report = run_review(tmp_path, settings)
    assert report["status"] == "reviewed"
    assert report["findings"][0]["status"] == "needs_review"
    assert (tmp_path / "app.py").read_bytes() == FRAGILE


def test_failing_baseline_never_requests_model_edit(tmp_path, monkeypatch):
    source_repo(tmp_path)
    monkeypatch.setattr(
        "codeproof.engine.run_tests",
        lambda *_args: {
            "kind": "sandbox_tests",
            "passed": False,
            "tests": 1,
            "details": "Existing test fails",
        },
    )
    monkeypatch.setattr(EditProvider, "propose", lambda *_args: pytest.fail("Model must not run"))
    report = run_review(tmp_path, configured())
    assert report["metrics"]["attempts"] == 0
    assert report["findings"][0]["status"] == "needs_review"


def test_failed_candidate_rolls_back_and_duplicate_stops(tmp_path, monkeypatch):
    source_repo(tmp_path)
    calls = 0

    def sandbox(*_args):
        nonlocal calls
        calls += 1
        return {
            "kind": "sandbox_tests",
            "passed": calls == 1,
            "tests": 2,
            "details": "Candidate behavior differs",
        }

    monkeypatch.setattr("codeproof.engine.run_tests", sandbox)
    monkeypatch.setattr(EditProvider, "propose", lambda *_args: proposal(FIXED))
    report = run_review(tmp_path, configured(max_attempts=5))
    assert report["status"] == "reviewed"
    assert (tmp_path / "app.py").read_bytes() == FRAGILE
    assert report["actions"][0]["rolled_back"]
    assert "duplicate" in report["actions"][-1]["rationale"]
    assert report["metrics"]["attempts"] == 2
    assert report["patch"] == ""


def test_distinct_invalid_candidates_stop_at_finding_retry_limit(tmp_path, monkeypatch):
    source_repo(tmp_path)
    sequence = iter([b"def invalid_one(:\n", b"def invalid_two(:\n", b"def invalid_three(:\n"])
    monkeypatch.setattr("codeproof.engine.run_tests", passing_tests)
    monkeypatch.setattr(EditProvider, "propose", lambda *_args: proposal(next(sequence)))
    report = run_review(tmp_path, configured(max_attempts=3))
    assert report["findings"][0]["attempts"] == 3
    assert report["findings"][0]["status"] == "rejected"
    assert (tmp_path / "app.py").read_bytes() == FRAGILE


def test_global_attempt_bound_stops_the_graph(tmp_path, monkeypatch):
    source_repo(tmp_path)
    monkeypatch.setattr("codeproof.engine.run_tests", passing_tests)
    monkeypatch.setattr(EditProvider, "propose", lambda *_args: proposal(b"def invalid(:\n"))
    report = run_review(tmp_path, configured(max_total_attempts=1))
    assert report["status"] == "stopped"
    assert report["metrics"]["attempts"] == 1
    assert "Global" in report["stop_reason"]


def test_new_static_issue_rolls_back(tmp_path, monkeypatch):
    source_repo(tmp_path)
    candidate = FIXED.replace(b"    return items", b"    return missing_variable")
    monkeypatch.setattr("codeproof.engine.run_tests", passing_tests)
    monkeypatch.setattr(EditProvider, "propose", lambda *_args: proposal(candidate))
    report = run_review(tmp_path, configured())
    assert report["metrics"]["accepted_changes"] == 0
    assert report["actions"][0]["rolled_back"]
    assert "new static" in report["actions"][0]["rationale"]
    assert (tmp_path / "app.py").read_bytes() == FRAGILE


def test_provider_failure_does_not_retry_forever(tmp_path, monkeypatch):
    source_repo(tmp_path)
    monkeypatch.setattr("codeproof.engine.run_tests", passing_tests)

    def failure(*_args):
        raise ProposalError("Provider unavailable after two bounded attempts")

    monkeypatch.setattr(EditProvider, "propose", failure)
    report = run_review(tmp_path, configured())
    assert report["metrics"]["attempts"] == 1
    assert report["findings"][0]["status"] == "rejected"


def test_invalid_baseline_is_stopped_without_writing(tmp_path):
    source_repo(tmp_path, b"def broken(:\n")
    report = run_review(tmp_path, Settings(_env_file=None))
    assert report["status"] == "stopped"
    assert report["metrics"]["attempts"] == 0
    assert report["validation"][0]["passed"] is False


@pytest.mark.parametrize(
    "path",
    [
        "../outside.py",
        "/app.py",
        "C:/app.py",
        "tests/../app.py",
        "app.txt",
        ".git/app.py",
        "app\\evil.py",
    ],
)
def test_path_traversal_and_non_python_guards(tmp_path, path):
    source_repo(tmp_path)
    with pytest.raises((AnalysisError, FileNotFoundError)):
        safe_path(tmp_path, path)


def test_suppression_and_function_deletion_guards():
    finding = {"path": "app.py", "rule": "B006"}
    with pytest.raises(ProposalError, match="suppression"):
        _guard_candidate(
            FRAGILE,
            FRAGILE.replace(b"items=[]):", b"items=[]):  # noqa: B006"),
            finding,
            Settings(_env_file=None),
        )
    with pytest.raises(ProposalError, match="removes"):
        _guard_candidate(FRAGILE, b"answer = 1\n", finding, Settings(_env_file=None))


def test_protected_test_sources_are_review_only(tmp_path, monkeypatch):
    source_repo(tmp_path, b"value = 1\n")
    (tmp_path / "tests" / "test_fragile.py").write_bytes(FRAGILE)
    monkeypatch.setattr("codeproof.engine.run_tests", passing_tests)
    monkeypatch.setattr(EditProvider, "propose", lambda *_args: pytest.fail("Do not edit tests"))
    report = run_review(tmp_path, configured())
    finding = next(item for item in report["findings"] if item["rule"] == "B006")
    assert finding["status"] == "needs_review"


def test_unexpected_validation_exception_always_rolls_back(tmp_path, monkeypatch):
    source_repo(tmp_path)
    calls = 0

    def sandbox(*_args):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("unexpected sandbox integration exception")
        return passing_tests()

    monkeypatch.setattr("codeproof.engine.run_tests", sandbox)
    monkeypatch.setattr(EditProvider, "propose", lambda *_args: proposal(FIXED))
    report = run_review(tmp_path, configured())
    assert (tmp_path / "app.py").read_bytes() == FRAGILE
    assert report["actions"][0]["rolled_back"]
    assert "Unexpected validation" in report["actions"][0]["rationale"]


def test_multiple_same_rule_findings_cannot_be_falsely_marked_resolved(tmp_path, monkeypatch):
    doubled = FRAGILE + FRAGILE.replace(b"append_item", b"append_other")
    source_repo(tmp_path, doubled)
    partially_fixed = FIXED + FRAGILE.replace(b"append_item", b"append_other")
    monkeypatch.setattr("codeproof.engine.run_tests", passing_tests)
    monkeypatch.setattr(EditProvider, "propose", lambda *_args: proposal(partially_fixed))
    report = run_review(tmp_path, configured())
    assert report["metrics"]["accepted_changes"] == 0
    assert all(finding["status"] != "resolved" for finding in report["findings"])
    assert (tmp_path / "app.py").read_bytes() == doubled


def test_run_deadline_stops_before_inventory(tmp_path, monkeypatch):
    source_repo(tmp_path)
    real_time = __import__("time").monotonic()
    times = iter([real_time, real_time + 100, real_time + 100])
    monkeypatch.setattr("codeproof.engine.time.monotonic", lambda: next(times, real_time + 100))
    report = run_review(tmp_path, Settings(_env_file=None, max_run_seconds=10))
    assert report["status"] == "stopped"
    assert "time limit" in report["stop_reason"]
    assert (tmp_path / "app.py").read_bytes() == FRAGILE


def test_dynamic_execution_injected_by_model_is_rejected():
    finding = {"path": "app.py", "rule": "B006"}
    injected = FIXED.replace(
        b"    return items", b"    __import__('os').system('echo injected')\n    return items"
    )
    with pytest.raises(ProposalError, match="dynamic execution"):
        _guard_candidate(FRAGILE, injected, finding, Settings(_env_file=None))


def test_worker_ownership_abort_during_candidate_validation_rolls_back(tmp_path, monkeypatch):
    source_repo(tmp_path)
    monkeypatch.setattr("codeproof.engine.run_tests", passing_tests)
    monkeypatch.setattr(EditProvider, "propose", lambda *_args: proposal(FIXED))

    def progress(stage, detail):
        if stage == "validating" and "candidate" in detail:
            raise RunAborted("Worker ownership was lost")

    with pytest.raises(RunAborted, match="ownership"):
        run_review(tmp_path, configured(), progress=progress)
    assert (tmp_path / "app.py").read_bytes() == FRAGILE


@pytest.fixture
def docker_behavior_repo(tmp_path):
    source_repo(tmp_path)
    tmp_path.chmod(0o755)
    (tmp_path / "tests").chmod(0o755)
    (tmp_path / "tests" / "test_app.py").write_text(
        "import unittest\nfrom app import append_item\n\n"
        "class Tests(unittest.TestCase):\n"
        "    def test_explicit_list_behavior(self):\n"
        "        target = [1]\n"
        "        self.assertIs(append_item(2, target), target)\n"
        "        self.assertEqual(target, [1, 2])\n",
        encoding="utf-8",
    )
    for path in tmp_path.rglob("*.py"):
        path.chmod(0o644)
    return tmp_path


def docker_config():
    return configured(
        sandbox_image=os.environ.get("CODEPROOF_SANDBOX_IMAGE", "codeproof-sandbox:local")
    )


@pytest.mark.sandbox
@pytest.mark.skipif(
    os.environ.get("CODEPROOF_TEST_SANDBOX") != "1",
    reason="Set CODEPROOF_TEST_SANDBOX=1 with a built local Docker sandbox image",
)
def test_real_docker_accepts_substantive_fix_after_baseline_and_candidate(
    docker_behavior_repo, monkeypatch
):
    monkeypatch.setattr(EditProvider, "propose", lambda *_args: proposal(FIXED))
    report = run_review(docker_behavior_repo, docker_config())
    assert report["status"] == "improved", report
    assert (docker_behavior_repo / "app.py").read_bytes() == FIXED
    test_results = [entry for entry in report["validation"] if entry["kind"] == "sandbox_tests"]
    assert [entry["phase"] for entry in test_results] == ["baseline", "candidate"]
    assert all(entry["passed"] and entry["tests"] == 1 for entry in test_results)


@pytest.mark.sandbox
@pytest.mark.skipif(
    os.environ.get("CODEPROOF_TEST_SANDBOX") != "1",
    reason="Set CODEPROOF_TEST_SANDBOX=1 with a built local Docker sandbox image",
)
def test_real_docker_rejects_behavior_regression_and_restores_source(
    docker_behavior_repo, monkeypatch
):
    broken_behavior = FIXED.replace(b"items.append(item)", b"items.append(None)")
    monkeypatch.setattr(EditProvider, "propose", lambda *_args: proposal(broken_behavior))
    report = run_review(docker_behavior_repo, docker_config())
    assert report["metrics"]["accepted_changes"] == 0, report
    assert (docker_behavior_repo / "app.py").read_bytes() == FRAGILE
    assert report["actions"][0]["rolled_back"]
    assert any(
        entry["phase"] == "candidate" and not entry["passed"]
        for entry in report["validation"]
        if entry["kind"] == "sandbox_tests"
    )
    assert report["metrics"]["attempts"] == 2  # Duplicate failed candidate stops the loop.
