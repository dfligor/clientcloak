/* ============================================================
   ClientCloak  --  Client-Side Utilities
   API calls, drag-and-drop, file validation, toast notifications
   ============================================================ */

// ---- API Helpers ----

/**
 * Upload a .docx file to the server.
 * @param {File} file
 * @returns {Promise<Object>} {session_id, filename, security_findings[], metadata, comments}
 */
async function uploadFile(file) {
    validateDocxFile(file);

    const form = new FormData();
    form.append('file', file);

    const res = await fetch('/api/upload', {
        method: 'POST',
        body: form,
    });

    if (!res.ok) {
        const err = await safeJson(res);
        throw new Error(err.detail || 'Upload failed. Please try again.');
    }
    return res.json();
}

/**
 * Cloak a document.
 * @param {string} sessionId
 * @param {Object} config  -- party_a, party_b, party_a_label, party_b_label, comment_mode
 * @returns {Promise<Object>} {replacements_applied, download_url, mapping_url}
 */
async function cloakDocument(sessionId, config) {
    const form = new FormData();
    form.append('session_id', sessionId);
    form.append('party_a', config.partyA);
    form.append('party_b', config.partyB);
    form.append('party_a_label', config.labelA);
    form.append('party_b_label', config.labelB);
    form.append('comment_mode', config.commentMode);
    form.append('strip_metadata', 'true');
    if (config.aliasesA && config.aliasesA.length > 0) {
        form.append('party_a_aliases', JSON.stringify(config.aliasesA));
    }
    if (config.aliasesB && config.aliasesB.length > 0) {
        form.append('party_b_aliases', JSON.stringify(config.aliasesB));
    }

    const res = await fetch('/api/cloak', {
        method: 'POST',
        body: form,
    });

    if (!res.ok) {
        const err = await safeJson(res);
        throw new Error(err.detail || 'Cloaking failed. Please try again.');
    }
    return res.json();
}

/**
 * Uncloak a document by uploading a redlined doc + mapping file.
 * @param {File} redlinedFile  .docx
 * @param {File} mappingFile   .json
 * @returns {Promise<Object>} {session_id, replacements_restored, download_url}
 */
async function uncloakDocument(redlinedFile, mappingFile) {
    validateDocxFile(redlinedFile);
    validateJsonFile(mappingFile);

    const form = new FormData();
    form.append('redlined_file', redlinedFile);
    form.append('mapping_file', mappingFile);

    const res = await fetch('/api/uncloak', {
        method: 'POST',
        body: form,
    });

    if (!res.ok) {
        const err = await safeJson(res);
        throw new Error(err.detail || 'Uncloaking failed. Please try again.');
    }
    return res.json();
}

/**
 * Trigger a browser file download.
 * In native pywebview mode, uses the JS bridge instead.
 * @param {string} url
 * @param {string} filename
 */
async function downloadFile(url, filename) {
    // Native pywebview mode
    if (window.pywebview && window.pywebview.api) {
        try {
            const result = await window.pywebview.api.download_file(filename, url);
            if (result && result.success) {
                showToast('File saved to Downloads.', 'success');
            } else if (result && result.error !== 'cancelled') {
                showToast('Could not save file: ' + result.error, 'error');
            }
            return;
        } catch (e) {
            // Fall through to browser download
        }
    }

    // Browser download
    try {
        const res = await fetch(url);
        if (!res.ok) throw new Error('Download failed');
        const blob = await res.blob();
        const blobUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = blobUrl;
        a.download = filename;
        a.style.display = 'none';
        document.body.appendChild(a);
        a.click();
        setTimeout(() => {
            URL.revokeObjectURL(blobUrl);
            a.remove();
        }, 200);
    } catch (e) {
        showToast('Download failed. Please try again.', 'error');
    }
}


// ---- File Validation ----

function validateDocxFile(file) {
    if (!file) throw new Error('No file selected.');
    const name = file.name.toLowerCase();
    if (name.endsWith('.doc') && !name.endsWith('.docx')) {
        throw new Error('That looks like an older .doc file. Please save it as .docx first.');
    }
    if (!name.endsWith('.docx')) {
        throw new Error('Hmm, that doesn\'t look like a Word document. Try a .docx file?');
    }
    // 100 MB limit
    if (file.size > 100 * 1024 * 1024) {
        throw new Error('That file is quite large. Try keeping it under 100 MB.');
    }
}

function validateJsonFile(file) {
    if (!file) throw new Error('No file selected.');
    if (!file.name.toLowerCase().endsWith('.json')) {
        throw new Error('Missing the mapping key? It\'s the .json file you downloaded when you cloaked.');
    }
}


// ---- Drag and Drop ----

/**
 * Set up drag-and-drop on a drop zone element.
 * @param {HTMLElement} el       The drop zone element
 * @param {Function}    onDrop   Called with the File when a valid file is dropped
 * @param {string[]}    accept   Array of accepted extensions, e.g. ['.docx']
 */
function initDropZone(el, onDrop, accept) {
    if (!el) return;

    const preventDefaults = (e) => {
        e.preventDefault();
        e.stopPropagation();
    };

    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(evt => {
        el.addEventListener(evt, preventDefaults, false);
    });

    let dragCounter = 0;

    el.addEventListener('dragenter', () => {
        dragCounter++;
        el.classList.add('drag-over');
    });

    el.addEventListener('dragleave', () => {
        dragCounter--;
        if (dragCounter <= 0) {
            dragCounter = 0;
            el.classList.remove('drag-over');
        }
    });

    el.addEventListener('drop', (e) => {
        dragCounter = 0;
        el.classList.remove('drag-over');

        const files = e.dataTransfer.files;
        if (!files || files.length === 0) return;

        const file = files[0];
        const ext = '.' + file.name.split('.').pop().toLowerCase();

        if (accept && accept.length > 0 && !accept.includes(ext)) {
            const expected = accept.join(', ');
            showToast('Expected a ' + expected + ' file. Got "' + file.name + '" instead.', 'error');
            return;
        }

        onDrop(file);
    });
}


// ---- Toast Notifications ----

let toastContainer = null;

function getToastContainer() {
    if (!toastContainer) {
        toastContainer = document.createElement('div');
        toastContainer.className = 'toast-container';
        toastContainer.setAttribute('aria-live', 'polite');
        toastContainer.setAttribute('role', 'status');
        document.body.appendChild(toastContainer);
    }
    return toastContainer;
}

/**
 * Show a toast notification.
 * @param {string} message
 * @param {'error'|'success'|'info'} type
 * @param {number} duration  ms to auto-dismiss (default 5000, 0 = manual)
 */
function showToast(message, type, duration) {
    if (typeof type === 'undefined') type = 'info';
    if (typeof duration === 'undefined') duration = 5000;

    const container = getToastContainer();

    const iconMap = {
        error:   '<svg class="w-4 h-4 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
        success: '<svg class="w-4 h-4 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
        info:    '<svg class="w-4 h-4 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
    };

    const toast = document.createElement('div');
    toast.className = 'toast toast-' + type;
    toast.innerHTML = (iconMap[type] || '') + '<span>' + escapeHtml(message) + '</span>';
    container.appendChild(toast);

    const dismiss = () => {
        toast.classList.add('toast-exit');
        setTimeout(() => {
            if (toast.parentNode) toast.parentNode.removeChild(toast);
        }, 260);
    };

    toast.addEventListener('click', dismiss);

    if (duration > 0) {
        setTimeout(dismiss, duration);
    }

    return dismiss;
}


// ---- Utilities ----

/**
 * Safely parse JSON from a fetch response.
 * Returns {} if parsing fails.
 */
async function safeJson(res) {
    try {
        return await res.json();
    } catch {
        return {};
    }
}

/**
 * Escape HTML special characters to prevent XSS.
 */
function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

/**
 * Format a file size in human-readable form.
 */
function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}
