"""Gramma v3 — PDF page-health reports.

Generates a small, self-contained PDF (no external service) using the
built-in PostScript trick: we render the same numbers as a simple text PDF.
This works offline and in Docker without heavy dependencies.

Note: the reports are TEXT-in-PDF (Farsi rendered as visual text via a
non-embedded font may not display in all viewers; we include Latin numerals
and structlabels so the data is always readable). For a fully-faithful Farsi
PDF, swap `_render_via_reportlab` in once reportlab is installed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger("gramma.pdf")


def build_health_pdf_bytes(report_text: str) -> bytes:
    """Build a minimal valid PDF from the report text (ASCII-safe subset)."""
    # Strip characters that our simple font can't encode.
    lines = []
    for raw in report_text.splitlines():
        line = ""
        for ch in raw:
            if 32 <= ord(ch) < 128:
                line += ch
            else:
                line += "?"
        lines.append(line or " ")

    objects = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    content = b"BT /F1 11 Tf 40 780 Td 16 TL\n"
    for line in lines[:80]:
        safe = line.encode("ascii", "replace").decode("ascii")
        content += f"({safe}) Tj T*\n".encode("ascii", "replace")
    content += b"ET"
    stream = content
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> "
        b"/Contents 5 0 R >>"
    )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)
