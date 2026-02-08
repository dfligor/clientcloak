# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

**ClientCloak** — Bidirectional document sanitization tool for attorneys. Replaces real party names with bracketed labels like `[Customer]` and `[Vendor]` before sharing documents. A mapping key file allows restoring the original names later.

**Key Principle:** All processing happens locally. No client data leaves the user's machine.

## Business Model

- **Engine + CLI + Web UI:** Open source (MIT)
- **Native Mac App:** $49 one-time purchase (native window, bundled GLiNER model, curated prompts, video walkthroughs)
- **Prompts/Walkthroughs:** Proprietary (not in this repo)

## Git Commit Guidelines

- **NEVER include AI attribution or "Generated with Claude Code" tags**
- **NEVER include "Co-Authored-By" lines referencing any AI**
- Use clear, concise commit messages
- Follow conventional commit format

## Open Source Rules

- This is a public MIT-licensed repository — all code is visible
- Never commit mapping files (contain real client names) — .gitignore handles this
- Never commit test documents with real client data (use `real_` prefix convention)
- Commercial-only assets (native app, prompts, walkthroughs) do NOT go in this repo

## Project Structure

```
src/clientcloak/
├── __init__.py          # Public API exports, __version__
├── __main__.py          # python -m clientcloak support
├── models.py            # All Pydantic models
├── paths.py             # Cross-platform paths
├── docx_handler.py      # Format-preserving cross-run text replacement
├── cloaker.py           # 8-step cloaking pipeline
├── uncloaker.py         # Restore originals from mapping
├── mapping.py           # Create/save/load JSON mapping files
├── security.py          # Prompt injection + hidden text detection
├── metadata.py          # ZIP-level metadata inspection and stripping
├── comments.py          # Strip / anonymize / sanitize comment modes
├── sessions.py          # File-backed sessions with 24h TTL
├── cli.py               # 4 subcommands: cloak, uncloak, scan, inspect
└── ui/
    ├── app.py            # FastAPI application factory
    ├── desktop.py        # Native macOS pywebview launcher (commercial)
    ├── routes/
    │   ├── cloak.py      # Upload, cloak, download endpoints
    │   └── uncloak.py    # Uncloak and download endpoints
    ├── templates/
    │   ├── base.html
    │   └── index.html
    └── static/
        ├── style.css
        └── app.js
```

## Tech Stack

- **Python 3.10+**
- **python-docx + lxml** — Document processing and XML manipulation
- **Pydantic v2** — Data models and JSON serialization
- **FastAPI + Uvicorn** — Local web server
- **Alpine.js + Tailwind CSS** — Frontend (CDN, no build step)
- **GLiNER** — Zero-shot NER for PII detection (optional, Phase 2)

## Key Commands

```bash
# Activate environment
source venv/bin/activate

# Run web UI
clientcloak-ui

# Run CLI
clientcloak cloak input.docx --party-a "Acme Corp" --party-b "BigCo LLC" --labels customer/vendor

# Run tests
python -m pytest tests/ -v

# Launch shortcut (defined in ~/.zshrc)
cloak
```

## Testing

- 130 tests across 8 test files
- Run in ~3.5 seconds
- Full round-trip coverage (cloak -> uncloak)
- All tests must pass before committing

## Security Practices

- No network calls — all processing is local
- No telemetry — the tool does not phone home
- Validate all file inputs (.docx format check)
- Never expose document content in error messages
- Scan for prompt injection and hidden text before cloaking

## License

MIT (full engine, CLI, and web UI)
