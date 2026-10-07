"""Static repository checks. Repository Python is parsed, never imported."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from codeproof.config import Settings

# Safe-fix classification is additionally narrowed to behavior-neutral textual rules.
SAFE_FIX_RULES = frozenset({"F541", "E703", "UP034", "W291", "W292"})
RUFF_RULES = "E4,E7,E9,F,I,B,UP,W291,W292"
EXCLUDED_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__"})


class AnalysisError(RuntimeError):
    """A trustworthy static baseline could not be established."""


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def safe_path(workspace: Path, relative: str) -> Path:
    """Allow existing Python files beneath the workspace, with no link traversal."""
    if not relative or "\\" in relative or ":" in relative:
        raise AnalysisError("Only repository-relative POSIX Python paths are allowed")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} or part in EXCLUDED_DIRS for part in parts):
        raise AnalysisError("Protected or traversing edit path rejected")
    root = workspace.resolve(strict=True)
    candidate = root.joinpath(*parts)
    for parent in [candidate, *candidate.parents]:
        if parent == root:
            break
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise AnalysisError("Linked edit paths are forbidden")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file() or resolved.suffix != ".py":
        raise AnalysisError("Edits must target existing repository Python files")
    return resolved


def inventory(workspace: Path, settings: Settings, deadline: float) -> dict[str, bytes]:
    root = workspace.resolve(strict=True)
    files: dict[str, bytes] = {}
    total_bytes = 0
    entries = 0
    for folder, directories, names in os.walk(root, followlinks=False):
        directories[:] = sorted(
            name
            for name in directories
            if name not in EXCLUDED_DIRS
            and not Path(folder, name).is_symlink()
            and not (
                hasattr(Path(folder, name), "is_junction") and Path(folder, name).is_junction()
            )
        )
        for name in sorted(names):
            if time.monotonic() >= deadline:
                raise AnalysisError("Run time limit reached during inventory")
            entries += 1
            if entries > settings.max_files:
                raise AnalysisError("Repository file count exceeds configured limit")
            path = Path(folder, name)
            if path.suffix != ".py":
                continue
            relative = path.relative_to(root).as_posix()
            path = safe_path(root, relative)
            if path.stat().st_size > settings.max_file_bytes:
                raise AnalysisError(f"Python file exceeds configured limit: {relative}")
            data = path.read_bytes()
            total_bytes += len(data)
            if total_bytes > settings.max_extracted_bytes:
                raise AnalysisError("Python source exceeds configured total byte limit")
            files[relative] = data
    return files


def _remaining_timeout(settings: Settings, deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AnalysisError("Run time limit reached")
    return min(float(settings.command_timeout), remaining)


def ruff_check(
    workspace: Path, files: dict[str, bytes], settings: Settings, deadline: float
) -> list[dict]:
    if not files:
        return []
    # stdin avoids repo configuration, symlink following, plugins, and argument-list limits.
    diagnostics = []
    for relative, source in files.items():
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "ruff",
                    "check",
                    "--isolated",
                    "--no-cache",
                    "--ignore-noqa",
                    "--output-format",
                    "json",
                    "--select",
                    RUFF_RULES,
                    "--stdin-filename",
                    relative,
                    "-",
                ],
                input=source,
                capture_output=True,
                cwd=workspace,
                shell=False,
                timeout=_remaining_timeout(settings, deadline),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AnalysisError("Static lint tool failed or timed out") from error
        if result.returncode not in {0, 1}:
            raise AnalysisError("Static lint tool could not establish a baseline")
        try:
            parsed = json.loads(result.stdout)
        except (ValueError, TypeError) as error:
            raise AnalysisError("Static lint tool returned malformed output") from error
        if not isinstance(parsed, list):
            raise AnalysisError("Static lint output was not a diagnostic list")
        for item in parsed:
            item["path"] = relative
            diagnostics.append(item)
    return diagnostics


def syntax_errors(files: dict[str, bytes]) -> list[dict]:
    errors = []
    for relative, source in files.items():
        try:
            ast.parse(source, filename=relative)
        except (SyntaxError, ValueError, RecursionError) as error:
            errors.append(
                {
                    "path": relative,
                    "line": getattr(error, "lineno", 1) or 1,
                    "message": str(error)[:500],
                }
            )
    return errors


def ast_findings(files: dict[str, bytes]) -> list[dict]:
    findings = []
    for relative, source in files.items():
        try:
            tree = ast.parse(source, filename=relative)
        except (SyntaxError, ValueError, RecursionError):
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ExceptHandler)
                and node.body
                and all(isinstance(statement, ast.Pass) for statement in node.body)
            ):
                findings.append(
                    {
                        "code": "CP001",
                        "path": relative,
                        "location": {"row": node.lineno},
                        "message": "Exception is silently discarded",
                        "rationale": "Discarding errors can conceal failed operations; "
                        "the intended recovery behavior requires review.",
                    }
                )
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and (node.func.id in {"eval", "exec"})
            ):
                findings.append(
                    {
                        "code": "CP002",
                        "path": relative,
                        "location": {"row": node.lineno},
                        "message": "Dynamic Python execution requires trust review",
                        "rationale": "Untrusted input can execute arbitrary code. "
                        "Static analysis cannot establish the input's trust boundary.",
                    }
                )
    return findings


def diagnostic_counts(diagnostics: list[dict]) -> Counter:
    # Lines may move after a fix; rule/path/message multiplicity remains meaningful.
    return Counter((item["path"], item["code"], item["message"]) for item in diagnostics)


def issue_count(diagnostics: list[dict], finding: dict) -> int:
    return sum(
        item["path"] == finding["path"] and item["code"] == finding["rule"] for item in diagnostics
    )


def make_findings(diagnostics: list[dict], maximum: int) -> list[dict]:
    findings = []
    for item in diagnostics[:maximum]:
        rule = item["code"] or "SYNTAX"
        path = item["path"]
        line = item.get("location", {}).get("row", 1)
        identity = f"{path}:{rule}:{line}:{item['message']}"
        rationale = item.get("rationale") or (
            "This rule has a conservative textual fix that can be statically verified."
            if rule in SAFE_FIX_RULES
            else "This finding may indicate fragile code or maintenance debt. Any substantive "
            "change must preserve behavior under the repository's sandboxed tests."
        )
        findings.append(
            {
                "id": digest(identity.encode())[:16],
                "rule": rule,
                "path": path,
                "line": line,
                "severity": "high"
                if rule in {"CP002", "F821"}
                else "medium"
                if rule.startswith(("B", "CP"))
                else "low",
                "message": item["message"],
                "rationale": rationale,
                "status": "pending",
                "attempts": 0,
            }
        )
    return findings


def safe_fix(source: bytes, relative: str, rule: str, settings: Settings, deadline: float) -> bytes:
    if rule not in SAFE_FIX_RULES:
        raise AnalysisError("Rule has no permitted deterministic fix")
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "ruff",
                "check",
                "--isolated",
                "--no-cache",
                "--ignore-noqa",
                "--fix",
                "--fix-only",
                "--no-unsafe-fixes",
                "--select",
                rule,
                "--stdin-filename",
                relative,
                "-",
            ],
            input=source,
            capture_output=True,
            shell=False,
            timeout=_remaining_timeout(settings, deadline),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AnalysisError("Deterministic fix tool failed or timed out") from error
    if result.returncode != 0:
        raise AnalysisError("Deterministic fix tool failed")
    return result.stdout
