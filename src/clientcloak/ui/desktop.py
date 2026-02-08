"""
Native macOS window launcher for ClientCloak (commercial build only).

This file is NOT part of the open-source MIT-licensed code.
It is only included in the commercial Mac app distributed via Gumroad.

Architecture (mirrors PlaybookRedliner's desktop.py):
1. Start FastAPI server on 127.0.0.1:8000 in a daemon thread
2. Open a pywebview native WebKit window pointed at the local server
3. Expose JS API bridge for native file dialogs (open/save)
4. Handle window close -> clean shutdown

Key differences from PlaybookRedliner:
- No Ollama setup window (no LLM dependency)
- GLiNER model is pre-bundled (no download step)
- Simpler API bridge (file dialogs only, no playbook export)
"""

from __future__ import annotations

import logging
import threading

import webview

from clientcloak.ui.app import start_server

logger = logging.getLogger(__name__)


class ClientCloakAPI:
    """
    JavaScript API bridge exposed to the webview window.

    In app.js, detect native mode with: if (window.pywebview)
    Then call: window.pywebview.api.open_file_dialog()

    Returns are JSON-safe dicts (PlaybookRedliner pattern: always return
    {success: bool, data: ..., error: ...} for consistent JS handling).
    """

    def open_file_dialog(self, file_types=("Document Files (*.docx)",)):
        """Native macOS file open dialog."""
        try:
            result = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=file_types,
            )
            if result:
                return {"success": True, "data": result[0]}
            return {"success": False, "error": "cancelled"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def save_file_dialog(self, filename="document.docx"):
        """Native macOS file save dialog."""
        try:
            result = webview.windows[0].create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=filename,
            )
            if result:
                return {"success": True, "data": result}
            return {"success": False, "error": "cancelled"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def download_file(self, filename: str, url: str):
        """
        Download a file from the local server to a user-chosen location.
        Used instead of browser downloads in the native window.
        (Adapted from PlaybookRedliner's Api.download_file)
        """
        try:
            save_result = self.save_file_dialog(filename)
            if not save_result["success"]:
                return save_result
            import httpx

            response = httpx.get(url)
            with open(save_result["data"], "wb") as f:
                f.write(response.content)
            return {"success": True, "data": save_result["data"]}
        except Exception as e:
            return {"success": False, "error": str(e)}


def main():
    """Launch the native macOS desktop app."""
    # Start FastAPI server in background thread
    server_thread = threading.Thread(
        target=start_server,
        kwargs={"host": "127.0.0.1", "port": 8000, "open_browser": False},
        daemon=True,
    )
    server_thread.start()

    # Create native window
    api = ClientCloakAPI()
    webview.create_window(
        "ClientCloak",
        "http://127.0.0.1:8000",
        js_api=api,
        width=900,
        height=700,
        min_size=(700, 500),
    )

    # Start the native event loop (blocks until window is closed)
    webview.start()


if __name__ == "__main__":
    main()
