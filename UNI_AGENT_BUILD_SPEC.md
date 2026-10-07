# UNI AGENT: Build Spec for Claude Code

> **How to use this file:** Create an empty folder `uni-agent/`, put this file inside it, open Claude Code there, and say:
> *"Read UNI_AGENT_BUILD_SPEC.md. Ask me the questions in section 2, then build it phase by phase. Stop after each phase so I can test it."*

---

## 1. Goal

A personal **university agent** that runs in the background on my computer and talks to me on my **phone via Telegram**. It has an **orchestrator** and five specialist **sub-agents**. Lectures and labs are handled by **two separate agents**, because they need different things from me:

| Sub-agent | Job |
|---|---|
| **Ingest** | Pulls deadlines/events from Moodle (calendar feed) and new files from two watched folders (`lectures/` and `labs/`). Tags every document as `lecture` or `lab`. |
| **Lecture agent** | Turns lecture slides and notes into concept summaries, key terms, and likely exam questions. Goal: *understand the theory*. |
| **Lab agent** | Turns lab sheets into a practical briefing: objectives, setup, steps, expected results, common errors, submission checklist, and viva-style questions. Goal: *get the practical done and understand it*. |
| **Deadline** | Sends alerts as deadlines approach (7 days, 3 days, 1 day, 3 hours). Lab and lecture-related items are labelled separately. |
| **Exam** | Quizzes me on lectures (theory) or labs (practical/viva), marks answers, tracks weak topics. |
| **Weekly Review** | Every Sunday sends separate "lectures" and "labs" sections, what's due next, and what to revise. |

**Hard rules**
- **Read-only.** The agent never submits, posts or changes anything on Moodle.
- **No Moodle password stored, ever.** Use a calendar export URL or a token that I generate myself (see 4).
- Only my Telegram account can talk to the bot (chat ID allowlist).
- Secrets live in `.env` only. Never log them, never commit them.
- Course content stays local except the text sent to the LLM for summarising. I will check my university's AI policy.
- **Academic integrity:** the Lab agent explains, guides and checks my understanding. It must **not** produce submission-ready answers, code or reports for graded work. If asked, it gives hints and explains concepts instead.

---

## 2. Questions to ask me before writing code

1. Which OS am I on (Windows / macOS / Linux)? This decides how it auto-starts in the background.
2. My Moodle base URL. Do I want to try the calendar export first? (default: yes)
3. Folders to watch. Default: `~/UniAgent/inbox/lectures/` and `~/UniAgent/inbox/labs/`. I may put course subfolders inside them (e.g. `lectures/CSIT121/`). The subfolder name becomes the course.
4. LLM provider: **Anthropic API** (default) or **Gemini** (free tier). Build a small provider interface so I can switch with one config line.
5. Confirm timezone: `Asia/Dubai`.

---

## 3. Tech stack

- Python 3.11+, `uv` or `venv`
- `python-telegram-bot` (long polling, so no public server or port forwarding is needed)
- `APScheduler` for background jobs
- `sqlite3` (stdlib) for state
- `icalendar` + `httpx` for the Moodle calendar feed
- `pypdf`, `python-pptx`, `python-docx` for reading files
- `watchdog` for the folder watcher
- `pydantic` + `python-dotenv` for config
- `anthropic` SDK and/or `google-genai` behind one `LLM` interface
- `pytest` for tests

---

## 4. Moodle connection (try in this order)

1. **Calendar export URL (preferred).** Moodle → Calendar → *Import or export calendars* → *Export calendar* → "All courses" → "Recent and next 60 days" → *Get calendar URL*. This is a private link with a token; I put it in `.env` as `MOODLE_ICAL_URL`. Parse it for assignments, quizzes and exams.
2. **Moodle web service token (optional).** If my Moodle allows it (Profile → Preferences → Security keys), I set `MOODLE_TOKEN` and use the REST API read-only (`core_enrol_get_users_courses`, `core_course_get_contents`) to list courses and files. If the university has disabled this, skip it and don't work around it.
3. **Folder fallback (always on).** I drop downloaded files into `inbox/lectures/` or `inbox/labs/` and the agent handles them. The folder decides which agent runs.

Do **not** scrape the Moodle login page, automate a browser login, or handle SSO or two-factor flows.

---

## 5. Architecture

```
uni-agent/
├── CLAUDE.md                 # short project notes for Claude Code (create it)
├── .env.example              # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, LLM_API_KEY, MOODLE_ICAL_URL
├── config.yaml               # schedules, lecture_inbox, lab_inbox, provider, alert offsets, daily token cap
├── src/uni_agent/
│   ├── main.py               # starts bot + scheduler
│   ├── orchestrator.py       # routes commands/events to sub-agents
│   ├── llm.py                # LLM interface + providers
│   ├── db.py                 # SQLite schema + helpers
│   ├── notify.py             # send_telegram(text)
│   ├── bot.py                # Telegram command + file handlers
│   ├── extract.py            # pdf/pptx/docx/txt -> text, chunking
│   ├── sources/
│   │   ├── moodle_ical.py
│   │   ├── moodle_ws.py      # optional
│   │   └── folder_watch.py   # watches both inboxes, tags kind=lecture|lab
│   └── agents/
│       ├── ingest.py
│       ├── lecture.py        # own system prompt: theory/concepts/exam focus
│       ├── lab.py            # own system prompt: practical/steps/viva focus
│       ├── deadlines.py
│       ├── exam.py
│       └── weekly.py
├── data/                     # gitignored: uni.db, summaries/
└── tests/
```

Each sub-agent is a module with **its own system prompt** (stored in a constant at the top of the file), a clear input/output, and no direct access to Telegram. Only the orchestrator and `notify.py` send messages. The orchestrator is the only thing that knows which agent handles which command or event.

### Database tables
- `items(id, course, title, type, due_at, source, uid UNIQUE, status)`: deadlines/exams
- `documents(id, path, sha256 UNIQUE, kind CHECK(kind IN ('lecture','lab')), course, ingested_at, summary_path)`
- `alerts_sent(item_id, offset_label, sent_at, UNIQUE(item_id, offset_label))`: prevents duplicate alerts
- `quiz_questions(id, doc_id, kind, topic, question, answer, box, next_due)`: simple Leitner boxes (`kind` copies the document's kind)
- `quiz_log(id, question_id, correct, answered_at)`
- `llm_usage(day, tokens_in, tokens_out)`: enforces the daily cap

---

## 6. Behaviour spec

### Telegram commands
| Command | Behaviour |
|---|---|
| `/start`, `/help` | Show commands. Ignore anyone whose chat ID isn't mine. |
| `/deadlines` | Next 14 days, sorted, with countdowns. |
| `/lecture` | Then send a file: the **Lecture agent** replies with the lecture summary format (section 6). |
| `/lab` | Then send a file: the **Lab agent** replies with the lab briefing format (section 6). |
| *(sending a file with no command)* | Bot asks "Lecture or Lab?" with two buttons, then routes it. Voice and photo support is **stretch**. |
| `/quiz lectures <topic or week>` | Exam agent asks theory questions one at a time, marks the reply, explains, continues until `/stop`. |
| `/quiz labs <topic or week>` | Exam agent asks practical/viva-style questions ("why did we use X?", "what would happen if...?"). |
| `/week` | Run the weekly review now. |
| `/status` | Last sync time, docs ingested, next alerts, today's LLM usage. |

### Background jobs (timezone `Asia/Dubai`)
- **Ingest:** every 30 min. Fetch the iCal feed, upsert into `items`. The folder watcher runs continuously. New files are hashed (skip duplicates), extracted, then sent to the **Lecture agent** (from `lectures/`) or the **Lab agent** (from `labs/`), saved to `data/summaries/lectures/` or `data/summaries/labs/`. A short "📘 New lecture summary" or "🧪 New lab briefing" message is sent.
- **Deadline check:** every 15 min. For each upcoming item, send an alert at **7d, 3d, 1d and 3h** before the due time. Record in `alerts_sent` so each is sent once. Format: `⏰ [Course] Assignment 2 is due in 3 days (Fri 17 Oct, 23:59)`.
- **Weekly review:** Sunday 18:00. Send two sections, **Lectures** and **Labs**, each with what I covered this week and the weakest quiz topics. Then what is due in the next 7 days and 3 suggested revision tasks.

### Lecture agent output format
```
📘 <Title> (<Course>) · Lecture
TL;DR: 2-3 sentences
Key concepts: 5-8 bullets
Key terms: term: short definition
How it connects: link to earlier lectures if known
Likely exam questions: 5 (mix of recall and application)
```

### Lab agent output format
```
🧪 <Title> (<Course>) · Lab
Objective: 1-2 sentences, what I should be able to do after
Before you start: software, files, setup needed
Steps: short numbered list, the real procedure condensed
Expected result: what "working" looks like
Common mistakes: 3-5 likely errors and how to spot them
Submission checklist: what the lab sheet says to hand in, and by when
Viva questions: 5 ("why did we...?", "what happens if...?")
```
The Lab agent explains and guides. It does not write the graded answers or code for me (see hard rules).

Long documents: chunk by section, summarise chunks, then combine. Telegram messages max 4096 characters, so split cleanly. Save the full output as markdown on disk.

### Exam agent
- Generate questions from stored summaries/documents for the requested topic, filtered by `kind`: lectures give theory questions, labs give practical and viva-style questions.
- Mark answers as correct, partly correct or wrong, and explain briefly.
- Move the question between Leitner boxes. Wrong goes back to box 1. Correct moves up a box.
- `/week` and the weekly review use `quiz_log` to find weak topics.

---

## 7. Build phases (stop after each one for me to test)

| # | Phase | Done when | Est. |
|---|---|---|---|
| 0 | **Scaffold**: repo, venv, deps, `.env.example`, `config.yaml`, DB schema, `.gitignore`, `CLAUDE.md` | `pytest` runs, app starts and exits cleanly | 20 min |
| 1 | **Phone link**: Telegram bot, allowlist, `/start`, `/status`, `notify.py` | I message the bot from my phone and it replies; a test alert arrives | 20 min |
| 2 | **Deadlines**: Moodle iCal ingest, `/deadlines`, alert scheduler with dedupe | `/deadlines` shows my real items; a fake item due in 1 minute triggers an alert once | 40 min |
| 3a | **Lecture agent**: extractors, folder watcher (`lectures/`), LLM interface, `/lecture` + file upload | Dropping lecture slides in `inbox/lectures/` sends a good summary to my phone | 40 min |
| 3b | **Lab agent**: `labs/` watcher, lab prompt and format, `/lab`, "Lecture or Lab?" buttons for untagged files | Dropping a lab sheet in `inbox/labs/` sends a lab briefing, and it clearly differs from a lecture summary | 30 min |
| **MVP** | *Phases 0-3b are the minimum for tonight.* | | |
| 4 | **Exam agent**: `/quiz lectures` and `/quiz labs`, marking, Leitner logic | A full quiz round on each kind | 40 min |
| 5 | **Weekly review**: job + `/week`, with separate Lectures and Labs sections | `/week` returns a sensible review | 20 min |
| 6 | **Run in background**: auto-start on login (Task Scheduler / launchd / systemd user service, depending on OS), log file with rotation | Reboot, and the bot still answers without me opening a terminal | 20 min |
| 7 | **Hardening**: retries/backoff, LLM daily cap, error alerts to Telegram, tests for dedupe/alerts/chunking, README | `pytest` passes; killing the network and restoring it doesn't crash the app | 30 min |
| Stretch | Moodle web-service course/file sync, voice notes (transcribe), photo-of-notes OCR, Notion/Google Calendar export | | |

---

## 8. Working agreements for Claude Code

- Build one phase at a time. Show me how to test it, then wait.
- Prefer simple and boring. No frameworks I don't need.
- Write tests for: alert dedupe, offset calculation across timezones, file hashing, message splitting, the allowlist, and routing (a file in `labs/` must always reach the Lab agent, never the Lecture agent).
- If something needs a manual step from me (BotFather, API key, Moodle URL), tell me exactly what to click and where to paste it.
- Do not run anything that deletes files outside `uni-agent/`.
- Use Claude Code subagents while building if useful (for example, a reviewer to check each phase and a test writer), kept in `.claude/agents/`. These are separate from the runtime sub-agents above.
- Commit after each phase with a clear message.

---

## 9. Manual setup I must do myself (do these first, ~10 min)

1. **Telegram bot:** open Telegram → search `@BotFather` → `/newbot` → copy the token → `TELEGRAM_BOT_TOKEN`.
2. **My chat ID:** message `@userinfobot` → copy my numeric ID → `TELEGRAM_CHAT_ID`.
3. **LLM key:** create an API key with my chosen provider → `LLM_API_KEY`. Set a spending limit if the provider allows.
4. **Moodle calendar URL:** see section 4, step 1 → `MOODLE_ICAL_URL`.
5. Create the two inbox folders (`inbox/lectures/` and `inbox/labs/`) and tell Claude Code their path.

---

## 10. Limits to be upfront about

- The agent only runs while my computer is on and awake. For 24/7 alerts, it can later move to a small cloud server or a Raspberry Pi.
- A Moodle iCal feed shows deadlines and events, **not** lecture files. Files come from the two inbox folders (or the optional web service).
- Summaries are study aids, not a substitute for reading the material. The exam agent can be wrong, so check answers against the slides.
