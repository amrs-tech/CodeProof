from html.parser import HTMLParser

import pytest

from codeproof.reporting import html_report, markdown_report


class ReportParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.attributes = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


@pytest.fixture
def run():
    return {
        "id": "06df5c7a-24f1-4052-8a98-d47961eadb30",
        "created_at": "2026-10-07T14:45:00+00:00",
        "status": "improved",
        "workspace": "private/server/workspace",
        "source": {
            "type": "github",
            "repository": "owner/repository",
            "branch": "main",
            "revision": "a" * 40,
        },
        "report": {
            "status": "improved",
            "summary": "Accepted one verified fix; one finding remains for review.",
            "provider": {
                "name": "Local reasoning provider",
                "model": "test-model",
                "mode": "configured",
                "reasoning_effort": "high",
                "credential_source": "byok",
            },
            "findings": [
                {
                    "id": "F001",
                    "rule": "F541",
                    "message": "Unnecessary f-string",
                    "path": "app.py",
                    "line": 3,
                    "severity": "low",
                    "status": "resolved",
                    "attempts": 1,
                    "rationale": "Remove interpolation without placeholders.",
                },
                {
                    "id": "F002",
                    "rule": "B006",
                    "message": "Shared mutable default",
                    "path": "app.py",
                    "line": 8,
                    "severity": "high",
                    "status": "needs_review",
                    "attempts": 0,
                    "rationale": "The default list is reused across calls.",
                },
            ],
            "actions": [
                {
                    "finding_id": "F001",
                    "path": "app.py",
                    "attempt": 1,
                    "method": "deterministic",
                    "accepted": True,
                    "rationale": "Resolved the finding with no new static issues.",
                    "rolled_back": False,
                },
                {
                    "finding_id": "F002",
                    "path": "app.py",
                    "attempt": 1,
                    "method": "model",
                    "accepted": False,
                    "rationale": "Candidate tests did not pass.",
                    "rolled_back": True,
                },
            ],
            "validation": [
                {
                    "kind": "baseline_lint",
                    "passed": True,
                    "findings": 2,
                    "details": "Existing findings recorded.",
                },
                {
                    "kind": "candidate_static",
                    "passed": True,
                    "details": "No new lint or AST findings.",
                },
                {
                    "kind": "sandbox_tests",
                    "phase": "candidate",
                    "passed": False,
                    "tests": 2,
                    "details": "One assertion failed.",
                },
            ],
            "stop_reason": "Completed bounded review; unresolved findings require review.",
            "limitations": ["Static checks do not prove behavioral correctness."],
            "metrics": {"files_analyzed": 1, "findings": 2, "accepted_changes": 1, "attempts": 2},
            "patch": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-return f'hello'\n+return 'hello'\n",
        },
    }


def test_html_is_self_contained_readable_and_preserves_complete_evidence(run):
    document = html_report(run)
    parser = ReportParser()
    parser.feed(document)
    assert document.startswith("<!doctype html>")
    assert {
        "main",
        "header",
        "section",
        "footer",
        "details",
        "summary",
        "pre",
        "code",
        "time",
    }.issubset(parser.tags)
    assert not {"script", "iframe", "img", "link"}.intersection(parser.tags)
    text = " ".join(parser.text)
    for expected in [
        "owner/repository",
        "07 Oct 2026",
        "test-model",
        "Reasoning effort",
        "Credential source",
        "Run-only own key",
        "Accepted changes",
        "Shared mutable default",
        "Static evidence",
        "Behavioral evidence",
        "Baseline established",
        "rolled back",
        "Unified diff",
    ]:
        assert expected in text
    assert "private/server/workspace" not in document
    assert "return 'hello'" in text
    assert "@media print" in document
    assert "@media(max-width:720px)" in document
    assert "break-inside:avoid" in document
    assert "display:block!important" in document
    csp = [
        attrs["content"]
        for tag, attrs in parser.attributes
        if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy"
    ]
    assert len(csp) == 1
    assert "default-src 'none'" in csp[0]
    assert "style-src 'unsafe-inline'" in csp[0]


def test_all_repository_and_model_fields_remain_escaped_text(run):
    hostile = '</style><script>alert("unsafe")</script><img src=x onerror="alert(1)">&\'"'
    run["id"] = hostile
    run["status"] = hostile
    run["created_at"] = hostile
    run["source"] = {
        "name": hostile,
        "type": hostile,
        "branch": hostile,
        "revision": hostile,
        "provider": {
            "name": hostile,
            "model": hostile,
            "mode": hostile,
            "reasoning_effort": hostile,
            "credential_source": hostile,
        },
    }
    run["report"] = {
        "status": hostile,
        "summary": hostile,
        "findings": [
            {
                "message": hostile,
                "path": hostile,
                "line": hostile,
                "rule": hostile,
                "severity": hostile,
                "status": hostile,
                "rationale": hostile,
            }
        ],
        "actions": [{"path": hostile, "method": hostile, "reason": hostile}],
        "validation": [{"kind": hostile, "details": hostile, "status": hostile, "phase": hostile}],
        "stop_reason": hostile,
        "limitations": [hostile],
        "patch": hostile,
    }
    document = html_report(run)
    parser = ReportParser()
    parser.feed(document)
    assert "script" not in parser.tags
    assert "img" not in parser.tags
    assert document.count("<style>") == document.count("</style>") == 1
    assert "&lt;script&gt;" in document
    assert "&quot;unsafe&quot;" in document
    assert hostile in " ".join(parser.text)
    assert all(not name.startswith("on") for _, attrs in parser.attributes for name in attrs)
    assert all("unsafe" not in attrs.get("class", "") for _, attrs in parser.attributes)


@pytest.mark.parametrize(
    "value", [None, "unstructured record", 42, True, [], {}, {"unexpected": "shape"}]
)
def test_malformed_optional_report_data_is_presentable(value):
    document = html_report(
        {
            "report": {
                "findings": value,
                "actions": value,
                "validation": value,
                "metrics": value,
                "limitations": value,
                "provider": value,
            }
        }
    )
    parser = ReportParser()
    parser.feed(document)
    assert "Complete review record" in document
    assert "script" not in parser.tags


@pytest.mark.parametrize("report", [None, "unstructured report", 42, []])
def test_absent_or_malformed_report_has_explicit_evidence_limits(report):
    document = html_report({"id": "pending-run", "status": "queued", "report": report})
    assert "No findings were recorded within the supported checks" in document
    assert "No validation evidence was recorded" in document
    assert "No accepted changes are available as a patch" in document
    assert "This review has not produced a completed report" in document


def test_counts_are_derived_when_metrics_are_missing_or_malformed(run):
    run["report"]["metrics"] = {
        "accepted_changes": -5,
        "attempts": "not-a-number",
        "unresolved": True,
    }
    document = html_report(run)
    assert "<dt>Findings</dt><dd>2</dd>" in document
    assert "<dt>Accepted changes</dt><dd>1</dd>" in document
    assert "<dt>Attempts</dt><dd>1</dd>" in document
    assert "<dt>Unresolved</dt><dd>1</dd>" in document


def test_markdown_report_remains_available(run):
    report = markdown_report(run)
    assert report.startswith("# CodeProof review report")
    assert "Shared mutable default" in report
    assert "```diff" in report
    assert "## Review configuration" in report
    assert "Review provider: Local reasoning provider" in report
    assert "Model: test-model" in report
    assert "Reasoning effort: High" in report
    assert "Credential source: Run-only own key" in report


@pytest.mark.parametrize(
    ("source", "label"),
    [
        ("byok", "Run-only own key"),
        ("environment", "Server environment"),
        ("none", "No provider key"),
    ],
)
def test_configuration_provenance_is_readable_in_html_and_markdown(run, source, label):
    run["source"]["provider"] = {"credential_source": source}
    run["report"]["provider"].pop("credential_source")
    run["report"]["provider"]["reasoning_effort"] = None
    document = html_report(run)
    assert f"<dt>Credential source</dt><dd>{label}</dd>" in document
    assert "<dt>Reasoning effort</dt><dd>Provider default / not applicable</dd>" in document
    markdown = markdown_report(run)
    assert f"Credential source: {label}" in markdown
    assert "Reasoning effort: Provider default / not applicable" in markdown


def test_markdown_provider_configuration_cannot_inject_markup(run):
    run["report"]["provider"]["model"] = "<script>alert(1)</script>\n[click](javascript:alert(1))"
    markdown = markdown_report(run)
    configuration = markdown.split("## Review configuration", 1)[1].split("## Outcome", 1)[0]
    assert "<script>" not in configuration
    assert "&lt;script&gt;" in configuration
    assert "\\[click\\]\\(javascript:alert\\(1\\)\\)" in configuration


def test_fallback_reports_actual_provider_and_initial_request_separately(run):
    run["source"]["provider"] = {
        "provider": "gemini",
        "model": "primary-text-model",
        "credential_source": "environment",
    }
    run["report"]["provider"] = {
        "provider": "openai",
        "model": "backup-text-model",
        "reasoning_effort": "medium",
        "requested_provider": "gemini",
        "requested_model": "primary-text-model",
        "actual_provider": "openai",
        "actual_model": "backup-text-model",
        "fallback_reason": "rate_limit_or_quota",
        "provider_calls": 4,
        "fallback_history": [
            {
                "from_provider": "gemini",
                "from_model": "primary-text-model",
                "to_provider": "openai",
                "to_model": "backup-text-model",
                "reason": "rate_limit_or_quota",
                "provider_calls": 2,
            }
        ],
    }
    document = html_report(run)
    assert "<dt>Review provider</dt><dd>openai</dd>" in document
    assert "<dt>Model</dt><dd>backup-text-model</dd>" in document
    assert "<dt>Requested provider</dt><dd>gemini</dd>" in document
    assert "<dt>Requested model</dt><dd>primary-text-model</dd>" in document
    assert "<dt>Provider calls</dt><dd>4</dd>" in document
    assert "gemini / primary-text-model → openai / backup-text-model" in document
    assert "The primary provider reached a rate limit or quota limit." in document
    assert "Shared provider calls at transition: 2." in document
    markdown = markdown_report(run)
    assert "Review provider: openai" in markdown
    assert "Requested provider: gemini" in markdown
    assert "## Provider fallback history" in markdown
    assert "primary-text-model → openai / backup-text-model" in markdown
    assert "Shared provider calls at transition: 2." in markdown


def test_fallback_report_escapes_models_and_does_not_render_unrecognized_reasons(run):
    hostile = '<script>alert(1)</script>"'
    run["report"]["provider"] = {
        "provider": "openai",
        "model": hostile,
        "requested_provider": hostile,
        "requested_model": hostile,
        "fallback_reason": hostile,
        "fallback_history": [
            {
                "from_provider": hostile,
                "from_model": hostile,
                "to_provider": hostile,
                "to_model": hostile,
                "reason": hostile,
            }
        ],
    }
    document = html_report(run)
    parser = ReportParser()
    parser.feed(document)
    assert "script" not in parser.tags
    assert "Provider availability failure; no recognized reason was recorded." in document
    assert "&lt;script&gt;" in document
    markdown = markdown_report(run)
    configuration = markdown.split("## Review configuration", 1)[1].split("## Outcome", 1)[0]
    assert "<script>" not in configuration
    assert "&lt;script&gt;" in configuration
