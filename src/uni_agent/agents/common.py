"""Shared summarise flow for the lecture and lab agents: one request, or chunk -> notes -> combine."""
from __future__ import annotations

from ..extract import chunk_text
from ..llm import LLM

CHUNK_PROMPT = """This is part {i} of {n} of a {doc_kind}. Write compact study notes for THIS part only:
the main points, definitions, steps, examples and any formulas, commands or diagrams described.
Plain text, bullets with "•".

{chunk}"""

OUTPUT_RULE = ("Output ONLY the structure below, starting with its first line. "
               "Do not repeat these instructions or the context section.")


def summarize_document(llm: LLM, system: str, fmt: str, context: str, text: str, doc_kind: str,
                       max_single_chars: int) -> str:
    """`fmt` is the required output structure; `context` is background (earlier docs, deadlines)."""
    prefix = f"CONTEXT (for your reference only):\n{context}\n\n{OUTPUT_RULE}\n\n{fmt}"
    if len(text) <= max_single_chars:
        return llm.complete(system, f"{prefix}\n\n{doc_kind.upper()} MATERIAL:\n{text}", max_tokens=8192).strip()

    chunks = chunk_text(text, max_single_chars)
    notes = [
        llm.complete(system, CHUNK_PROMPT.format(i=i, n=len(chunks), doc_kind=doc_kind, chunk=c), max_tokens=4096)
        for i, c in enumerate(chunks, 1)
    ]
    combined = "\n\n".join(f"[Part {i}]\n{n}" for i, n in enumerate(notes, 1))
    return llm.complete(system, f"{prefix}\n\nNOTES FROM ALL PARTS OF THE {doc_kind.upper()}:\n{combined}",
                        max_tokens=8192).strip()
