from pathlib import Path
from runpy import run_path

annotations = run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/report_test_failures.py")
)["annotations"]


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
