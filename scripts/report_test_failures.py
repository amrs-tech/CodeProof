"""Expose failed test names in CI annotations without publishing failure bodies or secrets."""

import json
import sys
from pathlib import Path
from xml.etree import ElementTree


def summary_annotation(path: Path) -> str | None:
    if not path.is_file():
        return None
    cases = list(ElementTree.parse(path).getroot().iter("testcase"))
    counts = {
        "tests": len(cases),
        "failures": sum(case.find("failure") is not None for case in cases),
        "errors": sum(case.find("error") is not None for case in cases),
        "skipped": sum(case.find("skipped") is not None for case in cases),
    }
    return "::notice title=Verification summary::" + json.dumps(counts)


def annotations(path: Path) -> list[str]:
    if not path.is_file():
        return []
    root = ElementTree.parse(path).getroot()
    result = []
    for case in root.iter("testcase"):
        if case.find("failure") is None and case.find("error") is None:
            continue
        name = f"{case.get('classname', 'test')}::{case.get('name', 'unknown')}"[:400]
        name = name.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        result.append(f"::error title=Failing test::{name}")
        if len(result) == 20:
            break
    return result


if __name__ == "__main__":
    report = Path(sys.argv[1])
    summary = summary_annotation(report)
    if summary:
        print(summary)
    for annotation in annotations(report):
        print(annotation)
