import io
import os
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from codeproof.app import create_app
from codeproof.config import Settings


@pytest.mark.integration
def test_zip_to_persisted_review_and_downloads(tmp_path):
    database_url = os.environ.get("CODEPROOF_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set CODEPROOF_TEST_DATABASE_URL to test real PostgreSQL + pgvector")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("demo/greeting.py", 'def greet():\n    return f"hello"\n')
    config = Settings(database_url=database_url, data_dir=tmp_path, worker_poll_seconds=0.1)
    with TestClient(create_app(config)) as http:
        assert http.get("/api/health").json()["database"] == "ready"
        submitted = http.post("/api/runs", files={"file": ("demo.zip", archive.getvalue())})
        assert submitted.status_code == 202, submitted.text
        run_id = submitted.json()["id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            run = http.get(f"/api/runs/{run_id}").json()
            if run["status"] not in {"queued", "running"}:
                break
            time.sleep(0.1)
        assert run["status"] == "improved", run
        assert run["report"]["patch"]
        assert 'return "hello"' in run["report"]["patch"]
        assert "CodeProof review report" in http.get(f"/api/runs/{run_id}/report").text
        html = http.get(f"/api/runs/{run_id}/report?format=html")
        assert html.status_code == 200
        assert "greeting.py" in html.text
        assert "Accepted changes" in html.text
        assert http.get(f"/api/runs/{run_id}/patch").status_code == 200
        assert any(item["id"] == run_id for item in http.get("/api/runs").json())
