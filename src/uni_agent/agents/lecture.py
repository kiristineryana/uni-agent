"""Lecture agent: lecture slides/notes -> concept summary for understanding the theory and exam prep.
Pure: takes text, returns text. No Telegram, no DB."""
from __future__ import annotations

from ..extract import chunk_text
from ..llm import LLM

SYSTEM_PROMPT = """You are a university lecture tutor for a computer science student.
Your job is to help the student UNDERSTAND THE THEORY in a lecture and prepare for exams.
Work only from the lecture material provided; if something is unclear in the source, say so rather than inventing it.
Write in plain text for a phone screen: no Markdown tables, no **bold**, no # headings. Use "•" for bullets.
Be concrete and concise. Define jargon in simple words."""

FORMAT = """Produce exactly this structure (keep the headings and emoji, fill in the content):

📘 {title} ({course}) · Lecture
TL;DR: 2-3 sentences on what this lecture is about and why it matters.

Key concepts:
• 5-8 bullets, each one idea explained in a sentence or two

Key terms:
• term: short definition (6-12 terms)

How it connects: 1-3 sentences linking to the earlier lectures listed below (if any are relevant), or to prerequisite ideas.

Likely exam questions:
1. 5 questions: a mix of recall ("Define...", "List...") and application ("Given..., what would...?")"""

CHUNK_PROMPT = """This is part {i} of {n} of a lecture. Write compact study notes for THIS part only:
the main concepts, definitions, examples and any formulas or diagrams described. Plain text, bullets with "•".

{chunk}"""


def summarize(llm: LLM, text: str, title: str, course: str, earlier: list[str],
              max_single_chars: int) -> str:
    earlier_txt = "\n".join(f"- {t}" for t in earlier[-10:]) or "(none recorded yet)"
    header = FORMAT.format(title=title, course=course) + f"\n\nEarlier lectures in this course:\n{earlier_txt}"

    if len(text) <= max_single_chars:
        return llm.complete(SYSTEM_PROMPT, f"{header}\n\nLECTURE MATERIAL:\n{text}", max_tokens=8192).strip()

    chunks = chunk_text(text, max_single_chars)
    notes = [
        llm.complete(SYSTEM_PROMPT, CHUNK_PROMPT.format(i=i, n=len(chunks), chunk=c), max_tokens=4096)
        for i, c in enumerate(chunks, 1)
    ]
    combined = "\n\n".join(f"[Part {i}]\n{n}" for i, n in enumerate(notes, 1))
    return llm.complete(SYSTEM_PROMPT, f"{header}\n\nNOTES FROM ALL PARTS OF THE LECTURE:\n{combined}",
                        max_tokens=8192).strip()
