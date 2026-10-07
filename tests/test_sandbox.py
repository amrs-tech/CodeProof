import io
import json
import os
import subprocess
import time

import pytest

from codeproof.config import Settings
from codeproof.sandbox import RUNNER, run_tests, sandbox_command


def test_docker_security_controls_and_fixed_command(tmp_path):
    command = sandbox_command(tmp_path, Settings(_env_file=None), "codeproof-test")
    for flag in (
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--memory=256m",
        "--cpus=1",
        "--pids-limit=64",
        "--user=65534:65534",
        "--pull=never",
    ):
        assert flag in command
    assert f"type=bind,src={tmp_path.resolve()},dst=/workspace,readonly" in command
    assert command[-1] == RUNNER
    assert "-I" in command
    assert "unittest.defaultTestLoader.discover" in RUNNER
    assert all("API_KEY" not in item for item in command)


def test_disabled_sandbox_never_starts_repository_code(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "codeproof.sandbox.subprocess.Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("No process allowed")),
    )
    result = run_tests(tmp_path, Settings(_env_file=None), time.monotonic() + 10)
    assert not result["passed"]
    assert "disabled" in result["details"]


def test_zero_tests_and_missing_docker_fail_closed(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    cleanup = []

    def missing(*_a, **_k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr("codeproof.sandbox.subprocess.Popen", missing)
    monkeypatch.setattr("codeproof.sandbox.subprocess.run", lambda args, **_k: cleanup.append(args))
    result = run_tests(
        tmp_path, Settings(_env_file=None, sandbox_enabled=True), time.monotonic() + 10
    )
    assert not result["passed"]
    assert "unavailable" in result["details"]
    assert cleanup[0][:3] == ["docker", "rm", "--force"]


def test_passed_test_metadata_is_reported_and_container_cleaned(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()

    class Process:
        stdout = io.BytesIO(
            b'CODEPROOF_TEST_RESULT={"tests":2,"failures":0,"errors":0,"skipped":0}\n'
        )

        def wait(self, timeout):
            return 0

    cleanup = []
    monkeypatch.setattr("codeproof.sandbox.subprocess.Popen", lambda *_a, **_k: Process())
    monkeypatch.setattr("codeproof.sandbox.subprocess.run", lambda args, **_k: cleanup.append(args))
    result = run_tests(
        tmp_path, Settings(_env_file=None, sandbox_enabled=True), time.monotonic() + 10
    )
    assert result["passed"] and result["tests"] == 2
    assert len(cleanup) == 1


def test_timeout_kills_client_and_removes_container(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    events = []

    class Process:
        stdout = io.BytesIO(b"")

        def wait(self, timeout):
            if not events:
                raise subprocess.TimeoutExpired("docker", timeout)
            return -1

        def kill(self):
            events.append("killed")

    monkeypatch.setattr("codeproof.sandbox.subprocess.Popen", lambda *_a, **_k: Process())
    monkeypatch.setattr(
        "codeproof.sandbox.subprocess.run", lambda *_a, **_k: events.append("removed")
    )
    result = run_tests(
        tmp_path, Settings(_env_file=None, sandbox_enabled=True), time.monotonic() + 10
    )
    assert not result["passed"]
    assert events == ["killed", "removed"]
    assert "bounded execution" in result["details"]


@pytest.mark.parametrize(
    "counts",
    [
        {"tests": 2, "failures": 1, "errors": 0, "skipped": 0},
        {"tests": 2, "failures": 0, "errors": 1, "skipped": 0},
        {"tests": 2, "failures": 0, "errors": 0, "skipped": -1},
        {"tests": True, "failures": 0, "errors": 0, "skipped": 0},
        {"tests": "2", "failures": 0, "errors": 0, "skipped": 0},
        {"tests": 2, "failures": 0, "errors": 0, "skipped": 2},
    ],
)
def test_invalid_or_failed_sandbox_counts_cannot_pass(tmp_path, monkeypatch, counts):
    (tmp_path / "tests").mkdir()

    class Process:
        stdout = io.BytesIO(("CODEPROOF_TEST_RESULT=" + json.dumps(counts) + "\n").encode())

        def wait(self, timeout):
            return 0

    monkeypatch.setattr("codeproof.sandbox.subprocess.Popen", lambda *_a, **_k: Process())
    monkeypatch.setattr("codeproof.sandbox.subprocess.run", lambda *_a, **_k: None)
    result = run_tests(
        tmp_path, Settings(_env_file=None, sandbox_enabled=True), time.monotonic() + 10
    )
    assert not result["passed"]


@pytest.mark.sandbox
@pytest.mark.skipif(
    os.environ.get("CODEPROOF_TEST_SANDBOX") != "1",
    reason="Set CODEPROOF_TEST_SANDBOX=1 with a built local Docker sandbox image",
)
def test_real_sandbox_has_no_network_write_access_or_host_secret(tmp_path, monkeypatch):
    tmp_path.chmod(0o755)
    (tmp_path / "tests").mkdir(mode=0o755)
    (tmp_path / "tests" / "test_security.py").write_text(
        "import os,socket,unittest\nfrom pathlib import Path\n\n"
        "class SecurityTests(unittest.TestCase):\n"
        "    def test_source_is_read_only(self):\n"
        "        with self.assertRaises(OSError):\n"
        "            Path('/workspace/injected.py').write_text('bad')\n"
        "    def test_network_is_disabled(self):\n"
        "        connection = socket.socket()\n"
        "        connection.settimeout(0.5)\n"
        "        try:\n"
        "            with self.assertRaises(OSError):\n"
        "                connection.connect(('1.1.1.1', 53))\n"
        "        finally:\n"
        "            connection.close()\n"
        "    def test_host_secret_is_not_inherited(self):\n"
        "        self.assertNotIn('CODEPROOF_HOST_SECRET_SENTINEL', os.environ)\n"
        "    def test_no_linux_capabilities(self):\n"
        "        status = Path('/proc/self/status').read_text()\n"
        "        cap_eff = next(line for line in status.splitlines() if line.startswith('CapEff:'))\n"
        "        self.assertEqual(int(cap_eff.split()[1], 16), 0)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_security.py").chmod(0o644)
    monkeypatch.setenv("CODEPROOF_HOST_SECRET_SENTINEL", "must-stay-on-host")
    settings = Settings(
        _env_file=None,
        sandbox_enabled=True,
        sandbox_image=os.environ.get("CODEPROOF_SANDBOX_IMAGE", "codeproof-sandbox:local"),
    )
    result = run_tests(tmp_path, settings, time.monotonic() + 30)
    assert result["passed"] and result["tests"] == 4, result
    assert not (tmp_path / "injected.py").exists()


@pytest.mark.sandbox
@pytest.mark.skipif(
    os.environ.get("CODEPROOF_TEST_SANDBOX") != "1",
    reason="Set CODEPROOF_TEST_SANDBOX=1 with a built local Docker sandbox image",
)
def test_real_sandbox_timeout_is_bounded_and_removes_container(tmp_path):
    tmp_path.chmod(0o755)
    (tmp_path / "tests").mkdir(mode=0o755)
    (tmp_path / "tests" / "test_timeout.py").write_text(
        "import time,unittest\n\n"
        "class TimeoutTest(unittest.TestCase):\n"
        "    def test_hanging_code(self):\n"
        "        time.sleep(30)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_timeout.py").chmod(0o644)
    settings = Settings(
        _env_file=None,
        sandbox_enabled=True,
        sandbox_timeout=1,
        sandbox_image=os.environ.get("CODEPROOF_SANDBOX_IMAGE", "codeproof-sandbox:local"),
    )
    started = time.monotonic()
    result = run_tests(tmp_path, settings, started + 10)
    assert not result["passed"] and "bounded execution" in result["details"], result
    assert time.monotonic() - started < 10
    inspect = subprocess.run(
        ["docker", "ps", "--filter", "name=codeproof-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert not inspect.stdout.strip(), inspect.stdout
