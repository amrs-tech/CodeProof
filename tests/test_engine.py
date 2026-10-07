import json
import os
import stat
import subprocess
import time

import pytest

from codeproof.analyzer import AnalysisError, digest, safe_path
from codeproof.config import Settings
from codeproof.engine import _atomic_write, _guard_candidate, run_review
from codeproof.errors import RunAborted
from codeproof.llm import EditProvider, Proposal, ProposalError
from codeproof.sandbox import run_tests

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


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("app.py", b"def greeting():\n    return f'hello'"),
        ("app.py", b"value = 1"),
        ("source with spaces.py", b"def greeting():\r\n    return f'hello'"),
        ("caf\u00e9.py", "message = f'hello\u2028world'\n".encode()),
        ("app.py", b"message = f'hello\x0cworld'\n"),
    ],
)
def test_report_patch_applies_to_exact_original_bytes(tmp_path, name, source):
    original = tmp_path / "original"
    reviewed = tmp_path / "reviewed"
    original.mkdir()
    reviewed.mkdir()
    (original / name).write_bytes(source)
    (reviewed / name).write_bytes(source)
    report = run_review(reviewed, Settings(_env_file=None))
    assert report["status"] == "improved", report
    patch_file = tmp_path / "accepted.patch"
    patch_file.write_bytes(report["patch"].encode("utf-8"))
    subprocess.run(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "apply",
            "--check",
            "--whitespace=nowarn",
            str(patch_file),
        ],
        cwd=original,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-c", "core.autocrlf=false", "apply", "--whitespace=nowarn", str(patch_file)],
        cwd=original,
        capture_output=True,
        check=True,
    )
    assert (original / name).read_bytes() == (reviewed / name).read_bytes()
    if not source.endswith(b"\n"):
        assert "\\ No newline at end of file\n" in report["patch"]


def test_atomic_replace_failure_preserves_original_and_removes_temporary_file(
    tmp_path, monkeypatch
):
    source_repo(tmp_path)

    def failed_replace(*_args):
        raise OSError("Replacement failed")

    monkeypatch.setattr("codeproof.engine.os.replace", failed_replace)
    with pytest.raises(OSError, match="Replacement failed"):
        _atomic_write(tmp_path, "app.py", FIXED, 1024)
    assert (tmp_path / "app.py").read_bytes() == FRAGILE
    assert not list(tmp_path.glob(".codeproof-*.tmp"))


def test_atomic_temporary_write_failure_never_truncates_original(tmp_path, monkeypatch):
    source_repo(tmp_path)

    def failed_sync(*_args):
        raise OSError("Storage is full")

    monkeypatch.setattr("codeproof.engine.os.fsync", failed_sync)
    with pytest.raises(OSError, match="Storage is full"):
        _atomic_write(tmp_path, "app.py", FIXED, 1024)
    assert (tmp_path / "app.py").read_bytes() == FRAGILE
    assert not list(tmp_path.glob(".codeproof-*.tmp"))


def test_atomic_replacement_preserves_source_permissions(tmp_path):
    source_repo(tmp_path)
    (tmp_path / "app.py").chmod(0o644)
    original_mode = stat.S_IMODE((tmp_path / "app.py").stat().st_mode)
    _atomic_write(tmp_path, "app.py", FIXED, 1024)
    assert (tmp_path / "app.py").read_bytes() == FIXED
    assert stat.S_IMODE((tmp_path / "app.py").stat().st_mode) == original_mode


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


def test_budget_stop_reports_all_occurrences_resolved_by_one_fix(tmp_path):
    source_repo(tmp_path, b'value = f"hello"\nother = f"world"\n')
    report = run_review(tmp_path, Settings(_env_file=None, max_total_attempts=1))
    assert report["metrics"]["attempts"] == 1
    assert len(report["findings"]) == 2
    assert all(finding["status"] == "resolved" for finding in report["findings"])


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
    # Add a trusted regression AFTER admission, then prove the actual default-sharing bug.
    regression = docker_behavior_repo / "tests" / "test_independent_defaults.py"
    regression.write_text(
        "import unittest\nfrom app import append_item\n\n"
        "class IndependentDefaultTests(unittest.TestCase):\n"
        "    def test_each_default_call_has_its_own_list(self):\n"
        "        first = append_item(1)\n"
        "        second = append_item(2)\n"
        "        self.assertEqual(first, [1])\n"
        "        self.assertEqual(second, [2])\n"
        "        self.assertIsNot(first, second)\n",
        encoding="utf-8",
    )
    regression.chmod(0o644)
    accepted_source = (docker_behavior_repo / "app.py").read_bytes()
    try:
        improved_regression = run_tests(
            docker_behavior_repo, docker_config(), time.monotonic() + 30
        )
        assert improved_regression["passed"] and improved_regression["tests"] == 2, (
            improved_regression
        )
        (docker_behavior_repo / "app.py").write_bytes(FRAGILE)
        original_regression = run_tests(
            docker_behavior_repo, docker_config(), time.monotonic() + 30
        )
        assert not original_regression["passed"] and original_regression["failures"] == 1, (
            original_regression
        )
    finally:
        (docker_behavior_repo / "app.py").write_bytes(accepted_source)
    assert (docker_behavior_repo / "app.py").read_bytes() == FIXED


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
