"""A finite LangGraph review, guarded proposal, and validation workflow."""

from __future__ import annotations

import ast
import difflib
import re
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from codeproof.analyzer import (
    SAFE_FIX_RULES,
    AnalysisError,
    ast_findings,
    diagnostic_counts,
    digest,
    inventory,
    issue_count,
    make_findings,
    ruff_check,
    safe_fix,
    safe_path,
    syntax_errors,
)
from codeproof.config import Settings
from codeproof.errors import RunAborted
from codeproof.llm import EditProvider, ProposalError
from codeproof.sandbox import run_tests


class ReviewState(TypedDict, total=False):
    route: str
    report: dict


def _is_test_path(path: str) -> bool:
    name = Path(path).name
    return "tests" in Path(path).parts or name.startswith("test_") or name.endswith("_test.py")


def _guard_candidate(source: bytes, candidate: bytes, finding: dict, settings: Settings) -> None:
    if not candidate or len(candidate) > settings.max_file_bytes:
        raise ProposalError("Candidate source exceeds size limits or empties the file")
    if candidate == source:
        raise ProposalError("No progress: candidate source is unchanged")
    try:
        before = ast.parse(source)
        after = ast.parse(candidate)
    except (SyntaxError, ValueError, RecursionError) as error:
        raise ProposalError("Candidate does not parse as valid Python") from error
    before_text = source.decode("utf-8", errors="replace")
    after_text = candidate.decode("utf-8", errors="replace")
    suppression = re.compile(r"#.*(?:noqa|ruff\s*:|type\s*:\s*ignore)", re.IGNORECASE)
    if Counter(suppression.findall(after_text)) - Counter(suppression.findall(before_text)):
        raise ProposalError("Candidate introduces a check-suppression directive")
    # A model cannot erase all existing callable definitions to make lint findings vanish.
    old_defs = {
        (type(node).__name__, node.name)
        for node in ast.walk(before)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    new_defs = {
        (type(node).__name__, node.name)
        for node in ast.walk(after)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    if not old_defs.issubset(new_defs):
        raise ProposalError("Candidate removes an existing function or class")
    changed_lines = sum(
        line.startswith(("+", "-"))
        for line in difflib.ndiff(before_text.splitlines(), after_text.splitlines())
    )
    if finding["rule"] not in SAFE_FIX_RULES and changed_lines > max(
        20, int(len(before_text.splitlines()) * 0.4)
    ):
        raise ProposalError("Candidate changes too many lines for a targeted fix")
    before_imports = {
        ast.dump(node)
        for node in ast.walk(before)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    }
    after_imports = {
        ast.dump(node) for node in ast.walk(after) if isinstance(node, (ast.Import, ast.ImportFrom))
    }
    if after_imports - before_imports:
        raise ProposalError("Candidate introduces imports outside the targeted source baseline")
    dangerous = {
        "eval",
        "exec",
        "__import__",
        "compile",
        "system",
        "popen",
        "Popen",
        "check_call",
        "check_output",
    }

    def dangerous_calls(tree):
        return Counter(
            ast.dump(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                isinstance(node.func, ast.Name)
                and node.func.id in dangerous
                or isinstance(node.func, ast.Attribute)
                and node.func.attr in dangerous
            )
        )

    if dangerous_calls(after) - dangerous_calls(before):
        raise ProposalError("Candidate introduces dynamic execution or process calls")
    if finding["rule"] not in SAFE_FIX_RULES and _is_test_path(finding["path"]):
        raise ProposalError("Substantive model edits to test files are forbidden")


class _Review:
    def __init__(self, workspace: Path, settings: Settings, context_search, progress):
        self.workspace = workspace.resolve(strict=True)
        self.settings = settings
        self.context_search = context_search
        self.progress = progress
        self.started = time.monotonic()
        self.deadline = self.started + settings.max_run_seconds
        self.provider = EditProvider(settings)
        self.original: dict[str, bytes] = {}
        self.current: dict[str, bytes] = {}
        self.diagnostics: list[dict] = []
        self.findings: list[dict] = []
        self.actions: list[dict] = []
        self.validation: list[dict] = []
        self.limitations = [
            "Static lint and syntax checks do not prove behavioral correctness.",
            "Only Python source is analyzed and edited; other languages require review.",
            "Only findings within the configured report limit are scheduled for remediation.",
            "Model changes require a passing stdlib unittest suite under tests/ in opt-in Docker.",
            "Existing repository tests may be incomplete; passing tests are evidence, not a proof.",
        ]
        self.index = -1
        self.total_attempts = 0
        self.seen: set[str] = set()
        self.candidate: bytes | None = None
        self.rationale = ""
        self.feedback = ""
        self.stop_reason = ""
        self.baseline_tests_passed = False
        self.model_attempt = False
        self.rejected = 0

    def emit(self, stage: str, detail: str) -> None:
        if self.progress:
            try:
                self.progress(stage, detail)
            except RunAborted:
                raise
            except Exception:
                self.limitations.append("Progress reporting failed; validation continued locally.")

    def bound(self) -> bool:
        if time.monotonic() >= self.deadline:
            self.stop_reason = "Run time limit reached"
        elif self.total_attempts >= self.settings.max_total_attempts:
            self.stop_reason = "Global remediation attempt limit reached"
        return bool(self.stop_reason)

    def scan(self, _state: ReviewState) -> ReviewState:
        self.emit("analyzing", "Establishing static lint and syntax baseline")
        try:
            self.original = inventory(self.workspace, self.settings, self.deadline)
            self.current = dict(self.original)
            lint = ruff_check(self.workspace, self.current, self.settings, self.deadline)
            self.diagnostics = lint + ast_findings(self.current)
            self.findings = make_findings(self.diagnostics, self.settings.max_findings)
            errors = syntax_errors(self.current)
            self.validation.append(
                {
                    "kind": "baseline_syntax",
                    "passed": not errors,
                    "details": errors,
                    "files": len(self.current),
                }
            )
            self.validation.append(
                {
                    "kind": "baseline_lint",
                    "passed": True,
                    "details": "Lint baseline established; existing findings recorded",
                    "findings": len(self.diagnostics),
                }
            )
            if errors:
                self.stop_reason = "Baseline contains invalid Python syntax; changes were held"
                return {"route": "report"}
            if self.provider.configured and self.settings.sandbox_enabled:
                self.emit("validating", "Running baseline tests in restricted Docker")
                baseline = run_tests(self.workspace, self.settings, self.deadline)
                baseline["phase"] = "baseline"
                self.validation.append(baseline)
                self.baseline_tests_passed = baseline["passed"]
            elif not self.provider.configured:
                self.limitations.append(
                    "No model configured: only conservative textual fixes are attempted."
                )
            else:
                self.limitations.append(
                    "Docker execution disabled: substantive model edits are held."
                )
        except (AnalysisError, OSError) as error:
            self.stop_reason = str(error)
            return {"route": "report"}
        return {"route": "select"}

    def select(self, _state: ReviewState) -> ReviewState:
        if self.bound():
            return {"route": "report"}
        self.index += 1
        while self.index < len(self.findings):
            finding = self.findings[self.index]
            self.feedback = ""
            if issue_count(self.diagnostics, finding) == 0:
                finding["status"] = "resolved"
            elif finding["rule"] not in SAFE_FIX_RULES and (
                not self.provider.configured
                or not self.baseline_tests_passed
                or _is_test_path(finding["path"])
            ):
                finding["status"] = "needs_review"
            else:
                return {"route": "propose"}
            self.index += 1
        return {"route": "report"}

    def propose(self, _state: ReviewState) -> ReviewState:
        if self.bound():
            return {"route": "report"}
        finding = self.findings[self.index]
        finding["attempts"] += 1
        self.total_attempts += 1
        self.candidate = None
        self.rationale = ""
        self.model_attempt = finding["rule"] not in SAFE_FIX_RULES
        self.emit(
            "remediating",
            f"Attempt {finding['attempts']} for {finding['rule']} in {finding['path']}",
        )
        source = self.current[finding["path"]]
        try:
            if self.model_attempt:
                context = []
                if self.context_search:
                    try:
                        context = (
                            self.context_search(f"{finding['rule']} {finding['message']}") or []
                        )
                    except Exception:
                        self.limitations.append(
                            "Related context retrieval failed; proposal used local source."
                        )
                proposal = self.provider.propose(
                    finding, source, context, self.feedback, self.deadline
                )
                candidate = proposal.content.encode("utf-8")
                self.rationale = proposal.rationale
            else:
                candidate = safe_fix(
                    source, finding["path"], finding["rule"], self.settings, self.deadline
                )
                self.rationale = finding["rationale"]
            fingerprint = digest(finding["path"].encode() + b"\0" + candidate)
            if fingerprint in self.seen:
                self.feedback = "No progress: duplicate candidate was already evaluated"
                finding["status"] = "rejected"
                self.rejected += 1
                self.actions.append(
                    {
                        "finding_id": finding["id"],
                        "path": finding["path"],
                        "attempt": finding["attempts"],
                        "accepted": False,
                        "rationale": self.feedback,
                        "method": "model" if self.model_attempt else "deterministic",
                        "rolled_back": False,
                    }
                )
                return {"route": "select"}
            self.seen.add(fingerprint)
            _guard_candidate(source, candidate, finding, self.settings)
            self.candidate = candidate
        except (ProposalError, AnalysisError, OSError) as error:
            self.feedback = str(error)
        return {"route": "validate"}

    def validate(self, _state: ReviewState) -> ReviewState:
        finding = self.findings[self.index]
        relative = finding["path"]
        accepted = False
        wrote = False
        candidate_diagnostics = None
        source = self.current[relative]
        validation = {
            "kind": "candidate_static",
            "finding_id": finding["id"],
            "attempt": finding["attempts"],
            "passed": False,
            "details": self.feedback,
        }
        try:
            if self.candidate is not None:
                if time.monotonic() >= self.deadline:
                    raise AnalysisError("Run time limit reached before candidate validation")
                path = safe_path(self.workspace, relative)
                if path.read_bytes() != source:
                    raise AnalysisError("Source changed outside the workflow; candidate rejected")
                candidate_files = dict(self.current)
                candidate_files[relative] = self.candidate
                if syntax_errors(candidate_files):
                    raise AnalysisError("Candidate introduces invalid Python syntax")
                path.write_bytes(self.candidate)
                wrote = True
                lint = ruff_check(self.workspace, candidate_files, self.settings, self.deadline)
                candidate_diagnostics = lint + ast_findings(candidate_files)
                new_issues = diagnostic_counts(candidate_diagnostics) - diagnostic_counts(
                    self.diagnostics
                )
                if new_issues:
                    raise AnalysisError("Candidate introduces new static findings")
                if issue_count(candidate_diagnostics, finding) != 0:
                    raise AnalysisError("No progress: targeted finding was not resolved")
                validation.update(
                    {
                        "passed": True,
                        "details": "Syntax valid; no new lint or AST findings; targeted file/rule resolved",
                    }
                )
                if self.model_attempt:
                    self.emit(
                        "validating", f"Running candidate tests for {relative} in restricted Docker"
                    )
                    candidate_tests = run_tests(self.workspace, self.settings, self.deadline)
                    candidate_tests.update(
                        {
                            "phase": "candidate",
                            "finding_id": finding["id"],
                            "attempt": finding["attempts"],
                        }
                    )
                    self.validation.append(candidate_tests)
                    if not candidate_tests["passed"]:
                        raise AnalysisError("Candidate sandbox tests did not pass")
                accepted = True
                self.current = candidate_files
                self.diagnostics = candidate_diagnostics
        except RunAborted:
            raise
        except Exception as error:
            self.feedback = (
                str(error)
                if isinstance(error, (AnalysisError, OSError))
                else "Unexpected validation failure; candidate was rolled back"
            )
        finally:
            if wrote and not accepted:
                # Always restore rejected bytes, including timeout or exceptions from validation.
                safe_path(self.workspace, relative).write_bytes(source)
        if not accepted:
            self.rejected += 1
            if not validation["passed"]:
                validation["details"] = self.feedback or "Candidate proposal failed"
        self.validation.append(validation)
        self.actions.append(
            {
                "finding_id": finding["id"],
                "path": relative,
                "attempt": finding["attempts"],
                "accepted": accepted,
                "method": "model" if self.model_attempt else "deterministic",
                "rationale": self.rationale if accepted else self.feedback,
                "rolled_back": wrote and not accepted,
            }
        )
        if accepted:
            finding["status"] = "resolved"
            return {"route": "select"}
        terminal_error = any(
            word in self.feedback.lower()
            for word in (
                "no progress",
                "budget",
                "provider",
                "time limit",
                "outside the workflow",
                "suppression",
                "removes",
                "too many",
                "path",
                "hash",
                "schema",
                "prompt",
            )
        )
        if self.bound():
            finding["status"] = "rejected"
            return {"route": "report"}
        if (
            finding["attempts"] >= self.settings.max_attempts
            or terminal_error
            or not self.model_attempt
        ):
            finding["status"] = "rejected"
            return {"route": "select"}
        return {"route": "propose"}

    def report(self, _state: ReviewState) -> ReviewState:
        self.emit(
            "reporting", "Preparing rationale, validation evidence, unresolved findings and patch"
        )
        accepted = sum(action["accepted"] for action in self.actions)
        for finding in self.findings:
            if finding["status"] == "pending":
                finding["status"] = "not_attempted" if self.stop_reason else "needs_review"
            elif issue_count(self.diagnostics, finding) == 0:
                finding["status"] = "resolved"
        unresolved = sum(finding["status"] != "resolved" for finding in self.findings)
        status = "improved" if accepted else "stopped" if self.stop_reason else "reviewed"
        if accepted:
            summary = f"Accepted {accepted} verified targeted change(s); {unresolved} finding(s) remain for review."
        elif self.stop_reason:
            summary = f"Review stopped safely: {self.stop_reason}. No changes were accepted."
        else:
            summary = f"Reviewed {len(self.current)} Python file(s); {unresolved} finding(s) require review."
        patch = ""
        for relative in self.original:
            if self.original[relative] != self.current.get(relative):
                patch += "".join(
                    difflib.unified_diff(
                        self.original[relative]
                        .decode("utf-8", errors="replace")
                        .splitlines(keepends=True),
                        self.current[relative]
                        .decode("utf-8", errors="replace")
                        .splitlines(keepends=True),
                        fromfile=f"a/{relative}",
                        tofile=f"b/{relative}",
                    )
                )
        reason = self.stop_reason or (
            "Completed bounded review; unresolved findings require review"
            if unresolved
            else "Completed bounded review"
        )
        report = {
            "summary": summary,
            "status": status,
            "findings": self.findings,
            "actions": self.actions,
            "validation": self.validation,
            "stop_reason": reason,
            "limitations": list(dict.fromkeys(self.limitations)),
            "patch": patch,
            "metrics": {
                "files_analyzed": len(self.current),
                "findings": len(self.findings),
                "total_static_findings": len(self.diagnostics),
                "attempts": self.total_attempts,
                "accepted_changes": accepted,
                "rejected_attempts": self.rejected,
                "provider_calls": self.provider.calls,
                "elapsed_seconds": round(time.monotonic() - self.started, 3),
                "max_attempts_per_finding": self.settings.max_attempts,
                "max_total_attempts": self.settings.max_total_attempts,
            },
        }
        return {"report": report}


def run_review(
    workspace: Path,
    settings: Settings,
    context_search: Callable[[str], list[dict]] | None = None,
    progress: Callable[[str, str], None] | None = None,
) -> dict:
    """Review and improve an isolated workspace, returning a JSON-serializable audit report."""
    review = _Review(workspace, settings, context_search, progress)
    graph = StateGraph(ReviewState)
    graph.add_node("scan", review.scan)
    graph.add_node("select", review.select)
    graph.add_node("propose", review.propose)
    graph.add_node("validate", review.validate)
    graph.add_node("report", review.report)
    graph.add_edge(START, "scan")
    for node in ("scan", "select", "propose", "validate"):
        graph.add_conditional_edges(
            node,
            lambda state: state["route"],
            {
                "select": "select",
                "propose": "propose",
                "validate": "validate",
                "report": "report",
            },
        )
    graph.add_edge("report", END)
    compiled = graph.compile()
    final = compiled.invoke(
        {}, {"recursion_limit": settings.max_total_attempts * 4 + settings.max_findings * 3 + 20}
    )
    return final["report"]
