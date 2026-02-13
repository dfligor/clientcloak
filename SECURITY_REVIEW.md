# ClientCloak Security Review

**Date:** 2026-02-13
**Reviewer:** Automated security analysis (Claude)
**Scope:** Full codebase review of `clientcloak` repository
**Commit:** HEAD of master branch

---

## Executive Summary

ClientCloak is a well-architected, security-conscious application. The
local-only design eliminates entire categories of vulnerabilities (no
database means no SQL injection, no outbound requests means no SSRF, no
authentication system means no credential management). Previous security
improvements — defusedxml, session path-traversal checks, HTML escaping,
security headers, CORS restrictions — are correctly implemented.

This review identifies **4 high-priority**, **6 medium-priority**, and
**4 low-priority** findings. None are remotely exploitable under the
default localhost configuration, but several weaken defense-in-depth and
should be addressed before any network-exposed or commercial deployment.

---

## HIGH Priority

### H1. CSP `unsafe-eval` weakens XSS defense

**File:** `src/clientcloak/ui/app.py:59`

The Content-Security-Policy includes `'unsafe-eval'` in `script-src`.
This is required because Tailwind CSS is loaded from its CDN JIT
compiler (`base.html:12`), which calls `eval()` internally. With
`'unsafe-eval'` present, any XSS vector — even an indirect one through a
dependency — can escalate to arbitrary JavaScript execution.

**Recommendation:** Replace the Tailwind CDN JIT compiler with a
pre-built Tailwind CSS file (either self-hosted or built at package
time). This eliminates the need for both `'unsafe-eval'` and the CDN
dependency. Alpine.js does not require `'unsafe-eval'`.


### H2. Tailwind CSS CDN not version-pinned, no SRI hashes

**File:** `src/clientcloak/ui/templates/base.html:12`

```html
<script src="https://cdn.tailwindcss.com"></script>
```

Alpine.js is correctly pinned to an exact version (`3.14.8`), but
Tailwind is loaded without any version pin. A CDN compromise or upstream
change would execute arbitrary code in the application. Additionally,
neither CDN resource uses Subresource Integrity (SRI) hashes.

**Recommendation:** Either self-host both libraries (preferred — removes
all CDN trust) or at minimum pin to an exact version and add SRI
hashes. The comment on `base.html:88` already notes the supply-chain
concern; extending that practice to Tailwind would complete the
mitigation.


### H3. Mapping file not covered by `.gitignore`

**File:** `.gitignore:37-38`

The gitignore rules are:
```
*_mapping.json
mapping_*.json
```

However, the web UI names the mapping download
`{stem}_Secret_Decoder_Ring.json`. This pattern is **not** matched by
either rule. If a user saves the decoder key to their project directory
(a natural workflow), it could be committed to version control, exposing
all original client names, emails, SSNs, and other PII.

**Recommendation:** Add `*Secret_Decoder_Ring*` to `.gitignore`. Also
consider adding `*_decoder_key*` and `*.decoder.json` as defensive
patterns.


### H4. `get_session_file` allows `..` as a filename

**File:** `src/clientcloak/sessions.py:103`

The filename regex `^[a-zA-Z0-9._-]+$` permits the string `..` (two
consecutive dots), which is a parent-directory reference. While all
current callers pass hardcoded filenames (so this is not exploitable
today), the function lacks the containment check that `get_session_dir`
has. If a future code change passes user input to this function, it
would enable path traversal.

**Recommendation:** Add either:
- A check that `..` is not in the filename: `if '..' in filename:`
- A resolved-path containment check matching the one in `get_session_dir`
- A stricter regex that disallows consecutive dots: `^[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?$`

---

## MEDIUM Priority

### M1. Partial upload not cleaned on size-limit rejection

**Files:** `src/clientcloak/ui/routes/cloak.py:62-78`,
`src/clientcloak/ui/routes/uncloak.py:71-103`

When a streamed upload exceeds the 100 MB limit, an `HTTPException` is
raised, but the partially written file and its session directory remain
on disk until the 24-hour TTL cleanup. Under sustained abuse, an
attacker could fill disk by repeatedly uploading files that exceed the
limit.

**Recommendation:** Wrap the upload in a try/except that removes the
session directory on failure, or use a temporary file that is only moved
into the session directory on success.


### M2. Session ID has only 32 bits of entropy

**File:** `src/clientcloak/sessions.py:43`

`uuid.uuid4().hex[:8]` produces 8 hex characters = 32 bits of
randomness (~4.3 billion possible values). If the server is exposed to a
network (despite the warning at `app.py:150`), an attacker could
brute-force valid session IDs to access uploaded documents containing
sensitive legal material.

**Recommendation:** Increase to 16 hex characters (64 bits) or use the
full UUID. The session ID appears in URLs and logs, so a balance between
length and usability is reasonable, but 32 bits is below the security
floor for a resource that guards confidential documents.


### M3. CORS allows any localhost port

**File:** `src/clientcloak/ui/app.py:105`

The CORS regex `r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$"` permits
cross-origin requests from **any** port on localhost. A malicious web
application running on a different localhost port (e.g., a compromised
dev server on port 3000) could make authenticated cross-origin requests
to the ClientCloak API.

**Recommendation:** If the pywebview desktop app connects from the same
port, CORS can be restricted to same-origin only. Otherwise, consider
using a CSRF token mechanism in addition to CORS, or restrict the CORS
allowed ports to the configured server port.


### M4. No CSRF protection on state-changing endpoints

**Files:** `src/clientcloak/ui/routes/cloak.py`,
`src/clientcloak/ui/routes/uncloak.py`

POST endpoints (`/api/upload`, `/api/cloak`, `/api/uncloak`) accept form
data without a CSRF token. Combined with M3 (broad CORS), this means any
script running on localhost can trigger document uploads, cloaking, and
uncloaking operations.

**Recommendation:** Add a CSRF token mechanism (e.g., double-submit
cookie pattern or synchronizer token) to state-changing endpoints.


### M5. Document preview iframe allows script execution

**File:** `src/clientcloak/ui/templates/index.html:228`

```html
<iframe ... sandbox="allow-same-origin allow-scripts" ...>
```

The preview iframe uses `iframeDoc.write()` (line 1142) with
`tempContainer.innerHTML` from the docx-preview library. The sandbox
allows both same-origin access and script execution. If docx-preview has
an XSS vulnerability in its rendering pipeline, it would execute in the
context of the main page.

**Recommendation:** Remove `allow-scripts` from the sandbox attribute.
The document preview is a read-only rendering; scripts should not be
needed. If docx-preview requires scripts for rendering, consider
rendering to a canvas or using `srcdoc` with a strict CSP instead.


### M6. No zip bomb / decompression bomb protection

**File:** `src/clientcloak/docx_handler.py:60-117`

The upload endpoint limits file size to 100 MB, but there is no check on
the compression ratio. A specially crafted .docx (ZIP archive) could have
a very high compression ratio, decompressing to many gigabytes when
python-docx loads it into memory.

**Recommendation:** After the ZIP validity check, read the ZIP's
`infolist()` and sum the uncompressed sizes. Reject if the total exceeds
a reasonable threshold (e.g., 500 MB or 10x the compressed size).

---

## LOW Priority

### L1. No rate limiting

**File:** `src/clientcloak/ui/app.py`

The API has no rate limiting middleware. Under the localhost-only design
this is acceptable, but if the server is exposed to a network, endpoints
like `/api/upload` (which creates sessions and processes documents) could
be used for denial-of-service.

**Recommendation:** Consider adding `slowapi` or a similar rate-limiting
middleware for production/network deployments.


### L2. No `Strict-Transport-Security` header

**File:** `src/clientcloak/ui/app.py:48-66`

The security headers middleware sets `X-Content-Type-Options`,
`X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, and
`Content-Security-Policy`, but not `Strict-Transport-Security`. This is
a non-issue for localhost HTTP but would matter if deployed over HTTPS.

**Recommendation:** Add HSTS header conditionally when the server is
configured for HTTPS.


### L3. Uncloaked filename may contain special characters

**File:** `src/clientcloak/ui/routes/uncloak.py:140-144`

The uncloaked filename is computed by applying mapping replacements to
the uploaded filename stem. If mapping values contain characters that are
problematic in filenames (e.g., `/`, `\`, newlines), they propagate to
the `Content-Disposition` header. FastAPI's `FileResponse` should handle
encoding, but explicit sanitization would be safer.

**Recommendation:** Sanitize the computed filename by removing or
replacing characters that are invalid in filenames across platforms.


### L4. `launch.sh` port parameter not validated

**File:** `scripts/launch.sh:8`

The `PORT` variable comes from `$1` and is used in `lsof -ti :"$PORT"`.
While `set -e` and the typical usage pattern limit exploitation, a
non-numeric port value is passed directly to shell commands.

**Recommendation:** Validate that `$PORT` is a numeric value:
```bash
if ! [[ "$PORT" =~ ^[0-9]+$ ]]; then echo "Invalid port"; exit 1; fi
```

---

## Items Verified as Secure

| Category | Status | Notes |
|---|---|---|
| SQL Injection | N/A | No database |
| Command Injection | N/A | No shell/subprocess calls |
| XXE | Secure | `defusedxml` used consistently in `comments.py`, `metadata.py` |
| Path Traversal (sessions) | Secure | Regex + `.resolve()` containment check in `get_session_dir` |
| XSS (output escaping) | Secure | `escapeHtml()` in `app.js:316`, `x-text` used throughout Alpine templates |
| XSS (server-side) | Secure | Jinja2 auto-escaping, no `|safe` filters |
| Input Validation | Strong | Pydantic models with `Field(ge=, le=, max_length=)` constraints |
| File Upload Validation | Good | Extension check, size limit, streaming to disk |
| Session Isolation | Good | Per-session directories, 24-hour TTL, fail-secure cleanup |
| CORS | Good | Restricted to localhost origins (see M3 for nuance) |
| Security Headers | Good | `nosniff`, `DENY`, `Referrer-Policy`, `Permissions-Policy`, CSP |
| Hardcoded Secrets | None | No API keys, passwords, or tokens in source |
| Sensitive Data Logging | Safe | Error messages are generic, no PII in logs |
| SSRF | N/A | No outbound HTTP requests |
| Dependency Security | Good | All dependencies are well-maintained, current versions |
| Prompt Injection Detection | Excellent | 30+ patterns covering instruction override, role manipulation, jailbreaks, boundary attacks |
| Hidden Text Detection | Excellent | Tiny fonts, hidden attribute, near-white color, invisible Unicode chars |
| XML Attribute Escaping | Secure | `_escape_xml_attr()` in `comments.py:42` properly escapes `&`, `<`, `>`, `"`, `'` |

---

## Comparison with Previous Review

Since the previous security review, the following improvements were
observed:

- defusedxml is consistently used for all XML parsing
- Session path-traversal protection with containment checks
- Security headers middleware with CSP, X-Frame-Options, etc.
- CORS restricted to localhost origins
- HTML escaping via `escapeHtml()` for toast messages
- Alpine.js version pinned to prevent supply-chain attacks
- Self-hosted jszip and docx-preview libraries (no CDN dependency)
- XML attribute escaping for comment author names
- File size limits enforced server-side with streaming uploads
- Structured logging without PII leakage
