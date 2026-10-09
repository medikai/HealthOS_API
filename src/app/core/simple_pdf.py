"""Minimal, dependency-free PDF writer for patient-facing generated documents.

This renders plain text only (no fabricated signatures, logos or clinical
interpretation). It exists because no PDF tooling is currently installed; it is
a real, valid PDF byte stream, not the old metadata stub. Documents state that
clinical signing metadata is recorded in MedikAI and is not a cryptographic
signature.
"""

from __future__ import annotations

from datetime import UTC, datetime

_PAGE_WIDTH = 595
_PAGE_HEIGHT = 842
_MARGIN_X = 56
_TOP_Y = 800
_LINE_HEIGHT = 14
_MAX_LINES_PER_PAGE = 52


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("\r", "")
    )


def _wrap(text: str, width: int = 92) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word[:width]
    if current:
        lines.append(current)
    return lines


def render_text_pdf(
    *,
    title: str,
    subtitle: str | None,
    sections: list[tuple[str, list[str]]],
    footer_note: str,
) -> bytes:
    """Render one text document. Returns valid PDF bytes."""
    lines: list[tuple[int, str]] = []  # (font_size, text)
    lines.append((16, title))
    if subtitle:
        lines.append((10, subtitle))
    lines.append((9, f"Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}"))
    lines.append((9, ""))
    for heading, body_lines in sections:
        lines.append((12, heading))
        for entry in body_lines:
            for wrapped in _wrap(entry):
                lines.append((10, wrapped))
        lines.append((10, ""))

    pages: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    for item in lines:
        if len(current) >= _MAX_LINES_PER_PAGE:
            pages.append(current)
            current = []
        current.append(item)
    current.append((8, footer_note))
    pages.append(current)

    objects: list[bytes] = []
    page_object_ids: list[int] = []
    content_object_ids: list[int] = []
    font_object_id = 3 + 2 * len(pages)

    for page_index, page_lines in enumerate(pages):
        page_object_ids.append(3 + 2 * page_index)
        content_object_ids.append(4 + 2 * page_index)
        stream_lines = ["BT", "/F1 9 Tf", f"1 0 0 1 {_MARGIN_X} {_TOP_Y} Tm", f"{_LINE_HEIGHT} TL"]
        for size, text in page_lines:
            stream_lines.append(f"/F1 {size} Tf")
            stream_lines.append(f"({_escape(text)}) Tj")
            stream_lines.append("T*")
        stream_lines.append("ET")
        stream = "\n".join(stream_lines).encode("latin-1", "replace")
        content = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream"
        )
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_PAGE_WIDTH} {_PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 {font_object_id} 0 R >> >> "
            f"/Contents {content_object_ids[page_index]} 0 R >>".encode("ascii")
        )
        objects.append(content)

    # Object numbering: 1 catalog, 2 pages, then per-page page+content, then font.
    ordered: dict[int, bytes] = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}
    kids = " ".join(f"{object_id} 0 R" for object_id in page_object_ids)
    ordered[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii")
    for offset, page_object_id in enumerate(page_object_ids):
        ordered[page_object_id] = objects[2 * offset]
        ordered[content_object_ids[offset]] = objects[2 * offset + 1]
    ordered[font_object_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    output = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for object_id in sorted(ordered):
        offsets[object_id] = len(output)
        output += f"{object_id} 0 obj\n".encode("ascii")
        output += ordered[object_id]
        output += b"\nendobj\n"

    xref_offset = len(output)
    max_id = max(ordered)
    output += f"xref\n0 {max_id + 1}\n".encode("ascii")
    output += b"0000000000 65535 f \n"
    for object_id in range(1, max_id + 1):
        offset = offsets.get(object_id, 0)
        output += f"{offset:010d} 00000 n \n".encode("ascii")
    output += (
        f"trailer\n<< /Size {max_id + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    return bytes(output)
