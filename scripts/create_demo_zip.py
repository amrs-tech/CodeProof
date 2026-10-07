"""Create a small local demonstration source upload."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
output = root / "data" / "fragile-python.zip"
output.parent.mkdir(parents=True, exist_ok=True)
source = root / "examples" / "fragile-python"
with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
    for path in source.rglob("*.py"):
        archive.write(path, path.relative_to(source))
print(output)
