"""
Cloaking API routes for ClientCloak.

Handles document upload, security scanning, cloaking, and file download.
"""

from __future__ import annotations

import json

import structlog
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ...cloaker import cloak_document, preview_entities, sanitize_filename
from ...comments import inspect_comments
from ...detector import detect_entities, detect_party_names
from ...docx_handler import extract_all_text, load_document
from ...metadata import inspect_metadata
from ...models import CloakConfig, CommentMode, PartyAlias
from ...security import scan_document
from ...sessions import create_session, get_session_dir, get_session_file

logger = structlog.get_logger(__name__)

router = APIRouter()

# Maximum upload file size: 100 MB (server-side enforcement).
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024


@router.post("/upload")
async def upload_document(file: UploadFile = File(...)):
    """
    Upload a .docx file for cloaking.

    Validates the file type, creates a session, saves the file, and
    automatically runs security scan, metadata inspection, and comment
    inspection.

    Returns JSON with session_id, filename, security findings, metadata,
    and comment information.
    """
    # --- Validate file type ---
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided.")

    if not file.filename.lower().endswith(".docx"):
        raise HTTPException(
            status_code=400,
            detail="Only .docx files are accepted.",
        )

    # --- Create session and save file ---
    try:
        session_id = create_session()
    except Exception as exc:
        logger.error("Failed to create session", error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to create session.") from exc

    try:
        upload_path = get_session_file(session_id, "original.docx")
        content = await file.read()
        if len(content) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail="File too large. Maximum upload size is 100 MB.",
            )
        upload_path.write_bytes(content)
        logger.info("File uploaded", session_id=session_id, filename=file.filename)
    except Exception as exc:
        logger.error("Failed to save uploaded file", session_id=session_id, error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to save uploaded file.") from exc

    # --- Run automatic scans ---
    try:
        doc = load_document(upload_path)
    except Exception as exc:
        logger.error("Failed to load document", session_id=session_id, error=str(exc))
        raise HTTPException(
            status_code=400,
            detail="Failed to load document. Ensure it is a valid .docx file.",
        ) from exc

    # Security scan
    try:
        findings = scan_document(doc)
        security_findings = [f.model_dump() for f in findings]
    except Exception as exc:
        logger.warning("Security scan failed", session_id=session_id, error=str(exc))
        security_findings = []

    # Metadata inspection
    try:
        metadata_report = inspect_metadata(upload_path)
        metadata = metadata_report.model_dump()
    except Exception as exc:
        logger.warning("Metadata inspection failed", session_id=session_id, error=str(exc))
        metadata = {}

    # Comment inspection
    try:
        comments_list, authors_list = inspect_comments(upload_path)
        comments = {
            "authors": [a.model_dump() for a in authors_list],
            "count": len(comments_list),
        }
    except Exception as exc:
        logger.warning("Comment inspection failed", session_id=session_id, error=str(exc))
        comments = {"authors": [], "count": 0}

    # --- Extract text for entity and party detection ---
    preamble_text = ""
    detected_entities = []
    suggested_parties = []
    try:
        text_fragments = extract_all_text(doc)
        full_text = "\n".join(text for text, _source in text_fragments)

        # Preamble: first ~5 non-empty paragraphs
        non_empty = [text for text, _source in text_fragments if text.strip()]
        preamble_text = "\n".join(non_empty[:5])

        # Detect entities (no party name filtering yet — that happens at cloak time)
        entities = detect_entities(full_text)
        detected_entities = [e.model_dump() for e in entities]

        # Detect party names from preamble
        preamble_for_parties = "\n".join(non_empty[:10])  # slightly more context for party detection
        suggested_parties = detect_party_names(preamble_for_parties)
    except Exception as exc:
        logger.warning("Text extraction/detection failed", session_id=session_id, error=str(exc))

    # --- Save the original filename for later reference ---
    try:
        name_file = get_session_file(session_id, ".original_filename")
        name_file.write_text(file.filename, encoding="utf-8")
    except Exception:
        pass  # non-critical

    return JSONResponse(content={
        "session_id": session_id,
        "filename": file.filename,
        "security_findings": security_findings,
        "metadata": metadata,
        "comments": comments,
        "preamble_text": preamble_text,
        "detected_entities": detected_entities,
        "suggested_parties": suggested_parties,
    })


@router.post("/detect-entities")
async def detect_entities_route(
    session_id: str = Form(...),
    party_a: str = Form(""),
    party_b: str = Form(""),
    party_a_aliases: str = Form("[]"),
    party_b_aliases: str = Form("[]"),
):
    """
    Detect structured PII entities in a previously uploaded document.

    Returns a JSON list of detected entities with type, count, and
    suggested placeholders. Party names are filtered out since they
    are already handled by explicit party configuration.
    """
    # --- Validate session ---
    try:
        session_dir = get_session_dir(session_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    upload_path = session_dir / "original.docx"
    if not upload_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="No uploaded file found in this session. Please upload a file first.",
        )

    # --- Parse aliases ---
    try:
        parsed_a_aliases = [PartyAlias(**a) for a in json.loads(party_a_aliases)]
        parsed_b_aliases = [PartyAlias(**a) for a in json.loads(party_b_aliases)]
    except Exception as exc:
        logger.warning("Failed to parse aliases for detection", error=str(exc))
        parsed_a_aliases = []
        parsed_b_aliases = []

    # --- Build minimal config for detection ---
    config = CloakConfig(
        party_a_name=party_a or "UNUSED_PARTY_A",
        party_b_name=party_b or "UNUSED_PARTY_B",
        party_a_aliases=parsed_a_aliases,
        party_b_aliases=parsed_b_aliases,
    )

    # --- Run detection ---
    try:
        entities = preview_entities(upload_path, config)
        logger.info(
            "Entity detection complete",
            session_id=session_id,
            entities_found=len(entities),
        )
    except Exception as exc:
        logger.error("Entity detection failed", session_id=session_id, error=str(exc))
        raise HTTPException(status_code=500, detail="Entity detection failed.") from exc

    return JSONResponse(content=[e.model_dump() for e in entities])


@router.post("/cloak")
async def cloak(
    session_id: str = Form(...),
    party_a: str = Form(...),
    party_b: str = Form(...),
    party_a_label: str = Form("Customer"),
    party_b_label: str = Form("Vendor"),
    comment_mode: str = Form("strip"),
    strip_metadata: bool = Form(True),
    party_a_aliases: str = Form("[]"),
    party_b_aliases: str = Form("[]"),
    additional_replacements: str = Form("{}"),
):
    """
    Run the cloaking pipeline on a previously uploaded document.

    Accepts form data with party names, labels, comment mode, and metadata
    stripping preference. Loads the uploaded file from the session, runs
    cloak_document, and saves results to the session directory.

    Returns JSON with replacement count, mapping preview, and download URLs.
    """
    # --- Validate session ---
    try:
        session_dir = get_session_dir(session_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    upload_path = session_dir / "original.docx"
    if not upload_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="No uploaded file found in this session. Please upload a file first.",
        )

    # --- Validate comment_mode ---
    try:
        comment_mode_enum = CommentMode(comment_mode.lower())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid comment_mode: '{comment_mode}'. Must be one of: strip, anonymize, sanitize.",
        )

    # --- Parse aliases ---
    try:
        parsed_a_aliases = [PartyAlias(**a) for a in json.loads(party_a_aliases)]
        parsed_b_aliases = [PartyAlias(**a) for a in json.loads(party_b_aliases)]
    except Exception as exc:
        logger.warning("Failed to parse aliases", error=str(exc),
                       party_a_aliases=party_a_aliases, party_b_aliases=party_b_aliases)
        raise HTTPException(status_code=400, detail=f"Invalid alias JSON: {exc}")
    logger.info("Aliases parsed", party_a_count=len(parsed_a_aliases),
                party_b_count=len(parsed_b_aliases))

    # --- Parse additional replacements (entity detections) ---
    try:
        parsed_additional = json.loads(additional_replacements)
        if not isinstance(parsed_additional, dict):
            parsed_additional = {}
    except (json.JSONDecodeError, TypeError):
        parsed_additional = {}

    # --- Build config ---
    config = CloakConfig(
        party_a_name=party_a,
        party_b_name=party_b,
        party_a_label=party_a_label,
        party_b_label=party_b_label,
        party_a_aliases=parsed_a_aliases,
        party_b_aliases=parsed_b_aliases,
        additional_replacements=parsed_additional,
        comment_mode=comment_mode_enum,
        strip_metadata=strip_metadata,
    )

    output_path = session_dir / "cloaked.docx"
    mapping_path = session_dir / "mapping.json"

    # --- Build cloak replacements for filename sanitization ---
    from ...cloaker import _build_cloak_replacements
    cloak_replacements = _build_cloak_replacements(config)

    # --- Run cloaking ---
    try:
        result = cloak_document(
            input_path=upload_path,
            output_path=output_path,
            mapping_path=mapping_path,
            config=config,
        )
        logger.info(
            "Cloaking complete",
            session_id=session_id,
            replacements=result.replacements_applied,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Source file not found.") from exc
    except Exception as exc:
        logger.error("Cloaking failed", session_id=session_id, error=str(exc))
        raise HTTPException(status_code=500, detail="Cloaking failed. Please try again.") from exc

    # --- Save cloak replacements for download filename sanitization ---
    try:
        replacements_file = session_dir / ".cloak_replacements.json"
        replacements_file.write_text(json.dumps(cloak_replacements), encoding="utf-8")
    except Exception:
        pass  # non-critical

    # --- Build mapping preview (first 10 entries) ---
    mapping_preview = dict(list(result.mapping.mappings.items())[:10])

    # --- Compute sanitized filenames for frontend downloads ---
    sanitized_filename = "cloaked.docx"
    sanitized_mapping_filename = "mapping.json"
    try:
        name_file = session_dir / ".original_filename"
        if name_file.is_file():
            original_name = name_file.read_text(encoding="utf-8").strip()
            stem = original_name.rsplit(".", 1)[0]
            sanitized_stem = sanitize_filename(stem, cloak_replacements)
            sanitized_filename = f"{sanitized_stem}_cloaked.docx"
            sanitized_mapping_filename = f"{stem}_mapping.json"
    except Exception:
        pass

    return JSONResponse(content={
        "replacements_applied": result.replacements_applied,
        "mapping_preview": mapping_preview,
        "download_url": f"/api/download/{session_id}/cloaked",
        "mapping_url": f"/api/download/{session_id}/mapping",
        "sanitized_filename": sanitized_filename,
        "sanitized_mapping_filename": sanitized_mapping_filename,
    })


@router.get("/download/{session_id}/{file_type}")
async def download_file(session_id: str, file_type: str):
    """
    Download a file from a session.

    file_type must be "cloaked", "mapping", or "original".
    """
    # --- Validate file_type ---
    if file_type not in ("cloaked", "mapping", "original"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file_type: '{file_type}'. Must be 'cloaked', 'mapping', or 'original'.",
        )

    # --- Validate session ---
    try:
        session_dir = get_session_dir(session_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    # --- Determine file path ---
    if file_type == "original":
        file_path = session_dir / "original.docx"
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        download_name = "original.docx"
    elif file_type == "cloaked":
        file_path = session_dir / "cloaked.docx"
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        # Try to get original filename for download name, then sanitize it
        # to remove party names that would otherwise leak in the filename.
        try:
            name_file = session_dir / ".original_filename"
            if name_file.is_file():
                original_name = name_file.read_text(encoding="utf-8").strip()
                stem = original_name.rsplit(".", 1)[0]
                # Apply cloak replacements to the filename stem
                replacements_file = session_dir / ".cloak_replacements.json"
                if replacements_file.is_file():
                    cloak_replacements = json.loads(
                        replacements_file.read_text(encoding="utf-8")
                    )
                    stem = sanitize_filename(stem, cloak_replacements)
                download_name = f"{stem}_cloaked.docx"
            else:
                download_name = "cloaked.docx"
        except Exception:
            download_name = "cloaked.docx"
    else:
        file_path = session_dir / "mapping.json"
        media_type = "application/json"
        # Try to get sanitized mapping filename
        try:
            name_file = session_dir / ".original_filename"
            if name_file.is_file():
                original_name = name_file.read_text(encoding="utf-8").strip()
                stem = original_name.rsplit(".", 1)[0]
                replacements_file = session_dir / ".cloak_replacements.json"
                if replacements_file.is_file():
                    cloak_replacements = json.loads(
                        replacements_file.read_text(encoding="utf-8")
                    )
                    stem = sanitize_filename(stem, cloak_replacements)
                download_name = f"{stem}_mapping.json"
            else:
                download_name = "mapping.json"
        except Exception:
            download_name = "mapping.json"

    if not file_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"File not found. Please run the cloaking operation first.",
        )

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=download_name,
    )
