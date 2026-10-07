"""Lab agent: lab sheet -> practical briefing (objectives, setup, steps, pitfalls, checklist, viva).
Guides and explains; never produces submission-ready answers. Pure: takes text, returns text."""
from __future__ import annotations

from ..llm import LLM
from .common import OCR_NOTE, summarize_document

SYSTEM_PROMPT = """You are a lab demonstrator for a computer science student.
Your job is to help the student GET THE PRACTICAL DONE THEMSELVES and UNDERSTAND IT.
Work only from the lab sheet provided; if the sheet is unclear or missing something (e.g. a due date), say so.

ACADEMIC INTEGRITY (strict):
• Never write the graded answers, final code, SQL queries, diagrams, calculations or report text that the lab asks the student to produce.
• Steps describe WHAT to do and WHICH concept or tool applies, not the finished solution.
• If a step needs code or a query, name the relevant syntax or command and what it does in general terms. Any example must be generic and must not solve the lab's actual task.
• If the material is itself a question sheet, give hints and point to the concepts. Don't answer it.

Write in plain text for a phone screen: no Markdown tables, no **bold**, no # headings. Use "•" for bullets.""" + OCR_NOTE

FORMAT = """🧪 {title} ({course}) · Lab
Objective: 1-2 sentences on what the student should be able to do after this lab.

Before you start:
• software, files, accounts and setup needed (from the sheet)

Steps:
1. a short numbered list: the real procedure condensed (what to do, not the answers)

Expected result: what "working" looks like (outputs, screens, behaviour), without giving the graded answers.

Common mistakes:
• 3-5 likely errors and how to spot or fix them

Submission checklist:
• exactly what the sheet says to hand in (files, format, naming), and by when. Use the Moodle deadlines in the context if the sheet doesn't say

Viva questions:
1. 5 questions like "Why did we use...?" or "What would happen if...?\""""


def summarize(llm: LLM, text: str, title: str, course: str, earlier: list[str],
              max_single_chars: int, deadlines: list[str] = ()) -> str:
    context = (
        "Earlier labs in this course:\n" + ("\n".join(f"- {t}" for t in earlier[-10:]) or "(none yet)")
        + "\n\nUpcoming Moodle deadlines for this course:\n" + ("\n".join(f"- {d}" for d in deadlines) or "(none known)")
    )
    return summarize_document(llm, SYSTEM_PROMPT, FORMAT.format(title=title, course=course), context, text,
                              "lab sheet", max_single_chars)
