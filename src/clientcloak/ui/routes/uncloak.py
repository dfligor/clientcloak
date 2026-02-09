"""
Uncloaking API routes for ClientCloak.

Handles uploading a redlined document with its mapping file, running the
uncloaking pipeline, and downloading the restored document.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ...mapping import load_mapping
from ...sessions import create_session, get_session_dir, get_session_file
from ...uncloaker import uncloak_document

logger = structlog.get_logger(__name__)

router = APIRouter()

# Maximum upload file sizes (server-side enforcement).
_MAX_DOCX_BYTES = 100 * 1024 * 1024   # 100 MB
_MAX_MAPPING_BYTES = 10 * 1024 * 1024  # 10 MB


@router.post("/uncloak")
async def uncloak(
    redlined_file: UploadFile = File(...),
    mapping_file: UploadFile = File(...),
):
    """
    Uncloak a redlined document using its mapping file.

    Accepts two file uploads:
    - redlined_file: The cloaked/redlined .docx document
    - mapping_file: The JSON mapping file generated during cloaking

    Creates a session, saves both files, runs uncloak_document, and
    returns the download URL for the restored document.
    """
    # --- Validate redlined file type ---
    if not redlined_file.filename:
        raise HTTPException(status_code=400, detail="No filename provided for redlined file.")

    if not redlined_file.filename.lower().endswith(".docx"):
        raise HTTPException(
            status_code=400,
            detail="Redlined file must be .docx.",
        )

    # --- Validate mapping file type ---
    if not mapping_file.filename:
        raise HTTPException(status_code=400, detail="No filename provided for mapping file.")

    if not mapping_file.filename.lower().endswith(".json"):
        raise HTTPException(
            status_code=400,
            detail="Mapping file must be .json.",
        )

    # --- Create session and save files ---
    try:
        session_id = create_session()
    except Exception as exc:
        logger.error("Failed to create session", error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to create session.") from exc

    try:
        redlined_path = get_session_file(session_id, "redlined.docx")
        redlined_content = await redlined_file.read()
        if len(redlined_content) > _MAX_DOCX_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Document too large. Maximum upload size is 100 MB.",
            )
        redlined_path.write_bytes(redlined_content)

        mapping_path = get_session_file(session_id, "mapping.json")
        mapping_content = await mapping_file.read()
        if len(mapping_content) > _MAX_MAPPING_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Mapping file too large. Maximum size is 10 MB.",
            )
        mapping_path.write_bytes(mapping_content)

        logger.info(
            "Files uploaded for uncloaking",
            session_id=session_id,
        )
    except Exception as exc:
        logger.error("Failed to save uploaded files", session_id=session_id, error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to save uploaded files.") from exc

    # --- Run uncloaking ---
    output_path = get_session_file(session_id, "uncloaked.docx")

    try:
        replacements_restored = uncloak_document(
            input_path=redlined_path,
            output_path=output_path,
            mapping_path=mapping_path,
        )
        logger.info(
            "Uncloaking complete",
            session_id=session_id,
            replacements_restored=replacements_restored,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Source file not found.") from exc
    except Exception as exc:
        logger.error("Uncloaking failed", session_id=session_id, error=str(exc))
        raise HTTPException(status_code=500, detail="Uncloaking failed. Please try again.") from exc

    # --- Compute restored filename ---
    # Apply the mapping (placeholder -> original) to the uploaded filename
    # so the download has the real party names back.
    uncloaked_filename = "uncloaked.docx"
    try:
        if redlined_file.filename:
            stem = redlined_file.filename.rsplit(".", 1)[0]
            # Remove _cloaked suffix if present
            if stem.endswith("_cloaked"):
                stem = stem[:-len("_cloaked")]
            mapping = load_mapping(mapping_path)
            # mapping.mappings is placeholder -> original
            for placeholder, original in sorted(
                mapping.mappings.items(), key=lambda kv: len(kv[0]), reverse=True
            ):
                stem = stem.replace(placeholder, original)
            uncloaked_filename = f"{stem}_uncloaked.docx"
    except Exception:
        pass

    # Save the restored filename for the download endpoint
    try:
        name_file = get_session_file(session_id, ".uncloaked_filename")
        name_file.write_text(uncloaked_filename, encoding="utf-8")
    except Exception:
        pass

    return JSONResponse(content={
        "session_id": session_id,
        "replacements_restored": replacements_restored,
        "download_url": f"/api/download-uncloaked/{session_id}",
        "uncloaked_filename": uncloaked_filename,
    })


@router.get("/download-uncloaked/{session_id}")
async def download_uncloaked(session_id: str):
    """
    Download the uncloaked (restored) document from a session.
    """
    # --- Validate session ---
    try:
        session_dir = get_session_dir(session_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    file_path = session_dir / "uncloaked.docx"

    if not file_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Uncloaked file not found. Please run the uncloaking operation first.",
        )

    # Try to use the restored filename
    download_name = "uncloaked.docx"
    try:
        name_file = session_dir / ".uncloaked_filename"
        if name_file.is_file():
            download_name = name_file.read_text(encoding="utf-8").strip()
    except Exception:
        pass

    return FileResponse(
        path=str(file_path),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=download_name,
    )
