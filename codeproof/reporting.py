"""Portable, evidence-based reports for the user."""

import json


def markdown_report(run: dict) -> str:
    report = run.get("report") or {}
    lines = [
        "# CodeProof review report",
        "",
        f"Run: `{run['id']}`",
        f"Status: {run['status']}",
        f"Source: {json.dumps(run.get('source', {}), ensure_ascii=False)}",
        "",
        "## Outcome",
        "",
        report.get("summary", "The review has not finished."),
        "",
        f"Stop reason: {report.get('stop_reason', 'Pending')}",
        "",
        "## Findings and rationale",
        "",
    ]
    for finding in report.get("findings", []):
        lines.extend(
            [
                f"### {finding.get('rule', 'Finding')}: {finding.get('message', '')}",
                "",
                f"Location: `{finding.get('path', '')}:{finding.get('line', '')}`",
                f"Severity: {finding.get('severity', 'unknown')}; "
                f"status: {finding.get('status', 'unresolved')}; "
                f"attempts: {finding.get('attempts', 0)}",
                "",
                finding.get("rationale", "See validation evidence below."),
                "",
            ]
        )
    for title, key in [("Actions taken", "actions"), ("Validation evidence", "validation")]:
        lines.extend([f"## {title}", ""])
        for item in report.get(key, []):
            lines.extend(["```json", json.dumps(item, indent=2, ensure_ascii=False), "```", ""])
    lines.extend(["## Coverage and limitations", ""])
    lines.extend(f"- {item}" for item in report.get("limitations", []))
    lines.extend(
        [
            "",
            "## Run metrics",
            "",
            "```json",
            json.dumps(report.get("metrics", {}), indent=2),
            "```",
            "",
            "## Patch",
            "",
            "```diff",
            report.get("patch", ""),
            "```",
            "",
        ]
    )
    return "\n".join(lines)
