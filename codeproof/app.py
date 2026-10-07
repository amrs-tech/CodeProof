"""Local UI and authenticated, bounded submission API."""

import asyncio
import hmac
import json
import logging
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event, Thread
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from codeproof.config import Settings, settings
from codeproof.reporting import markdown_report
from codeproof.worker import work

logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class BodyLimitMiddleware:
    def __init__(self, app, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        declared = headers.get(b"content-length", b"0")
        if len(declared) > 20 or not declared.isdigit() or int(declared) > self.limit:
            response = Response(
                '{"detail":"Upload exceeds the configured limit"}',
                status_code=413,
                media_type="application/json",
            )
            return await response(scope, receive, send)
        used = 0

        async def bounded_receive():
            nonlocal used
            message = await receive()
            used += len(message.get("body", b""))
            if used > self.limit:
                raise HTTPException(413, "Upload exceeds the configured limit")
            return message

        await self.app(scope, bounded_receive, send)


def public_run(run: dict) -> dict:
    return {key: value for key, value in run.items() if key != "workspace"}


def create_app(config: Settings = settings, store=None, start_worker: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from codeproof.storage import Store

        app.state.store = store if store is not None else Store(config)
        stop = Event()
        thread = None
        try:
            app.state.store.initialize()
            if start_worker:
                if not app.state.store.acquire_worker_lock():
                    raise RuntimeError("Another CodeProof worker is already using this database")
                app.state.store.recover_interrupted()
                thread = Thread(target=work, args=(app.state.store, config, stop), daemon=True)
                thread.start()
            config.data_dir.mkdir(parents=True, exist_ok=True)
            yield
        finally:
            stop.set()
            if thread:
                # Shutdown waits for the bounded active review before closing its database pool.
                await asyncio.to_thread(thread.join, config.max_run_seconds + 30)
            app.state.store.close()

    app = FastAPI(title="CodeProof", version="0.1.0", lifespan=lifespan)
    app.add_middleware(BodyLimitMiddleware, limit=config.max_upload_bytes + 1024 * 1024)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"]
    )
    app.state.ingestion_slots = asyncio.Semaphore(2)

    @app.middleware("http")
    async def access_guard(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.url.path != "/api/health":
            if config.api_token:
                expected = f"Bearer {config.api_token}"
                if not hmac.compare_digest(
                    request.headers.get("authorization", "").encode("utf-8"),
                    expected.encode("utf-8"),
                ):
                    return Response(
                        '{"detail":"API token required"}',
                        status_code=401,
                        media_type="application/json",
                    )
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                origin = request.headers.get("origin")
                try:
                    origin_host = urlsplit(origin).netloc if origin else None
                except ValueError:
                    origin_host = "invalid"
                if request.headers.get("sec-fetch-site") == "cross-site" or (
                    origin and origin_host != request.headers.get("host")
                ):
                    return Response(
                        '{"detail":"Cross-origin submission is not allowed"}',
                        status_code=403,
                        media_type="application/json",
                    )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    def health(request: Request):
        # A DB query makes this readiness evidence, not merely an HTTP liveness check.
        request.app.state.store.list_runs(1)
        return {
            "database": "ready",
            "provider_configured": bool(config.llm_model and config.llm_api_key),
            "sandbox_enabled": config.sandbox_enabled,
            "max_upload_bytes": config.max_upload_bytes,
        }

    @app.post("/api/runs", status_code=202)
    async def submit(
        request: Request,
        repository: Annotated[str | None, Form()] = None,
        file: Annotated[UploadFile | None, File()] = None,
    ):
        from codeproof.ingestion import IngestionError, ingest_github, ingest_zip

        repository = (repository or "").strip()
        if bool(repository) == bool(file):
            raise HTTPException(422, "Provide exactly one public GitHub repository or ZIP file")
        if repository and len(repository) > 300:
            raise HTTPException(422, "Repository reference is too long")
        if file and not (file.filename or "").lower().endswith(".zip"):
            raise HTTPException(422, "Upload a source code .zip file")
        if app.state.ingestion_slots.locked():
            raise HTTPException(429, "Intake is busy; retry after the current uploads finish")
        async with app.state.ingestion_slots:
            run_id = str(uuid4())
            run_root = config.data_dir.resolve() / run_id
            run_root.mkdir(parents=True)
            archive = run_root / "upload.zip"
            workspace = run_root / "source"
            try:
                if file:
                    size = 0
                    with archive.open("wb") as target:
                        while chunk := await file.read(64 * 1024):
                            size += len(chunk)
                            if size > config.max_upload_bytes:
                                raise HTTPException(413, "Upload exceeds the configured limit")
                            target.write(chunk)
                    metadata = await asyncio.to_thread(ingest_zip, archive, workspace, config)
                    source = {"type": "zip", "name": Path(file.filename).name, **metadata}
                    archive.unlink(missing_ok=True)
                else:
                    source = await asyncio.to_thread(ingest_github, repository, workspace, config)
                run = await asyncio.to_thread(
                    request.app.state.store.create_run, run_id, source, workspace
                )
                return public_run(run)
            except (IngestionError, ValueError) as exc:
                shutil.rmtree(run_root)
                raise HTTPException(422, str(exc)) from None
            except HTTPException:
                shutil.rmtree(run_root)
                raise
            except Exception as exc:
                shutil.rmtree(run_root)
                logger.error("Submission failed (%s)", type(exc).__name__)
                raise HTTPException(503, "Submission failed; check service configuration") from None
            finally:
                if file:
                    await file.close()

    def lookup(request: Request, run_id: str):
        try:
            UUID(run_id)
        except ValueError:
            raise HTTPException(404, "Run not found") from None
        run = request.app.state.store.get_run(run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        return run

    @app.get("/api/runs")
    def history(request: Request):
        return [public_run(run) for run in request.app.state.store.list_runs()]

    @app.get("/api/runs/{run_id}")
    def detail(request: Request, run_id: str):
        return public_run(lookup(request, run_id))

    @app.get("/api/runs/{run_id}/report")
    def report_download(request: Request, run_id: str, format: str = "markdown"):
        run = lookup(request, run_id)
        if not run.get("report"):
            raise HTTPException(409, "The review report is not ready")
        if format not in {"markdown", "json"}:
            raise HTTPException(422, "Report format must be markdown or json")
        content = (
            markdown_report(run)
            if format == "markdown"
            else json.dumps(public_run(run), indent=2, default=str)
        )
        extension = "md" if format == "markdown" else "json"
        return Response(
            content,
            media_type="text/markdown" if extension == "md" else "application/json",
            headers={
                "Content-Disposition": f'attachment; filename="codeproof-{run_id}.{extension}"'
            },
        )

    @app.get("/api/runs/{run_id}/patch")
    def patch_download(request: Request, run_id: str):
        run = lookup(request, run_id)
        if not run.get("report"):
            raise HTTPException(409, "The patch is not ready")
        return Response(
            run["report"].get("patch", ""),
            media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="codeproof-{run_id}.patch"'},
        )

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")
    return app


app = create_app()
