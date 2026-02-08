"""
Metadata inspection and removal for .docx files.

.docx files are ZIP archives containing XML. python-docx exposes core_properties
but does not surface everything (Company, Manager, custom properties). We use
both python-docx for convenient access AND direct XML parsing of the underlying
ZIP entries for complete metadata inspection and removal.

Key ZIP entries for metadata:
    docProps/core.xml     - Dublin Core: author, dates, revision
    docProps/app.xml      - Extended: company, manager, application, template
    docProps/custom.xml   - Arbitrary user-defined properties
    word/comments.xml     - Comment count (for reporting only)
"""

import zipfile
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

from docx import Document

from .models import MetadataReport

# ---------------------------------------------------------------------------
# XML namespace maps
# ---------------------------------------------------------------------------

# Dublin Core / core properties (docProps/core.xml)
_NS_CORE = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "dcmitype": "http://purl.org/dc/dcmitype/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}

# Extended properties (docProps/app.xml)
_NS_APP = {
    "ep": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
    "vt": "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes",
}

# Custom properties (docProps/custom.xml)
_NS_CUSTOM = {
    "cust": "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties",
    "vt": "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes",
}

# Word processing main namespace (word/comments.xml)
_NS_W = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
}

# Generic date used when neutralizing timestamps.
_GENERIC_DATE = "2020-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Register namespace prefixes so ET.tostring preserves them
# ---------------------------------------------------------------------------
for _prefix, _uri in {
    **_NS_CORE,
    **_NS_APP,
    **_NS_CUSTOM,
    **_NS_W,
}.items():
    ET.register_namespace(_prefix, _uri)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def inspect_metadata(doc_path: Path) -> MetadataReport:
    """
    Inspect a .docx file and return a report of all metadata found.

    Reads core properties via python-docx for convenience, then directly
    parses docProps/app.xml and docProps/custom.xml from the ZIP archive
    to capture fields that python-docx does not expose (Company, Manager,
    Application, Template, and all custom properties).

    Comments are counted by parsing word/comments.xml.

    The file is **not** modified.

    Args:
        doc_path: Path to the .docx file to inspect.

    Returns:
        A MetadataReport populated with every metadata field found.

    Raises:
        FileNotFoundError: If doc_path does not exist.
        zipfile.BadZipFile: If the file is not a valid ZIP / .docx.
    """
    doc_path = Path(doc_path)

    # --- python-docx for core properties ---
    doc = Document(str(doc_path))
    props = doc.core_properties

    report = MetadataReport(
        author=props.author or None,
        last_modified_by=props.last_modified_by or None,
        created=props.created.isoformat() if props.created else None,
        modified=props.modified.isoformat() if props.modified else None,
        last_printed=props.last_printed.isoformat() if props.last_printed else None,
        revision=str(props.revision) if props.revision else None,
    )

    # --- Direct ZIP access for extended & custom properties ---
    with zipfile.ZipFile(doc_path, "r") as zf:
        # Extended properties (app.xml)
        if "docProps/app.xml" in zf.namelist():
            app_xml = zf.read("docProps/app.xml")
            report = _extract_app_properties(app_xml, report)

        # Custom properties
        if "docProps/custom.xml" in zf.namelist():
            custom_xml = zf.read("docProps/custom.xml")
            report = _extract_custom_properties(custom_xml, report)

        # Comment count
        if "word/comments.xml" in zf.namelist():
            comments_xml = zf.read("word/comments.xml")
            report.comments_count = _count_comments(comments_xml)

    return report


def strip_metadata(
    input_path: Path,
    output_path: Path,
    preserve_comments: bool = False,
) -> MetadataReport:
    """
    Remove all metadata from a .docx file and write a clean copy.

    Opens the .docx as a ZIP archive and copies every entry to a new ZIP,
    modifying metadata-bearing XML files in transit:

    - **docProps/core.xml**: author, lastModifiedBy blanked; created/modified
      set to a generic date; revision reset to "1".
    - **docProps/app.xml**: Company, Manager blanked; Application set to
      "Microsoft Office Word"; Template set to "Normal.dotm".
    - **docProps/custom.xml**: Skipped entirely (not copied to output).
    - **word/comments.xml**: Cleared unless *preserve_comments* is True.

    Args:
        input_path: Path to the original .docx file.
        output_path: Path where the cleaned .docx will be written. May be
            the same as *input_path* (the file is read fully into memory
            first).
        preserve_comments: If True, comments are left intact. If False,
            all comment elements are removed.

    Returns:
        A MetadataReport describing the metadata that **was present**
        before stripping (i.e. the "before" snapshot).

    Raises:
        FileNotFoundError: If input_path does not exist.
        zipfile.BadZipFile: If the file is not a valid ZIP / .docx.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    # Capture a "before" report so callers know what was removed.
    before_report = inspect_metadata(input_path)

    # Read entire input into memory so input_path == output_path is safe.
    input_bytes = input_path.read_bytes()

    buf = BytesIO()
    with zipfile.ZipFile(BytesIO(input_bytes), "r") as zin, \
         zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:

        for item in zin.infolist():
            raw = zin.read(item.filename)

            # Skip custom properties entirely
            if item.filename == "docProps/custom.xml":
                continue

            if item.filename == "docProps/core.xml":
                raw = _clean_core_properties(raw)
            elif item.filename == "docProps/app.xml":
                raw = _clean_app_properties(raw)
            elif item.filename == "word/comments.xml" and not preserve_comments:
                raw = _clean_comments(raw)

            zout.writestr(item, raw)

    output_path.write_bytes(buf.getvalue())
    return before_report


# ---------------------------------------------------------------------------
# Internal XML cleaners
# ---------------------------------------------------------------------------

def _clean_core_properties(xml_data: bytes) -> bytes:
    """
    Parse docProps/core.xml and neutralize all identifying metadata.

    Sets:
        dc:creator         -> ""
        cp:lastModifiedBy  -> ""
        dcterms:created    -> generic date
        dcterms:modified   -> generic date
        cp:revision        -> "1"
        dc:description     -> ""  (sometimes contains user text)
        cp:lastPrinted     -> removed

    Args:
        xml_data: Raw bytes of the core.xml entry.

    Returns:
        Cleaned XML as bytes, preserving the original namespace declarations.
    """
    root = ET.fromstring(xml_data)

    _set_element_text(root, "dc:creator", "", _NS_CORE)
    _set_element_text(root, "cp:lastModifiedBy", "", _NS_CORE)
    _set_element_text(root, "dc:description", "", _NS_CORE)
    _set_element_text(root, "cp:revision", "1", _NS_CORE)

    # Timestamps: set to generic date, preserve xsi:type attribute
    for tag in ("dcterms:created", "dcterms:modified"):
        _set_element_text(root, tag, _GENERIC_DATE, _NS_CORE)

    # Remove lastPrinted entirely
    _remove_element(root, "cp:lastPrinted", _NS_CORE)

    return _to_xml_bytes(root)


def _clean_app_properties(xml_data: bytes) -> bytes:
    """
    Parse docProps/app.xml and neutralize identifying fields.

    Sets:
        Company     -> ""
        Manager     -> ""
        Application -> "Microsoft Office Word"
        Template    -> "Normal.dotm"

    Args:
        xml_data: Raw bytes of the app.xml entry.

    Returns:
        Cleaned XML as bytes.
    """
    root = ET.fromstring(xml_data)

    _set_element_text(root, "ep:Company", "", _NS_APP)
    _set_element_text(root, "ep:Manager", "", _NS_APP)
    _set_element_text(root, "ep:Application", "Microsoft Office Word", _NS_APP)
    _set_element_text(root, "ep:Template", "Normal.dotm", _NS_APP)

    return _to_xml_bytes(root)


def _clean_comments(xml_data: bytes) -> bytes:
    """
    Remove all ``<w:comment>`` elements from word/comments.xml.

    Preserves the root ``<w:comments>`` wrapper so Word can still open
    the file without repair prompts.

    Args:
        xml_data: Raw bytes of the comments.xml entry.

    Returns:
        Cleaned XML with all comment elements removed.
    """
    root = ET.fromstring(xml_data)
    ns_w = _NS_W["w"]

    for comment in root.findall(f"{{{ns_w}}}comment"):
        root.remove(comment)

    return _to_xml_bytes(root)


# ---------------------------------------------------------------------------
# Internal helpers — app.xml / custom.xml extraction
# ---------------------------------------------------------------------------

def _extract_app_properties(xml_data: bytes, report: MetadataReport) -> MetadataReport:
    """
    Extract extended property values from docProps/app.xml into an existing report.

    Args:
        xml_data: Raw bytes of app.xml.
        report: The MetadataReport to populate. Modified in place and returned.

    Returns:
        The same MetadataReport instance, updated with app property values.
    """
    root = ET.fromstring(xml_data)
    ns_ep = _NS_APP["ep"]

    report.company = _get_text(root, f"{{{ns_ep}}}Company")
    report.manager = _get_text(root, f"{{{ns_ep}}}Manager")
    report.application = _get_text(root, f"{{{ns_ep}}}Application")
    report.app_version = _get_text(root, f"{{{ns_ep}}}AppVersion")
    report.template = _get_text(root, f"{{{ns_ep}}}Template")

    return report


def _extract_custom_properties(xml_data: bytes, report: MetadataReport) -> MetadataReport:
    """
    Extract all custom properties from docProps/custom.xml into the report.

    Custom properties are arbitrary key/value pairs. The XML structure is:

    .. code-block:: xml

        <Properties>
            <property fmtid="..." pid="..." name="MyProp">
                <vt:lpwstr>value</vt:lpwstr>
            </property>
        </Properties>

    Args:
        xml_data: Raw bytes of custom.xml.
        report: The MetadataReport to populate. Modified in place and returned.

    Returns:
        The same MetadataReport instance, updated with custom property values.
    """
    root = ET.fromstring(xml_data)
    ns_cust = _NS_CUSTOM["cust"]
    ns_vt = _NS_CUSTOM["vt"]

    custom_props: dict[str, str] = {}

    for prop in root.findall(f"{{{ns_cust}}}property"):
        name = prop.get("name", "")
        # The value child can be any vt:* type; try common ones.
        value = ""
        for child in prop:
            if child.text:
                value = child.text
                break
        if name:
            custom_props[name] = value

    report.custom_properties = custom_props
    return report


def _count_comments(xml_data: bytes) -> int:
    """
    Count ``<w:comment>`` elements in word/comments.xml.

    Args:
        xml_data: Raw bytes of comments.xml.

    Returns:
        The number of comment elements found.
    """
    root = ET.fromstring(xml_data)
    ns_w = _NS_W["w"]
    return len(root.findall(f"{{{ns_w}}}comment"))


# ---------------------------------------------------------------------------
# XML utility helpers
# ---------------------------------------------------------------------------

def _get_text(root: ET.Element, tag: str) -> str | None:
    """
    Find a direct child element by tag and return its text, or None.

    Args:
        root: The parent element to search within.
        tag: Fully-qualified tag name (e.g. ``{uri}LocalName``).

    Returns:
        The element's text content, or None if the element is absent or
        has no text.
    """
    el = root.find(tag)
    if el is not None and el.text:
        return el.text
    return None


def _set_element_text(
    root: ET.Element,
    prefixed_tag: str,
    value: str,
    ns_map: dict[str, str],
) -> None:
    """
    Set the text of a child element, creating it if absent.

    The *prefixed_tag* uses the short prefix form (e.g. ``"dc:creator"``).
    It is expanded using *ns_map* before lookup.

    Args:
        root: Parent element.
        prefixed_tag: Namespace-prefixed tag such as ``"dc:creator"``.
        value: The text value to assign.
        ns_map: Prefix -> URI mapping for namespace expansion.
    """
    prefix, local = prefixed_tag.split(":", 1)
    uri = ns_map[prefix]
    full_tag = f"{{{uri}}}{local}"

    el = root.find(full_tag)
    if el is not None:
        el.text = value
    else:
        new_el = ET.SubElement(root, full_tag)
        new_el.text = value


def _remove_element(
    root: ET.Element,
    prefixed_tag: str,
    ns_map: dict[str, str],
) -> None:
    """
    Remove a direct child element if it exists.

    Args:
        root: Parent element.
        prefixed_tag: Namespace-prefixed tag such as ``"cp:lastPrinted"``.
        ns_map: Prefix -> URI mapping for namespace expansion.
    """
    prefix, local = prefixed_tag.split(":", 1)
    uri = ns_map[prefix]
    full_tag = f"{{{uri}}}{local}"

    el = root.find(full_tag)
    if el is not None:
        root.remove(el)


def _to_xml_bytes(root: ET.Element) -> bytes:
    """
    Serialize an ElementTree root to bytes with an XML declaration.

    Args:
        root: The root element to serialize.

    Returns:
        UTF-8 encoded XML bytes including the ``<?xml ...?>`` declaration.
    """
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)
