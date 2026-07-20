"""FastAPI service for the Spain housing dashboard.

Serves one JSON endpoint (``/api/dataset``) plus the static single-page
dashboard from ``web/``. The page does all map ↔ time-series cross-filtering
client-side, so this service stays tiny: shape the data, hand over the file.

The dashboard reads the same Postgres database the ingest writes to; with no
housing rows yet (or no database at all) it serves clearly-labelled sample data
so the page always renders. Run it with:
    python -m fintracker.webapp
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from fintracker.config import get_settings
from fintracker.housing.dataset import build_dataset

log = logging.getLogger(__name__)


def _web_dir() -> Path | None:
    """Locate the static ``web/`` directory (env override, cwd, or repo root)."""
    override = os.environ.get("HOUSING_WEB_DIR", "").strip()
    candidates = [
        Path.cwd() / "web",
        Path(__file__).resolve().parents[2] / "web",
    ]
    if override:
        candidates.insert(0, Path(override))
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    log.warning("web/ directory not found in: %s", ", ".join(map(str, candidates)))
    return None


def create_app() -> FastAPI:
    app = FastAPI(title="Spain Housing Dashboard", docs_url=None, redoc_url=None)

    @app.get("/api/dataset")
    def dataset() -> JSONResponse:
        return JSONResponse(build_dataset())

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # Static SPA last so /api and /healthz win; html=True serves index.html at /.
    web_dir = _web_dir()
    if web_dir is not None:
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log.info(
        "Serving Spain housing dashboard on %s:%s",
        settings.housing_web_host,
        settings.housing_web_port,
    )
    uvicorn.run(
        app,
        host=settings.housing_web_host,
        port=settings.housing_web_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
