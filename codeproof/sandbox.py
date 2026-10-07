"""Opt-in Docker validation of untrusted stdlib unittest suites."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from codeproof.config import Settings

RUNNER = (
    "import json,sys,unittest; sys.path.insert(0,'/workspace'); "
    "suite=unittest.defaultTestLoader.discover('/workspace/tests'); "
    "result=unittest.TextTestRunner(verbosity=1).run(suite); "
    "print('CODEPROOF_TEST_RESULT='+json.dumps({'tests':result.testsRun,"
    "'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped)})); "
    "sys.exit(0 if result.wasSuccessful() and result.testsRun>len(result.skipped) else 1)"
)


def sandbox_command(workspace: Path, settings: Settings, name: str) -> list[str]:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]{0,200}", settings.sandbox_image):
        raise ValueError("Sandbox image name is invalid")
    path = str(workspace.resolve(strict=True))
    if "," in path:
        raise ValueError("Sandbox paths containing commas are unsupported")
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--memory=256m",
        "--memory-swap=256m",
        "--cpus=1",
        "--pids-limit=64",
        "--user=65534:65534",
        "--ulimit=nofile=128:128",
        "--ulimit=fsize=16384:16384",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
        "--workdir=/workspace",
        "--mount",
        f"type=bind,src={path},dst=/workspace,readonly",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "PYTHONNOUSERSITE=1",
        "--entrypoint=python",
        settings.sandbox_image,
        "-I",
        "-B",
        "-c",
        RUNNER,
    ]


def run_tests(workspace: Path, settings: Settings, deadline: float) -> dict:
    result = {
        "kind": "sandbox_tests",
        "passed": False,
        "tests": 0,
        "details": "",
        "command": "Fixed stdlib unittest discovery in restricted Docker",
    }
    if not settings.sandbox_enabled:
        result["details"] = "Sandbox execution is disabled; repository code was not executed"
        return result
    if not (workspace / "tests").is_dir():
        result["details"] = "A tests/ directory with stdlib unittest tests is required"
        return result
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        result["details"] = "Run time limit reached before sandbox validation"
        return result
    name = "codeproof-" + uuid.uuid4().hex
    try:
        command = sandbox_command(workspace, settings, name)
        # Continuously drain output into a bounded tail: neither memory nor disk grows with logs.
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False
        )
        tail = bytearray()
        lock = threading.Lock()

        def drain() -> None:
            assert process.stdout is not None
            while chunk := process.stdout.read1(8192):
                with lock:
                    tail.extend(chunk)
                    del tail[:-10000]

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            returncode = process.wait(timeout=min(float(settings.sandbox_timeout), remaining))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            result["details"] = "Sandbox tests exceeded their bounded execution time"
            return result
        reader.join(timeout=2)
        with lock:
            text = bytes(tail).decode("utf-8", errors="replace")
        result["details"] = text[-6000:]
        markers = re.findall(r"^CODEPROOF_TEST_RESULT=(.+)$", text, flags=re.MULTILINE)
        if markers:
            counts = json.loads(markers[-1])
            result.update(
                {key: int(counts[key]) for key in ("tests", "failures", "errors", "skipped")}
            )
            result["passed"] = returncode == 0 and result["tests"] > result["skipped"]
        elif returncode == 0:
            result["details"] = "Sandbox did not return a test-count verification marker"
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        result["details"] = "Docker sandbox is unavailable or returned invalid test metadata"
    finally:
        # Killing the client alone does not stop the container. Force cleanup by known UUID name.
        try:
            subprocess.run(
                ["docker", "rm", "--force", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    return result
