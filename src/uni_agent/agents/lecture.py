"""Lecture agent: lecture slides/notes -> concept summary for understanding the theory and exam prep.
Pure: takes text, returns text. No Telegram, no DB."""
from __future__ import annotations

from ..llm import LLM
from .common import OCR_NOTE, summarize_document

SYSTEM_PROMPT = """You are a university lecture tutor for a computer science student.
Your job is to help the student UNDERSTAND THE THEORY in a lecture and prepare for exams.
Work only from the lecture material provided; if something is unclear in the source, say so rather than inventing it.
Write in plain text for a phone screen: no Markdown tables, no **bold**, no # headings. Use "•" for bullets.
Be concrete and concise. Define jargon in simple words.""" + OCR_NOTE

FORMAT = """📘 {title} ({course}) · Lecture
TL;DR: 2-3 sentences on what this lecture is about and why it matters.

Key concepts:
• 5-8 bullets, each one idea explained in a sentence or two

Key terms:
• term: short definition (6-12 terms)

How it connects: 1-3 sentences linking to the earlier lectures in the context (if any are relevant), or to prerequisite ideas.

Likely exam questions:
1. 5 questions: a mix of recall ("Define...", "List...") and application ("Given..., what would...?")"""


def summarize(llm: LLM, text: str, title: str, course: str, earlier: list[str],
              max_single_chars: int) -> str:
    context = "Earlier lectures in this course:\n" + ("\n".join(f"- {t}" for t in earlier[-10:]) or "(none yet)")
    return summarize_document(llm, SYSTEM_PROMPT, FORMAT.format(title=title, course=course), context, text,
                              "lecture", max_single_chars)
