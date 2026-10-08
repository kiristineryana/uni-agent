# Uni Agent

A personal university assistant that runs in the background on Windows and talks to you on **Telegram**.

- ⏰ **Deadlines** from your Moodle calendar feed, with alerts 7 days, 3 days, 1 day and 3 hours before
- 📘 **Lecture agent**: slides → TL;DR, key concepts, key terms, likely exam questions
- 🧪 **Lab agent**: lab sheet → objective, setup, steps, common mistakes, submission checklist, viva questions
  (it guides and explains; it never writes graded answers or code)
- 🎯 **Exam agent**: `/quiz` with marking and spaced repetition (Leitner boxes)
- 🗓️ **Weekly review** every Sunday 18:00: lectures vs labs, weak topics, what's due, what to revise

Read-only toward Moodle. No Moodle password is stored. Only your Telegram account can use the bot.

## Telegram commands

| Command | What it does |
|---|---|
| `/help` | List commands |
| `/deadlines` | Everything due in the next 14 days, with countdowns |
| `/lecture [course]` then a file | Lecture summary (e.g. `/lecture 205`) |
| `/lab [course]` then a file | Lab briefing (e.g. `/lab 235`) |
| *a file with no command* | Bot asks **Lecture or Lab?** with buttons |
| 📷 *a photo or album* | Text is read with OCR, then summarised like a file. Caption e.g. `/lecture 235 Week 3 board` (type, course, title). An album = one document |
| 🎙️ *a voice note or audio file* | Transcribed locally. **During a quiz it's your answer**; otherwise summarised like a file (caption/buttons). Short memos just get the transcript back |
| `/quiz lectures <topic or week>` | Theory quiz, e.g. `/quiz lectures normalization`, `/quiz lectures week 3` |
| `/quiz labs <topic or week>` | Practical / viva quiz |
| `/skip`, `/stop` | Skip a question (counts as wrong) / end the quiz |
| `/files [course]` | Live list of the files in your course folders (read from disk each time) with their status: summarised, waiting for Lecture/Lab, here before the bot, ignored... e.g. `/files 235` |
| `/week` | Weekly review now |
| `/status` | Last Moodle sync, docs summarised, next deadline, LLM usage today, last problem |
| `/sync` | Pull Moodle deadlines now |
| `/fakedeadline <min>` | Test alert: fake item due in `<min>` minutes |

You can also put the command in a file's caption, e.g. send a PDF captioned `/lab 235`.

**Photos:** for best OCR, send the photo as a *File* (📎 → File) so Telegram doesn't compress it; keep the page flat, well lit and filling the frame. Printed text and slides work well; handwriting doesn't.

**Voice:** speech-to-text runs on your PC with faster-whisper (`voice_model` in `config.yaml`, default `base.en`; the ~150 MB model is downloaded once into `data\models`). Audio never leaves the computer; only the transcript is summarised.

## How files get in

The agent watches the course folders in `config.yaml` (`course_folders`). Drop a downloaded file in and:

1. A `lectures\` or `labs\` subfolder inside a course folder decides the type, otherwise
2. the filename does (`lab`, `tutorial`, `practice`… → lab; `lecture`, `L01`, `slides`… → lecture), otherwise
3. the bot asks you on Telegram.

**Moodle file sync (optional, not available at UOW: no web-service key):** with `MOODLE_TOKEN` in `.env`, lecture/lab files from your Moodle courses are downloaded every hour into `<course folder>\Moodle\` (sorted into `lectures\`/`labs\` when the name makes it clear). Read-only: only three read functions can be called. The first sync only makes local copies (no summaries); later new or updated files are summarised like any new file. Files you already have are not duplicated. Courses without a matching course folder are skipped.

Files matching `ignore_patterns` (e.g. your own submissions with your student ID) are never processed.
Files that were already in the folders on first run are marked as seen and not summarised; send any of them with `/lecture` or `/lab` to summarise it.
Identical files are only ever processed once.

**Image-only slides and scanned PDFs/DOCX** are read with the OCR engine built into Windows (`ocr_enabled` in `config.yaml`). It runs locally, costs nothing, and only kicks in for slides/pages with almost no real text (about 0.3 s per image). The recognised text is marked so the summariser expects small OCR errors.

## Setup (from scratch)

1. **Python 3.11+** and a virtualenv:
   ```powershell
   py -3.13 -m venv .venv
   .venv\Scripts\pip install -e .[dev]
   ```
2. **Secrets**: copy `.env.example` to `.env` (never commit `.env`) and fill in:
   - `TELEGRAM_BOT_TOKEN`: Telegram → `@BotFather` → `/newbot`
   - `TELEGRAM_CHAT_ID`: message `@userinfobot`, copy your numeric Id. Then open your bot and press **Start**.
   - `LLM_API_KEY`: Gemini key from aistudio.google.com/apikey (or an Anthropic key, see below)
   - `MOODLE_ICAL_URL`: Moodle → Calendar → *Import or export calendars* → *Export calendar* → All courses,
     Recent and next 60 days → *Get calendar URL*
3. **Settings**: edit `config.yaml` (course folders, course names, provider, alert offsets, daily token cap).
4. **Check**: `.venv\Scripts\python -m uni_agent --check` then `.venv\Scripts\python -m uni_agent --test-alert`.
5. **Auto-start at login**: `powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1`
   then `Start-ScheduledTask -TaskName UniAgent`. Remove with `scripts\uninstall_autostart.ps1`.

## LLM provider

Set `llm.provider` in `config.yaml` to `gemini` or `anthropic` (and put that provider's key in `LLM_API_KEY`).

- **Gemini free tier**: about 20 requests/day on Flash. A summary is 1 request; starting a quiz is 1 per new
  document; each quiz answer is 1. Models in `gemini_models` are tried in order, so when one is overloaded the next is used.
  Note: on the free tier Google may use what you send to improve its products. Check your university's AI policy.
- `daily_token_cap` stops all LLM calls for the day once reached (resets at midnight, Asia/Dubai).

## Running it

| Task | Command (PowerShell) |
|---|---|
| Status of the background task | `Get-ScheduledTask UniAgent \| Get-ScheduledTaskInfo` |
| Stop / start | `Stop-ScheduledTask UniAgent` / `Start-ScheduledTask UniAgent` |
| Run in a terminal instead (stop the task first) | `.venv\Scripts\python -m uni_agent` |
| Logs | `logs\uni_agent.log` (rotates at 1 MB, keeps 5) |
| Tests | `.venv\Scripts\python -m pytest` |

Only one copy can run at a time (`data\uni_agent.lock`); a second copy exits immediately.
Summaries are saved as Markdown in `data\summaries\lectures\` and `data\summaries\labs\`.

## Reliability

- Network drops, laptop sleep, Wi-Fi not up at login: the bot waits and reconnects on its own.
  Telegram sends are retried with backoff; an alert that couldn't be delivered is retried on the next check, never duplicated.
- Real problems (a crashed job, Moodle sync failing 3 times in a row) are sent to Telegram as ⚠️ alerts,
  at most once an hour per kind, and shown under "Last problem" in `/status`. Alerts never contain tokens or URLs.
- The agent only runs while the computer is on and awake.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bot doesn't answer | `Get-ScheduledTask UniAgent` should be *Running*; check the end of `logs\uni_agent.log` |
| "Chat not found" | Open your bot in Telegram and press **Start**; check `TELEGRAM_CHAT_ID` |
| "Another Uni Agent is already running" | Expected if the task is running; stop it before starting a terminal copy |
| "Gemini free-tier limit reached (429)" | Daily free quota used up; try tomorrow, or switch provider |
| "no readable text" | Even OCR found nothing (blank/handwritten/very low-res images). Check `ocr_enabled: true` |
| Alerts for lab sections you're not in | Add part of their titles to `deadline_ignore` in `config.yaml` |

## Layout

```
config.yaml            non-secret settings          .env          secrets (gitignored)
src/uni_agent/
  main.py              startup, scheduler, lock      bot.py        Telegram handlers (thin)
  orchestrator.py      routes commands/events to agents (only it + notify.py send messages)
  llm.py               Gemini/Anthropic behind one interface + daily cap
  notify.py            send with retries, 4096-char splitting       errors.py   rate-limited error alerts
  extract.py           pdf/pptx/docx/txt → text, hashing, chunking
  sources/             moodle_ical.py, folder_watch.py
  agents/              ingest, lecture, lab, deadlines, exam, weekly (each with its own prompt; no Telegram)
scripts/               install_autostart.ps1, uninstall_autostart.ps1
data/                  uni.db, summaries/, uploads/ (gitignored)
```
