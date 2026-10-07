"""One bounded durable queue consumer; interrupted jobs never restart silently."""

import logging
from pathlib import Path
from threading import Event

from codeproof.config import Settings
from codeproof.errors import RunAborted
from codeproof.providers import ProviderConfigurationError, RunSettingsVault, recover_run_settings

logger = logging.getLogger(__name__)


class WorkerOwnershipLost(RunAborted):
    pass


def work(store, settings: Settings, stop: Event, vault: RunSettingsVault | None = None) -> None:
    from codeproof.engine import run_review

    while not stop.is_set():
        try:
            if not store.worker_lock_healthy():
                logger.error("Worker ownership lost; stopping without modifying active jobs.")
                return
            run = store.claim_run()
        except Exception:
            logger.error("Queue polling failed; stopping worker. Restart after database recovery.")
            return
        if not run:
            stop.wait(settings.worker_poll_seconds)
            continue
        run_id = run["id"]

        def progress(stage, detail, rid=run_id):
            if not store.worker_lock_healthy():
                raise WorkerOwnershipLost
            store.update_progress(rid, stage, detail)

        run_config = None
        try:
            run_config = vault.pop(run_id) if vault is not None else None
            if run_config is None:
                run_config = recover_run_settings(
                    settings, run.get("source", {}).get("provider", {})
                )
            workspace = Path(run["workspace"]).resolve()
            root = settings.data_dir.resolve()
            if not workspace.is_relative_to(root) or workspace == root:
                raise ValueError("Run workspace is outside the configured storage directory")
            progress("indexing", "Indexing source for repository context")
            store.index_workspace(run_id, workspace)
            report = run_review(
                workspace,
                run_config,
                context_search=lambda query, rid=run_id: store.search_context(rid, query),
                progress=progress,
            )
            if not store.worker_lock_healthy():
                raise WorkerOwnershipLost
            report["provider"] = run.get("source", {}).get("provider", report.get("provider", {}))
            store.finish_run(run_id, report)
        except WorkerOwnershipLost:
            logger.error("Worker ownership lost; interrupted review left for restart recovery.")
            return
        except Exception as exc:
            # Repository/provider content and credentials must not appear in server logs.
            logger.error("Run %s failed (%s)", run_id, type(exc).__name__)
            try:
                if not store.worker_lock_healthy():
                    return
                store.fail_run(
                    run_id,
                    str(exc)
                    if isinstance(exc, ProviderConfigurationError)
                    else "Review could not finish safely. Check local service configuration.",
                )
            except Exception:
                logger.error("Could not persist failed run status; worker is stopping.")
                return
        finally:
            # Release a run-only credential even when validation or persistence fails.
            run_config = None
