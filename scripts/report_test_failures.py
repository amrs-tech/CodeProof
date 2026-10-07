"""Expose failed test names in CI annotations without publishing failure bodies or secrets."""

import sys
from pathlib import Path
from xml.etree import ElementTree


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
    for annotation in annotations(Path(sys.argv[1])):
        print(annotation)
