"""One small LLM interface with Gemini and Anthropic providers, plus a daily token cap.
Switch provider with `llm.provider` in config.yaml. Calls are synchronous; run them in a thread."""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Protocol

from . import db
from .config import Settings

log = logging.getLogger(__name__)


class LLMError(Exception):
    """Provider call failed. Message is safe to show the user (no keys)."""


class BudgetExceeded(LLMError):
    pass


@dataclass
class LLMResult:
    text: str
    tokens_in: int
    tokens_out: int


class Provider(Protocol):
    name: str

    def complete(self, system: str, prompt: str, max_tokens: int) -> LLMResult: ...


class GeminiProvider:
    """Tries each model in order; moves to the next when one is overloaded (5xx) or rate-limited (429)."""

    name = "gemini"

    def __init__(self, api_key: str, models: list[str] | str):
        from google import genai
        from google.genai import types

        self._types = types
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=90_000))
        self.models = [models] if isinstance(models, str) else list(models)

    def _call(self, model: str, system: str, prompt: str, max_tokens: int) -> LLMResult:
        t = self._types
        resp = self.client.models.generate_content(
            model=model,
            contents=prompt,
            config=t.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
                temperature=0.3,
                automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        u = resp.usage_metadata
        text = resp.text or ""
        if not text.strip():
            raise LLMError(f"{model} returned an empty response.")
        out = (u.candidates_token_count or 0) + (getattr(u, "thoughts_token_count", 0) or 0)
        return LLMResult(text, u.prompt_token_count or 0, out)

    def complete(self, system: str, prompt: str, max_tokens: int) -> LLMResult:
        reasons = []
        for model in self.models:
            try:
                return self._call(model, system, prompt, max_tokens)
            except LLMError as e:
                reasons.append(str(e))
            except Exception as e:
                code = getattr(e, "code", None)
                reasons.append(f"{model}: {code or type(e).__name__}")
                if not (code == 429 or (isinstance(code, int) and code >= 500)):
                    break  # bad request / auth: another model won't help
            log.warning("Gemini %s unavailable, trying next model", model)
        if any("429" in r for r in reasons):
            raise LLMError("Gemini free-tier limit reached (429). Try again later.")
        raise LLMError("Gemini unavailable (" + "; ".join(reasons) + ")")


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str):
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, timeout=180.0, max_retries=2)
        self.model = model

    def complete(self, system: str, prompt: str, max_tokens: int) -> LLMResult:
        try:
            resp = self.client.messages.create(
                model=self.model,
                system=system,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except self._anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic call failed (HTTP {e.status_code})") from e
        except self._anthropic.APIError as e:
            raise LLMError(f"Anthropic call failed ({type(e).__name__})") from e
        text = "".join(b.text for b in resp.content if b.type == "text")
        return LLMResult(text, resp.usage.input_tokens, resp.usage.output_tokens)


class LLM:
    """Provider + daily token cap recorded in `llm_usage`."""

    def __init__(self, provider: Provider, conn: sqlite3.Connection, daily_cap: int, tz):
        self.provider = provider
        self.conn = conn
        self.daily_cap = daily_cap
        self.tz = tz

    def _today(self) -> str:
        return db.utcnow().astimezone(self.tz).date().isoformat()

    def used_today(self) -> int:
        row = self.conn.execute(
            "SELECT tokens_in + tokens_out AS n FROM llm_usage WHERE day = ?", (self._today(),)
        ).fetchone()
        return row["n"] if row else 0

    def complete(self, system: str, prompt: str, max_tokens: int = 4096) -> str:
        if self.used_today() >= self.daily_cap:
            raise BudgetExceeded(f"Daily LLM cap of {self.daily_cap:,} tokens reached; resets at midnight.")
        result = self.provider.complete(system, prompt, max_tokens)
        with self.conn:
            self.conn.execute(
                "INSERT INTO llm_usage(day, tokens_in, tokens_out) VALUES (?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET tokens_in = tokens_in + excluded.tokens_in, "
                "tokens_out = tokens_out + excluded.tokens_out",
                (self._today(), result.tokens_in, result.tokens_out),
            )
        log.info("LLM %s: %d in / %d out tokens", self.provider.name, result.tokens_in, result.tokens_out)
        return result.text


def make_llm(settings: Settings, conn: sqlite3.Connection) -> LLM | None:
    key = settings.secrets.llm_api_key
    if not key:
        return None
    cfg = settings.llm
    if cfg.provider == "gemini":
        provider: Provider = GeminiProvider(key.get_secret_value(), cfg.gemini_models)
    elif cfg.provider == "anthropic":
        provider = AnthropicProvider(key.get_secret_value(), cfg.anthropic_model)
    else:
        raise ValueError(f"Unknown llm.provider: {cfg.provider!r} (use 'gemini' or 'anthropic')")
    return LLM(provider, conn, cfg.daily_token_cap, settings.tz)
