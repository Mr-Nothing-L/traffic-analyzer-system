"""FastAPI application factory for the traffic analyzer web UI.

``create_app()`` is referenced by the ``web`` CLI subcommand via uvicorn's
factory mode; a preset workspace is passed through the
``TRAFFIC_ANALYZER_WEB_WORKSPACE`` environment variable (factory mode cannot
forward arguments).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from traffic_analyzer.web import (
    evaluate,
    evidence_api,
    frames,
    jobs,
    workspace as workspace_mod,
)

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
WORKSPACE_ENV_VAR = "TRAFFIC_ANALYZER_WEB_WORKSPACE"


def create_app(workspace: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="Traffic Analyzer Web UI")
    app.state.workspace = workspace_mod.WorkspaceState()
    app.state.jobs = jobs.JobManager()

    preset = workspace or os.environ.get(WORKSPACE_ENV_VAR)
    if preset:
        path = Path(preset).expanduser().resolve()
        if path.is_dir():
            app.state.workspace.set(path)
        else:
            logger.warning("Preset workspace is not a directory, ignored: %s", preset)

    app.include_router(workspace_mod.router)
    app.include_router(jobs.router)
    app.include_router(evidence_api.router)
    app.include_router(frames.router)
    app.include_router(evaluate.router)

    # Static frontend (developed in parallel) — must not crash when missing.
    try:
        app.mount(
            "/",
            StaticFiles(directory=str(_STATIC_DIR), html=True, check_dir=False),
            name="static",
        )
    except Exception:
        logger.warning("Static directory unavailable, frontend not served: %s", _STATIC_DIR)

    return app
