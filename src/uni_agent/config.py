"""Loads config.yaml (non-secret settings) and .env (secrets) into one Settings object."""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class LLMConfig(BaseModel):
    provider: str = "anthropic"
    anthropic_model: str = "claude-sonnet-5-5"
    gemini_models: list[str] = Field(default_factory=lambda: ["gemini-3.8-flash", "gemini-3.5-flash-lite"])
    daily_token_cap: int = 400_000
    max_single_request_chars: int = 300_000


class WeeklySchedule(BaseModel):
    day_of_week: str = "sun"
    hour: int = 18
    minute: int = 0


class Schedules(BaseModel):
    ingest_every_minutes: int = 30
    deadline_check_every_minutes: int = 15
    weekly_review: WeeklySchedule = WeeklySchedule()


class Secrets(BaseModel):
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: int | None = None
    llm_api_key: SecretStr | None = None
    moodle_ical_url: SecretStr | None = None
    moodle_token: SecretStr | None = None


class Settings(BaseModel):
    timezone: str = "Asia/Dubai"
    course_folders: list[Path] = Field(default_factory=list)
    course_names: dict[str, str] = Field(default_factory=dict)
    ignore_patterns: list[str] = Field(default_factory=list)
    backfill_existing: bool = False
    llm: LLMConfig = LLMConfig()
    schedules: Schedules = Schedules()
    alert_offsets: dict[str, int] = Field(
        default_factory=lambda: {"7d": 10080, "3d": 4320, "1d": 1440, "3h": 180}
    )
    deadlines_window_days: int = 14
    deadline_ignore: list[str] = Field(default_factory=list)
    ocr_enabled: bool = True
    moodle_base_url: str | None = None  # default: taken from MOODLE_ICAL_URL
    moodle_sync_every_minutes: int = 60
    moodle_max_file_mb: int = 50
    voice_enabled: bool = True
    voice_model: str = "base.en"
    voice_language: str | None = "en"
    data_dir: Path = PROJECT_ROOT / "data"
    log_dir: Path = PROJECT_ROOT / "logs"
    secrets: Secrets = Secrets()

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "uni.db"

    def course_name(self, folder: Path) -> str:
        return self.course_names.get(folder.name, folder.name)


def _env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def load_settings(config_path: Path | None = None, env_path: Path | None = None) -> Settings:
    config_path = config_path or PROJECT_ROOT / "config.yaml"
    load_dotenv(env_path or PROJECT_ROOT / ".env")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    chat_id = _env("TELEGRAM_CHAT_ID")
    raw["secrets"] = Secrets(
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=int(chat_id) if chat_id else None,
        llm_api_key=_env("LLM_API_KEY"),
        moodle_ical_url=_env("MOODLE_ICAL_URL"),
        moodle_token=_env("MOODLE_TOKEN"),
    )
    return Settings(**raw)
