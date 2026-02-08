"""
FastAPI application factory for ClientCloak web UI.

Follows the PlaybookRedliner pattern: a create_app() factory function
that wires up routes, static files, templates, and startup hooks.
"""

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..sessions import cleanup_expired_sessions
from .routes.cloak import router as cloak_router
from .routes.uncloak import router as uncloak_router

logger = structlog.get_logger(__name__)

# Resolve paths relative to this file's directory
_UI_DIR = Path(__file__).parent
_STATIC_DIR = _UI_DIR / "static"
_TEMPLATES_DIR = _UI_DIR / "templates"


def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application.

    - Mounts static files at /static
    - Configures Jinja2 templates
    - Includes cloak and uncloak routers under /api prefix
    - Registers startup hook for session cleanup
    - Serves index.html at GET /
    """
    application = FastAPI(
        title="ClientCloak",
        description="Bidirectional document sanitization for safe AI contract review.",
        version=__version__,
    )

    # --- Mount static files ---
    _STATIC_DIR.mkdir(parents=True, exist_ok=True)
    application.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # --- Templates ---
    _TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    # --- Include API routers ---
    application.include_router(cloak_router, prefix="/api")
    application.include_router(uncloak_router, prefix="/api")

    # --- Startup hook ---
    @application.on_event("startup")
    async def on_startup():
        removed = cleanup_expired_sessions()
        if removed:
            logger.info("Cleaned up expired sessions", count=removed)
        logger.info("ClientCloak web server started")

    # --- Root route ---
    @application.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(
            "index.html", {"request": request, "version": __version__}
        )

    return application


# Module-level app instance for uvicorn (e.g., `uvicorn clientcloak.ui.app:app`)
app = create_app()


def start_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    open_browser: bool = True,
) -> None:
    """
    Start the uvicorn server and optionally open the browser.

    Args:
        host: Bind address. Defaults to localhost.
        port: Port number. Defaults to 8000.
        open_browser: If True, opens the default browser to the app URL
            after a short delay.
    """
    if open_browser:
        url = f"http://{host}:{port}"

        def _open():
            import time
            time.sleep(1.5)
            webbrowser.open(url)

        thread = threading.Thread(target=_open, daemon=True)
        thread.start()

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
    )
