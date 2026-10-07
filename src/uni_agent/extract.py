"""File -> plain text (pdf/pptx/docx/txt/md), file hashing, and section-aware chunking."""
from __future__ import annotations

import hashlib
from pathlib import Path

SUPPORTED = {".pdf", ".pptx", ".docx", ".txt", ".md"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n\n".join(f"## Page {i}\n{(p.extract_text() or '').strip()}" for i, p in enumerate(reader.pages, 1))


def _pptx(path: Path) -> str:
    from pptx import Presentation

    parts = []
    for i, slide in enumerate(Presentation(str(path)).slides, 1):
        lines = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                lines += [p.text for p in shape.text_frame.paragraphs if p.text.strip()]
            elif getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    lines.append(" | ".join(c.text.strip() for c in row.cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append(f"[Speaker notes] {notes}")
        parts.append(f"## Slide {i}\n" + "\n".join(lines))
    return "\n\n".join(parts)


def _docx(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    lines = []
    for p in doc.paragraphs:
        if not p.text.strip():
            continue
        style = (p.style.name or "").lower() if p.style is not None else ""
        lines.append(f"## {p.text}" if style.startswith("heading") or style == "title" else p.text)
    for table in doc.tables:
        for row in table.rows:
            lines.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(lines)


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _pdf(path)
    if ext == ".pptx":
        return _pptx(path)
    if ext == ".docx":
        return _docx(path)
    if ext in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="replace")
    raise ValueError(f"Unsupported file type: {ext}")


def has_real_text(text: str, min_chars: int = 200) -> bool:
    """False for scanned PDFs / image-only slides where extraction yields only headers."""
    body = "\n".join(l for l in text.splitlines() if not l.startswith("## "))
    return len(body.strip()) >= min_chars


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Split on '## ' section headers (slides/pages/headings), packing sections up to max_chars.
    A single oversized section is split on line boundaries."""
    sections: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("## ") and current:
            sections.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        sections.append("\n".join(current))

    pieces: list[str] = []
    for sec in sections:
        while len(sec) > max_chars:
            cut = sec.rfind("\n", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            pieces.append(sec[:cut])
            sec = sec[cut:].lstrip("\n")
        pieces.append(sec)

    chunks: list[str] = []
    buf = ""
    for piece in pieces:
        if buf and len(buf) + len(piece) + 2 > max_chars:
            chunks.append(buf)
            buf = piece
        else:
            buf = f"{buf}\n\n{piece}" if buf else piece
    if buf.strip():
        chunks.append(buf)
    return chunks
