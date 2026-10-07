import io
import zipfile
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from codeproof.app import create_app
from codeproof.config import Settings


class MemoryStore:
    """HTTP boundary tests; persistence is covered separately against real Postgres."""

    def __init__(self):
        self.runs = {}

    def initialize(self):
        pass

    def close(self):
        pass

    def create_run(self, run_id, source, workspace):
        run = {
            "id": run_id,
            "source": source,
            "workspace": str(workspace),
            "status": "queued",
            "stage": "queued",
            "detail": "Waiting",
            "report": None,
        }
        self.runs[run_id] = run
        return run

    def list_runs(self, limit=20):
        return list(self.runs.values())[:limit]

    def get_run(self, run_id):
        return self.runs.get(run_id)


def zip_bytes(name="src/main.py", content="print('hello')\n"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, content)
    return buffer.getvalue()


@pytest.fixture
def client(tmp_path):
    store = MemoryStore()
    app = create_app(Settings(data_dir=tmp_path), store, start_worker=False)
    with TestClient(app) as http:
        yield http, store


def test_submission_isolated_and_paths_hidden(client):
    http, store = client
    response = http.post("/api/runs", files={"file": ("source.zip", zip_bytes())})
    assert response.status_code == 202
    run = response.json()
    assert run["status"] == "queued"
    assert "workspace" not in run
    assert "workspace" not in http.get(f"/api/runs/{run['id']}").json()
    assert len(store.runs) == 1


def test_requires_exactly_one_input(client):
    http, _ = client
    assert http.post("/api/runs").status_code == 422
    assert (
        http.post(
            "/api/runs",
            data={"repository": "owner/repo"},
            files={"file": ("source.zip", zip_bytes())},
        ).status_code
        == 422
    )


def test_rejects_unsafe_archive_and_cleans_workspace(client, tmp_path):
    http, store = client
    assert (
        http.post(
            "/api/runs", files={"file": ("source.zip", zip_bytes("../escape.py"))}
        ).status_code
        == 422
    )
    assert not store.runs
    assert not list(tmp_path.iterdir())


def test_origin_and_host_guard(client):
    http, _ = client
    assert http.post("/api/runs", headers={"Origin": "https://evil.example"}).status_code == 403
    assert http.get("/api/runs", headers={"Host": "evil.example"}).status_code == 400
    assert http.post("/api/runs", headers={"Origin": "http://["}).status_code == 403


def test_token_required_for_private_routes(tmp_path):
    with TestClient(
        create_app(Settings(data_dir=tmp_path, api_token="local-test-token"), MemoryStore(), False)
    ) as http:
        assert http.get("/api/runs").status_code == 401
        assert http.get("/api/health").status_code == 200
        assert http.get("/api/runs", headers={"Authorization": b"Bearer\xe9"}).status_code == 401
        assert (
            http.get("/api/runs", headers={"Authorization": "Bearer local-test-token"}).status_code
            == 200
        )


def test_upload_limit_enforced(tmp_path):
    config = Settings(data_dir=tmp_path, max_upload_bytes=1024)
    with TestClient(create_app(config, MemoryStore(), False)) as http:
        assert http.post("/api/runs", headers={"Content-Length": "9" * 5000}).status_code == 413
        assert (
            http.post("/api/runs", files={"file": ("source.zip", b"x" * 1025)}).status_code == 413
        )
        assert (
            http.post(
                "/api/runs", files={"file": ("source.zip", b"x" * (2 * 1024 * 1024))}
            ).status_code
            == 413
        )


def test_report_and_patch_downloads(client):
    http, store = client
    run_id = str(uuid4())
    store.runs[run_id] = {
        "id": run_id,
        "source": {"type": "zip"},
        "status": "improved",
        "report": {
            "summary": "Fixed redundant interpolation",
            "patch": "--- a/a.py\n",
            "findings": [],
            "actions": [],
            "validation": [],
            "limitations": [],
            "stop_reason": "Complete",
            "metrics": {},
        },
    }
    assert http.get(f"/api/runs/{run_id}/patch").text == "--- a/a.py\n"
    assert "Fixed redundant interpolation" in http.get(f"/api/runs/{run_id}/report").text
    assert http.get(f"/api/runs/{run_id}/report?format=json").json()["status"] == "improved"
    html = http.get(f"/api/runs/{run_id}/report?format=html")
    assert html.status_code == 200
    assert html.headers["content-type"].startswith("text/html")
    assert html.headers["content-disposition"].endswith(f'{run_id}.html"')
    assert "style-src 'unsafe-inline'" in html.headers["content-security-policy"]
    assert "default-src 'none'" in html.headers["content-security-policy"]
    assert "Fixed redundant interpolation" in html.text
    assert "<!doctype html>" in html.text.lower()
    assert http.get(f"/api/runs/{run_id}/report?format=exe").status_code == 422
    assert http.get("/api/runs/not-a-run").status_code == 404


def test_stopped_worker_fails_readiness_and_rejects_new_jobs(tmp_path, monkeypatch):
    class WorkerStore(MemoryStore):
        def acquire_worker_lock(self):
            return True

        def recover_interrupted(self):
            pass

    monkeypatch.setattr("codeproof.app.work", lambda *_args: None)
    store = WorkerStore()
    with TestClient(create_app(Settings(data_dir=tmp_path), store)) as http:
        assert http.get("/api/health").status_code == 503
        assert http.post("/api/runs", files={"file": ("demo.zip", zip_bytes())}).status_code == 503
        assert not store.runs
        run_id = str(uuid4())
        store.runs[run_id] = {"id": run_id, "status": "running"}
        assert http.get(f"/api/runs/{run_id}").status_code == 503
        store.runs[run_id]["status"] = "reviewed"
        assert http.get(f"/api/runs/{run_id}").status_code == 200
