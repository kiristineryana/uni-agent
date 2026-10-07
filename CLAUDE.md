# Uni Agent

Personal university agent: Moodle iCal deadlines + lecture/lab summaries + quizzes, talking over Telegram.
Full spec: `UNI_AGENT_BUILD_SPEC.md`. Build proceeds phase by phase (section 7); stop after each phase for the user to test.

## Run
- Windows, Python 3.13 in `.venv` (`py -3.13 -m venv .venv`, then `.venv\Scripts\pip install -e .[dev]`)
- `.venv\Scripts\python -m uni_agent` runs the agent; `--check` validates setup and exits
- `.venv\Scripts\python -m pytest`

## Layout
- `config.yaml` = non-secret settings; `.env` = secrets (never log/commit)
- `src/uni_agent/` — `orchestrator.py` routes; `agents/*` are pure (own SYSTEM_PROMPT constant, no Telegram); only `orchestrator` + `notify` send messages
- `data/` (gitignored) — `uni.db`, `summaries/lectures|labs/`

## Decisions (differ from spec defaults)
- Watched folders are per-course: `C:/Users/YOU/Desktop/UOWD/{205,214,235}` with lectures and labs mixed.
  Kind = `lectures/`/`labs/` subfolder if present, else filename heuristic, else ask via Telegram buttons.
- Files matching `ignore_patterns` (student ID = own submissions) are skipped.
- First run marks existing files as seen (no backfill summaries).
- LLM default: Anthropic; timezone Asia/Dubai. Auto-start via Windows Task Scheduler.

## Hard rules
Read-only toward Moodle; no Moodle password; chat-ID allowlist; Lab agent never writes graded answers/code.
