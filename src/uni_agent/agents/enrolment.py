"""Enrolment filter: decides whether a Moodle calendar item is the student's to be alerted about.

Rules (from the student's timetable):
1. Only enrolled courses. Any other course code is ignored, never alerted.
2. Section-specific items (they name a tutor, or a weekday/time group) are alerted only when they match one of the
   enrolled sessions (same tutor, or same day and time). Another group of the same course is ignored.
3. Course-wide items (no tutor or group in the title) are alerted for enrolled courses.
4. Anything that doesn't clearly say its course or section is NOT alerted; it's flagged as "unclear" instead.
Pure: no Telegram, no DB.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

DAYS = {"mon": "mon", "monday": "mon", "tue": "tue", "tues": "tue", "tuesday": "tue", "wed": "wed",
        "weds": "wed", "wednesday": "wed", "thu": "thu", "thur": "thu", "thurs": "thu", "thursday": "thu",
        "fri": "fri", "friday": "fri", "sat": "sat", "saturday": "sat", "sun": "sun", "sunday": "sun"}
_DAY_RE = re.compile(r"(?<![a-z])(" + "|".join(sorted(DAYS, key=len, reverse=True)) + r")(?![a-z])", re.I)
_TIME_RE = re.compile(r"(?<!\d)(\d{1,2})[:.](\d{2})\s*(am|pm)?(?![a-z0-9])", re.I)
_COURSE_RE = re.compile(r"\b([A-Z]{3,4}\d{3})\b")
_PAREN_RE = re.compile(r"\(([^()]*)\)")
# Section labels look like "_Monday", "(Wednesday)", "Wednesday Batches", "Tue 10.30AM", "group", "section".
_SECTION_WORDS = re.compile(r"\b(batch|batches|group|groups|section|sections|class)\b", re.I)
# Words that can appear capitalised in parentheses without being a person's name.
NOT_NAMES = {"part", "parts", "remotely", "proctored", "online", "optional", "practice", "draft", "final", "group",
             "individual", "updated", "extended", "extension", "resubmission", "late", "new", "revised", "week",
             "lab", "labs", "lecture", "quiz", "test", "exam", "submission", "report", "code", "video", "pdf",
             "word", "zip", "moodle", "turnitin", "required", "graded", "ungraded", "mandatory", "bonus"}


@dataclass(frozen=True)
class Session:
    course: str
    kind: str
    staff: str
    day: str  # mon..sun
    time: str  # "HH:MM", 24h


@dataclass(frozen=True)
class Decision:
    status: str  # "open" (alert) | "ignored" | "unclear"
    reason: str


def staff_tokens(staff: str) -> set[str]:
    """'Emery Fo' -> {'emery'}: whole words of 4+ letters (timetable names are often truncated)."""
    return {w.lower() for w in re.findall(r"[A-Za-z]+", staff) if len(w) >= 4}


def _norm_time(h: int, m: int, ampm: str | None) -> str:
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and h < 12:
            h += 12
        if ampm == "am" and h == 12:
            h = 0
    elif h < 8:  # classes run ~08:00-20:00, so "2:30" means 14:30
        h += 12
    return f"{h:02d}:{m:02d}"


def _words(title: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z]+", title)}


def classify(title: str, course: str | None, enrolled_courses: set[str], sessions: list[Session],
             other_staff: set[str] = frozenset()) -> Decision:
    if not enrolled_courses:
        return Decision("open", "no enrolment configured")
    code = (course or "").upper() or None
    if code is None or not _COURSE_RE.fullmatch(code):
        in_title = _COURSE_RE.findall(title.upper())
        code = in_title[0] if len(set(in_title)) == 1 else None
    if code is None:
        return Decision("unclear", "no course code on this Moodle item")
    if code not in enrolled_courses:
        return Decision("ignored", f"{code} is not one of your courses")

    mine = [s for s in sessions if s.course.upper() == code]
    words = _words(title)

    # A tutor of one of my sessions is named: it's my group.
    for s in mine:
        if staff_tokens(s.staff) & words:
            return Decision("open", f"your {s.kind} section ({s.staff})")
    # Another group's tutor is named.
    named_other = {n for n in other_staff if n.lower() in words}
    if named_other:
        return Decision("ignored", f"another group's section ({', '.join(sorted(named_other))})")

    days = {DAYS[d.lower()] for d in _DAY_RE.findall(title)}
    times = {_norm_time(int(h), int(m), ap) for h, m, ap in _TIME_RE.findall(title)}
    my_slots = {(s.day, s.time) for s in mine}
    my_days = {s.day for s in mine}
    if days:
        if times:
            if any((d, t) in my_slots for d in days for t in times):
                return Decision("open", "your section's day and time")
            return Decision("ignored", "another group's day/time ("
                            + ", ".join(sorted(f"{d.title()} {t}" for d in days for t in times)) + ")")
        section_like = bool(_SECTION_WORDS.search(title) or re.search(r"[_(]\s*(" + "|".join(DAYS) + r")\b", title, re.I))
        if not days & my_days:
            if section_like:
                return Decision("ignored", f"another group's day ({', '.join(sorted(d.title() for d in days))})")
            return Decision("unclear", "mentions a day that isn't one of your sessions; check if it applies")
        return Decision("unclear", "mentions your class day but not the tutor or time")

    # A capitalised name in brackets, e.g. "(`Morgan)", that isn't one of my tutors.
    for inner in _PAREN_RE.findall(title):
        name_words = re.findall(r"[A-Za-z]+", inner)
        if 1 <= len(name_words) <= 3 and all(w[0].isupper() and w.lower() not in NOT_NAMES for w in name_words) \
                and not re.search(r"\d", inner):
            return Decision("unclear", f"names '{' '.join(name_words)}', who isn't one of your tutors")

    return Decision("open", "course-wide item")
