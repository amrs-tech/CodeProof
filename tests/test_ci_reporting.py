from pathlib import Path
from runpy import run_path

helper = run_path(str(Path(__file__).resolve().parents[1] / "scripts/report_test_failures.py"))
annotations = helper["annotations"]
summary_annotation = helper["summary_annotation"]


def test_ci_annotations_omit_failure_bodies_and_escape_command_injection(tmp_path):
    report = tmp_path / "results.xml"
    report.write_text(
        '<testsuite><testcase classname="tests.module" name="bad&#10;::warning::injected%">'
        '<failure message="private-token">private-source-and-token</failure></testcase>'
        '<testcase classname="tests.module" name="passed"/></testsuite>'
    )
    result = annotations(report)
    assert result == ["::error title=Failing test::tests.module::bad%0A::warning::injected%25"]
    assert "private" not in result[0]
    assert "\n" not in result[0]


def test_ci_annotations_ignore_missing_report_and_limit_failure_volume(tmp_path):
    assert annotations(tmp_path / "missing.xml") == []
    report = tmp_path / "results.xml"
    report.write_text(
        "<testsuite>" + '<testcase name="failed"><error/></testcase>' * 30 + "</testsuite>"
    )
    assert len(annotations(report)) == 20


def test_ci_summary_includes_only_outcome_counts(tmp_path):
    report = tmp_path / "results.xml"
    report.write_text(
        '<testsuite><testcase name="private-name"/>'
        "<testcase><failure>private-source</failure></testcase>"
        "<testcase><error>private-key</error></testcase>"
        '<testcase><skipped message="private-reason"/></testcase></testsuite>'
    )
    assert summary_annotation(report) == (
        '::notice title=Verification summary::{"tests": 4, "failures": 1, '
        '"errors": 1, "skipped": 1}'
    )
    assert summary_annotation(tmp_path / "missing.xml") is None
