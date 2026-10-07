"""Portable, evidence-based reports for the user."""

import json
from datetime import datetime
from html import escape


def markdown_report(run: dict) -> str:
    report = run.get("report") or {}
    lines = [
        "# CodeProof review report",
        "",
        f"Run: `{run['id']}`",
        f"Status: {run['status']}",
        f"Source: {json.dumps(run.get('source', {}), ensure_ascii=False)}",
        "",
    ]
    configuration = _provider_details(run, report)
    if configuration:
        lines.extend(["## Review configuration", ""])
        lines.extend(f"- {label}: {_markdown_text(value)}" for label, value in configuration)
        lines.append("")
    lines.extend(
        [
            "## Outcome",
            "",
            report.get("summary", "The review has not finished."),
            "",
            f"Stop reason: {report.get('stop_reason', 'Pending')}",
            "",
            "## Findings and rationale",
            "",
        ]
    )
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


_HTML_STYLES = """
:root{color-scheme:light;--paper:#fff;--canvas:#f2f4f3;--ink:#172b2b;--muted:#657674;
--line:#dce4df;--green:#176650;--green-bg:#e7f4eb;--amber:#875609;--amber-bg:#fff3d9;
--red:#a23b37;--red-bg:#fbe9e7;--blue:#375d83;--blue-bg:#eaf1f8}
*{box-sizing:border-box}body{margin:0;background:var(--canvas);color:var(--ink);
font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}
.report{max-width:1120px;margin:40px auto;background:var(--paper);border:1px solid var(--line);
border-radius:20px;overflow:hidden;box-shadow:0 12px 42px #20352b08}
.masthead{padding:38px 48px 34px;background:#162f2d;color:#fff}
.brand{display:flex;align-items:center;gap:12px;font-size:21px;font-weight:750;letter-spacing:-.6px}
.monogram{border:1px solid #ffffff60;border-radius:9px;padding:2px 7px;font-size:13px;
letter-spacing:-.5px;line-height:25px}.eyebrow{text-transform:uppercase;letter-spacing:1.5px;
font-size:11px;font-weight:750;color:var(--muted);margin:0 0 9px}
.masthead .eyebrow{color:#bdcdc7;margin-top:26px}h1{font-size:36px;letter-spacing:-1.3px;
line-height:1.18;margin:0 0 17px}h2{font-size:22px;letter-spacing:-.5px;line-height:1.3;
margin:0}h3{font-size:16px;line-height:1.5;margin:0;font-weight:700}
p{margin:0 0 12px}.masthead-meta{display:flex;flex-wrap:wrap;gap:12px 22px;color:#d5e1dc;
font-size:13px}.source-name{color:#fff;font-weight:650;overflow-wrap:anywhere}
.content{padding:38px 48px 30px}.section{margin-top:36px}.section:first-child{margin-top:0}
.section-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;
padding-bottom:15px;margin-bottom:18px;border-bottom:1px solid var(--line)}
.section-note{font-size:12px;color:var(--muted)}.summary{font-size:19px;line-height:1.6;
max-width:850px;letter-spacing:-.25px}.badge{display:inline-block;vertical-align:middle;
padding:4px 10px;border-radius:6px;font-size:11px;font-weight:700;line-height:1.5;
white-space:normal;overflow-wrap:anywhere;background:#eef1ef;color:#526660}
.positive{color:var(--green);background:var(--green-bg)}.warning{color:var(--amber);
background:var(--amber-bg)}.negative{color:var(--red);background:var(--red-bg)}
.active{color:var(--blue);background:var(--blue-bg)}.masthead .badge{align-self:center}
.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:24px}
.metric{border:1px solid var(--line);border-radius:12px;padding:17px 18px;margin:0}
.metric dt{font-size:12px;color:var(--muted)}.metric dd{font-size:34px;font-weight:750;
letter-spacing:-1px;line-height:1.3;margin:3px 0}.metric .metric-detail{font-size:11px;
font-weight:400;letter-spacing:0;line-height:1.6;color:var(--muted)}
.provenance{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 28px;
padding:19px 22px;background:#f7f9f7;border:1px solid var(--line);border-radius:12px;margin:20px 0 0}
.provenance dt{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.7px}
.provenance dd{font-size:13px;margin:3px 0 0;overflow-wrap:anywhere}
.finding,.action,.validation{border:1px solid var(--line);border-radius:12px;padding:20px 22px;
margin:12px 0}.card-header{display:flex;align-items:flex-start;justify-content:space-between;
gap:16px;margin-bottom:10px}.tags{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:6px}
.location{font:12px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;color:var(--muted);
overflow-wrap:anywhere;margin:6px 0 13px}.rationale,.reason{font-size:14px;overflow-wrap:anywhere}
.label{font-weight:650}.fineprint{font-size:12px;color:var(--muted)}
.rollback{border-left:3px solid #d49b48;padding-left:12px;color:var(--amber);font-size:13px}
.empty{padding:20px 22px;background:#f7f9f7;border-radius:10px;color:var(--muted);font-size:14px}
.validation-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.validation{margin:0}.validation .card-header{gap:8px}.validation .eyebrow{font-size:10px}
details{margin-top:14px;border-top:1px solid var(--line);padding-top:10px}
summary{cursor:pointer;font-size:12px;font-weight:650;color:#47625c}
pre{margin:10px 0 0;padding:15px 17px;border:1px solid var(--line);border-radius:8px;
background:#f7f9f7;font:12px/1.65 ui-monospace,SFMono-Regular,Consolas,monospace;
overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;tab-size:4}
.patch{background:#162f2d;color:#e8f0ec;border-color:#162f2d;font-size:12px}
.stop{border-left:3px solid #6e9384;padding:14px 19px;background:#f0f6f2;
font-size:14px;border-radius:0 9px 9px 0;overflow-wrap:anywhere}
.limitations{padding-left:21px;margin:14px 0;font-size:14px}.limitations li{padding:4px 0;
overflow-wrap:anywhere}.footer{display:flex;flex-wrap:wrap;justify-content:space-between;
gap:10px;padding:20px 48px;border-top:1px solid var(--line);font-size:11px;color:var(--muted)}
.run-id{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}
@media(max-width:720px){.report{margin:0;border-radius:0;border-width:0}.masthead{padding:28px 22px}
.content{padding:26px 22px}.footer{padding:20px 22px}h1{font-size:28px}.summary{font-size:17px}
.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.provenance,.validation-grid{
grid-template-columns:1fr}.card-header{flex-direction:column;gap:10px}.tags{justify-content:flex-start}
.finding,.action,.validation{padding:17px}.section-heading{align-items:flex-start}}
@page{size:A4;margin:15mm}
@media print{body{background:#fff;font-size:10pt}.report{margin:0;max-width:none;border:0;
border-radius:0;box-shadow:none}.masthead{background:#fff;color:#172b2b;padding:0 0 18px;
border-bottom:2px solid #172b2b}.masthead .eyebrow,.masthead-meta,.source-name{color:#526660}
.monogram{border-color:#526660}.content{padding:22px 0}.footer{padding:12px 0}h1{font-size:24pt}
h2{font-size:15pt}.summary{font-size:12pt}.section{margin-top:25px}.metrics{gap:8px}
.metric{padding:10px}.metric dd{font-size:24pt}.finding,.action,.validation,.provenance,
.metric,.stop{break-inside:avoid}.section-heading{break-after:avoid}.validation-grid{
display:block}.validation{margin:10px 0}.badge{border:1px solid #ccc;padding:2px 6px}
details{break-inside:avoid}details>pre{display:block!important}
details::details-content{content-visibility:visible!important}summary{font-size:9pt}
pre{font-size:8pt;white-space:pre-wrap;overflow:visible;overflow-wrap:anywhere}
.patch{background:#f7f9f7;color:#172b2b;border-color:#dce4df}.fineprint,.footer{font-size:8pt}}
"""


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _html(value: object) -> str:
    return escape(_text(value), quote=True)


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _markdown_text(value: object) -> str:
    """Render configuration values as prose even if metadata contains markup."""
    text = escape(_text(value), quote=False).replace("\r", " ").replace("\n", " ")
    for character in ("\\", "`", "*", "_", "[", "]", "(", ")"):
        text = text.replace(character, "\\" + character)
    return text


def _provider_details(run: dict, report: dict) -> list[tuple[str, object]]:
    source = _mapping(run.get("source"))
    provider_value = report.get("provider") or source.get("provider") or run.get("provider")
    provider = {}
    # A report can record executed model details while the source carries the
    # original credential provenance; retain both without exposing any key.
    for value in (run.get("provider"), source.get("provider"), report.get("provider")):
        provider.update(_mapping(value))
    details = []
    if provider_value:
        details.append(
            (
                "Review provider",
                provider.get("name") or provider.get("provider") or _text(provider_value),
            )
        )
    model = provider.get("model") or report.get("model") or source.get("model")
    if model or "model" in provider:
        details.append(("Model", model or "No model configured"))
    if "reasoning_effort" in provider:
        effort = provider["reasoning_effort"]
        details.append(
            (
                "Reasoning effort",
                "Provider default / not applicable" if effort is None else _humanize(effort),
            )
        )
    if "credential_source" in provider:
        credentials = {
            "byok": "Run-only own key",
            "environment": "Server environment",
            "none": "No provider key",
        }
        details.append(
            (
                "Credential source",
                credentials.get(_text(provider["credential_source"]), "Not recorded"),
            )
        )
    if provider.get("mode"):
        details.append(("Provider mode", provider["mode"]))
    return details


def _items(value: object) -> list:
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, dict):
        return [value] if value else []
    return [] if value is None or value == "" else [value]


def _count(value: object, fallback: int = 0) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        count = int(value)
        return count if count >= 0 else fallback
    except (TypeError, ValueError, OverflowError):
        return fallback


def _humanize(value: object) -> str:
    text = _text(value).replace("_", " ").replace("-", " ")
    return text[:1].upper() + text[1:] if text else "Unknown"


def _badge(value: object, label: object | None = None) -> str:
    status = _text(value).casefold()
    if status in {"improved", "resolved", "accepted", "applied", "passed", "fixed"}:
        tone = "positive"
    elif status in {"stopped", "needs_review", "not_attempted", "pending", "medium", "unresolved"}:
        tone = "warning"
    elif status in {"failed", "rejected", "high", "error"}:
        tone = "negative"
    elif status in {"queued", "running"}:
        tone = "active"
    else:
        tone = "neutral"
    # Dynamic data can only become text. Class names always come from fixed literals.
    return f'<span class="badge {tone}">{_html(label if label is not None else _humanize(value))}</span>'


def _evidence(value: object, title: str = "Inspect evidence") -> str:
    content = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return f"<details><summary>{_html(title)}</summary><pre><code>{_html(content)}</code></pre></details>"


def _date(value: object) -> str:
    raw = _text(value)
    try:
        date = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        timezone = date.tzname() or ""
        return date.strftime("%d %b %Y · %H:%M") + (f" {timezone}" if timezone else "")
    except ValueError:
        return raw or "Date not recorded"


def _finding(item: object, index: int) -> str:
    finding = _mapping(item)
    title = finding.get("message") or finding.get("title") or finding.get("rule") or _text(item)
    location = _text(finding.get("path") or finding.get("file") or "Location not recorded")
    if finding.get("line") is not None:
        location += ":" + _text(finding["line"])
    if finding.get("rule"):
        location += " · " + _text(finding["rule"])
    severity = finding.get("severity", "unknown")
    status = finding.get("status", "unresolved")
    attempts = _count(finding.get("attempts"))
    rationale = finding.get("rationale") or "No corrective rationale was recorded."
    return (
        '<article class="finding">'
        '<div class="card-header">'
        f'<h3>{index}. {_html(title or "Review finding")}</h3><div class="tags">'
        + _badge(severity, _humanize(severity) + " severity")
        + _badge(status)
        + "</div></div>"
        + f'<p class="location">{_html(location)}</p>'
        + f'<p class="rationale"><span class="label">Rationale:</span> {_html(rationale)}</p>'
        + f'<p class="fineprint">{attempts} remediation attempt(s) recorded.</p>'
        + _evidence(item, "Inspect finding record")
        + "</article>"
    )


def _action(item: object, index: int) -> str:
    action = _mapping(item)
    accepted = action.get("accepted")
    status = "accepted" if accepted is True else "rejected" if accepted is False else "recorded"
    location = action.get("path") or action.get("file") or f"Action {index}"
    attempt = _count(action.get("attempt"))
    title = _text(location) + (f" · Attempt {attempt}" if attempt else "")
    method = _humanize(action.get("method", "Method not recorded"))
    reason = (
        action.get("rationale")
        or action.get("reason")
        or action.get("detail")
        or ("No action rationale was recorded." if action else _text(item))
    )
    rollback = (
        '<p class="rollback">The rejected candidate was rolled back; its changes were not retained.</p>'
        if action.get("rolled_back") is True
        else ""
    )
    return (
        '<article class="action"><div class="card-header">'
        f'<h3>{_html(title)}</h3><div class="tags">'
        + _badge(status)
        + _badge("neutral", method)
        + "</div></div>"
        + f'<p class="reason">{_html(reason)}</p>'
        + rollback
        + _evidence(item, "Inspect action record")
        + "</article>"
    )


def _validation(item: object, index: int) -> str:
    record = _mapping(item)
    kind = record.get("kind") or record.get("name") or record.get("check") or f"Validation {index}"
    category = (_text(kind) + " " + _text(record.get("category"))).casefold()
    behavioral = any(word in category for word in ("sandbox", "behavior", "tests", "test_"))
    category_label = (
        "Behavioral evidence"
        if behavioral
        else (
            "Static evidence"
            if any(word in category for word in ("syntax", "lint", "ruff", "ast", "static"))
            else "Verification record"
        )
    )
    passed = record.get("passed")
    status = (
        "passed"
        if passed is True
        else "failed"
        if passed is False
        else record.get("status", "recorded")
    )
    status_label = "Baseline established" if kind == "baseline_lint" and passed is True else None
    details = (
        record.get("details")
        or record.get("detail")
        or record.get("summary")
        or record.get("reason")
    )
    if not details:
        details = "See the recorded evidence." if record else _text(item)
    readable = (
        _text(details)
        if not isinstance(details, (dict, list))
        else "Structured evidence is available below."
    )
    counters = []
    for key, label in (
        ("files", "source files"),
        ("findings", "existing findings"),
        ("tests", "tests"),
        ("skipped", "skipped tests"),
    ):
        if isinstance(record.get(key), (int, float)) and not isinstance(record.get(key), bool):
            counters.append(f"{_text(record[key])} {label}")
    if record.get("phase"):
        counters.insert(0, "Phase: " + _humanize(record["phase"]))
    neutral_baseline = _badge("neutral", status_label) if status_label else _badge(status)
    return (
        '<article class="validation">'
        f'<p class="eyebrow">{category_label}</p><div class="card-header">'
        f"<h3>{_html(_humanize(kind))}</h3>{neutral_baseline}</div>"
        f'<p class="reason">{_html(readable)}</p>'
        + (f'<p class="fineprint">{_html(" · ".join(counters))}</p>' if counters else "")
        + _evidence(item)
        + "</article>"
    )


def html_report(run: dict) -> str:
    """Export a self-contained report; all repository and model data stays escaped text."""
    run = _mapping(run)
    report = _mapping(run.get("report"))
    source_value = run.get("source")
    source = _mapping(source_value)
    source_name = source.get("repository") or source.get("name") or source.get("reference")
    source_name = source_name or (_text(source_value) if not source else "Source not recorded")
    findings = _items(report.get("findings"))
    actions = _items(report.get("actions"))
    validations = _items(report.get("validation"))
    metrics = _mapping(report.get("metrics"))
    status = report.get("status") or run.get("status") or "pending"
    titles = {
        "improved": "Verified improvements, ready for review.",
        "reviewed": "Repository review complete.",
        "stopped": "Review stopped at a guardrail.",
        "failed": "Review ended with an error.",
    }
    title = titles.get(_text(status), "Repository review report")
    summary = report.get("summary") or "This review has not produced a completed report."
    accepted = _count(
        metrics.get("accepted_changes"),
        sum(_mapping(item).get("accepted") is True for item in actions),
    )
    attempts = _count(
        metrics.get("attempts"), sum(_count(_mapping(item).get("attempts")) for item in findings)
    )
    unresolved = _count(
        metrics.get("unresolved"),
        sum(
            _text(_mapping(item).get("status")) not in {"resolved", "fixed", "applied", "accepted"}
            for item in findings
        ),
    )
    kpis = (
        ("Findings", _count(metrics.get("findings"), len(findings)), "Prioritized review items"),
        ("Accepted changes", accepted, "Retained after verification"),
        ("Attempts", attempts, "Bounded remediation attempts"),
        ("Unresolved", unresolved, "Require further review"),
    )
    metric_html = "".join(
        f'<dl class="metric"><dt>{label}</dt><dd>{value}</dd><dd class="metric-detail">{detail}</dd></dl>'
        for label, value, detail in kpis
    )
    provenance = []
    for label, key in (
        ("Source type", "type"),
        ("Branch", "branch"),
        ("Immutable revision", "revision"),
    ):
        if source.get(key) is not None:
            provenance.append((label, source[key]))
    if metrics.get("files_analyzed") is not None:
        provenance.append(("Source files analyzed", _count(metrics["files_analyzed"])))
    provenance.extend(_provider_details(run, report))
    provenance_html = (
        (
            '<dl class="provenance">'
            + "".join(
                f"<div><dt>{label}</dt><dd>{_html(value)}</dd></div>" for label, value in provenance
            )
            + "</dl>"
        )
        if provenance
        else ""
    )
    finding_html = "".join(_finding(item, index) for index, item in enumerate(findings, 1)) or (
        '<p class="empty">No findings were recorded within the supported checks. '
        "This does not establish that the repository is free of defects.</p>"
    )
    action_html = "".join(_action(item, index) for index, item in enumerate(actions, 1)) or (
        '<p class="empty">No corrective changes were attempted or accepted in this review.</p>'
    )
    validation_html = (
        '<div class="validation-grid">'
        + "".join(_validation(item, index) for index, item in enumerate(validations, 1))
        + "</div>"
        if validations
        else (
            '<p class="empty">No validation evidence was recorded. Behavioral correctness has not been established.</p>'
        )
    )
    stop_reason = report.get("stop_reason") or "No stop reason was recorded."
    limitations = _items(report.get("limitations"))
    limitation_html = (
        '<ul class="limitations">'
        + "".join(f"<li>{_html(item)}</li>" for item in limitations)
        + "</ul>"
        if limitations
        else '<p class="fineprint">No additional limitations were recorded.</p>'
    )
    patch = _text(report.get("patch"))
    patch_html = (
        f'<pre class="patch"><code>{_html(patch)}</code></pre>'
        if patch
        else ('<p class="empty">No accepted changes are available as a patch.</p>')
    )
    public_evidence = {key: value for key, value in run.items() if key != "workspace"}
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; '
        'style-src &#39;unsafe-inline&#39;; base-uri &#39;none&#39;; form-action &#39;none&#39;">'
        f"<title>CodeProof review report · {_html(source_name)}</title>"
        f'<style>{_HTML_STYLES}</style></head><body><main class="report">'
        '<header class="masthead"><div class="brand"><span class="monogram" aria-hidden="true">CP</span>CodeProof</div>'
        '<p class="eyebrow">Repository review &amp; remediation</p>'
        f'<h1>{title}</h1><div class="masthead-meta"><span class="source-name">{_html(source_name)}</span>'
        f"<time>{_html(_date(run.get('created_at')))}</time>{_badge(status)}</div></header>"
        '<div class="content"><section class="section" aria-labelledby="outcome-heading">'
        '<p class="eyebrow">Review outcome</p><h2 id="outcome-heading" class="summary">'
        f'{_html(summary)}</h2><div class="metrics">{metric_html}</div>{provenance_html}</section>'
        '<section class="section" aria-labelledby="findings-heading"><div class="section-heading">'
        '<h2 id="findings-heading">Findings &amp; rationale</h2>'
        f'<span class="section-note">{len(findings)} recorded item(s)</span></div>{finding_html}</section>'
        '<section class="section" aria-labelledby="actions-heading"><div class="section-heading">'
        '<h2 id="actions-heading">Actions taken</h2><span class="section-note">Decision and retry history</span>'
        f"</div>{action_html}</section>"
        '<section class="section" aria-labelledby="validation-heading"><div class="section-heading">'
        '<h2 id="validation-heading">Validation evidence</h2><span class="section-note">Static and behavioral evidence</span>'
        f"</div>{validation_html}</section>"
        '<section class="section" aria-labelledby="coverage-heading"><div class="section-heading">'
        '<h2 id="coverage-heading">Stop reason &amp; limitations</h2></div>'
        f'<p class="stop">{_html(stop_reason)}</p>{limitation_html}</section>'
        '<section class="section" aria-labelledby="patch-heading"><div class="section-heading">'
        '<h2 id="patch-heading">Appendix · accepted patch</h2><span class="section-note">Unified diff</span></div>'
        f"{patch_html}</section>"
        '<section class="section" aria-labelledby="record-heading"><div class="section-heading">'
        '<h2 id="record-heading">Complete review record</h2></div>'
        '<p class="fineprint">The complete recorded evidence is preserved below for independent inspection. '
        "Static checks and executed behavioral tests are identified separately.</p>"
        + _evidence(public_evidence, "Inspect complete review evidence")
        + '</section></div><footer class="footer"><span>CodeProof · Evidence-based repository improvement</span>'
        + f'<span class="run-id">Run {_html(run.get("id", "Not recorded"))}</span>'
        + "</footer></main></body></html>"
    )
