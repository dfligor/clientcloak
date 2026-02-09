"""
FastAPI application factory for ClientCloak web UI.

Follows the PlaybookRedliner pattern: a create_app() factory function
that wires up routes, static files, templates, and startup hooks.
"""

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

import sys

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .. import __version__
from ..sessions import cleanup_expired_sessions
from .routes.cloak import router as cloak_router
from .routes.uncloak import router as uncloak_router

logger = structlog.get_logger(__name__)


def _get_ui_dir() -> Path:
    """Resolve UI directory, supporting both development and PyInstaller frozen mode."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "clientcloak" / "ui"
    return Path(__file__).parent


# Resolve paths — works in both dev and PyInstaller frozen mode
_UI_DIR = _get_ui_dir()
_STATIC_DIR = _UI_DIR / "static"
_TEMPLATES_DIR = _UI_DIR / "templates"


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security-related HTTP headers to every response."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.tailwindcss.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-src 'self'"
        )
        return response


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

    # --- Security headers ---
    application.add_middleware(_SecurityHeadersMiddleware)

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
