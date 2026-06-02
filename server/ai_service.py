"""AI assistant integration for group-chat @AI mentions.

The service is intentionally small and dependency-free. If OPENAI_API_KEY is
configured, it calls an OpenAI-compatible chat completions endpoint through
the standard library. Without a key, it returns a deterministic fallback so
local development and tests do not depend on external services.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Any, Protocol
from urllib import error, request


AI_TRIGGER = "@AI"
DEFAULT_ASSISTANT_NAME = "AI助手"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_MIMO_MODEL = "mimo-v2.5-pro"
DEFAULT_AI_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_COMPLETION_TOKENS = 1024


class AIResponder(Protocol):
    assistant_name: str

    def answer(
        self,
        prompt: str,
        *,
        username: str | None = None,
        group_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> str:
        ...


@dataclass(frozen=True, slots=True)
class AIConfig:
    api_key: str | None = None
    base_url: str = DEFAULT_OPENAI_BASE_URL
    model: str = DEFAULT_OPENAI_MODEL
    timeout_seconds: float = DEFAULT_AI_TIMEOUT_SECONDS
    assistant_name: str = DEFAULT_ASSISTANT_NAME
    max_completion_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS
    temperature: float = 1.0
    top_p: float = 0.95

    @classmethod
    def from_env(cls) -> "AIConfig":
        load_dotenv()
        mimo_api_key = os.getenv("MIMO_API_KEY")
        api_key = os.getenv("OPENAI_API_KEY") or os.getenv("AI_API_KEY") or mimo_api_key
        raw_base_url = (
            os.getenv("OPENAI_BASE_URL")
            or os.getenv("AI_BASE_URL")
            or os.getenv("MIMO_BASE_URL")
            or ("https://api.xiaomimimo.com/v1" if mimo_api_key else DEFAULT_OPENAI_BASE_URL)
        )
        return cls(
            api_key=api_key,
            base_url=normalize_chat_completions_url(raw_base_url),
            model=os.getenv("OPENAI_MODEL")
            or os.getenv("AI_MODEL")
            or os.getenv("MIMO_MODEL")
            or (DEFAULT_MIMO_MODEL if mimo_api_key else DEFAULT_OPENAI_MODEL),
            timeout_seconds=float(os.getenv("AI_TIMEOUT_SECONDS", str(DEFAULT_AI_TIMEOUT_SECONDS))),
            assistant_name=os.getenv("AI_ASSISTANT_NAME", DEFAULT_ASSISTANT_NAME),
            max_completion_tokens=int(os.getenv("AI_MAX_COMPLETION_TOKENS", str(DEFAULT_MAX_COMPLETION_TOKENS))),
            temperature=float(os.getenv("AI_TEMPERATURE", "1.0")),
            top_p=float(os.getenv("AI_TOP_P", "0.95")),
        )


class AIService:
    """Generate assistant replies for @AI requests."""

    def __init__(self, config: AIConfig | None = None) -> None:
        self.config = config or AIConfig.from_env()
        self.assistant_name = self.config.assistant_name

    def answer(
        self,
        prompt: str,
        *,
        username: str | None = None,
        group_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> str:
        question = prompt.strip()
        image_attachments = [
            item
            for item in attachments or []
            if item.get("kind") == "image" and isinstance(item.get("data"), str)
        ]
        if not question and not image_attachments:
            return "请在 @AI 后面写下你想问的问题，或附上一张图片。"
        if not self.config.api_key:
            if image_attachments:
                return f"我已收到你的图片和问题：{question or '请分析这张图片'}"
            return f"我已收到你的问题：{question}"

        payload = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是群聊中的 AI 助手，回答要准确、简洁、友好。",
                },
                {"role": "user", "content": build_user_content(question, image_attachments)},
            ],
            "max_completion_tokens": self.config.max_completion_tokens,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "stream": False,
            "frequency_penalty": 0,
            "presence_penalty": 0,
        }
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self.config.base_url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
        )

        try:
            with request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise AIServiceError(f"AI API call failed: HTTP {exc.code}: {detail}") from exc
        except (OSError, TimeoutError, error.URLError, json.JSONDecodeError) as exc:
            raise AIServiceError(f"AI API call failed: {exc}") from exc

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIServiceError("AI API response missing choices[0].message.content") from exc

        text = str(content).strip()
        if not text:
            raise AIServiceError("AI API returned an empty response")
        return text


class AIServiceError(RuntimeError):
    """Raised when the upstream AI service cannot produce a reply."""


class AIRateLimiter:
    """Simple in-memory per-key cooldown limiter."""

    def __init__(self, cooldown_seconds: float = 3.0) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._last_seen: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        previous = self._last_seen.get(key)
        if previous is not None and now - previous < self.cooldown_seconds:
            return False
        self._last_seen[key] = now
        return True


def extract_ai_prompt(content: str) -> str | None:
    """Return the text after a leading @AI trigger, or None if not triggered."""

    stripped = content.strip()
    if not stripped.casefold().startswith(AI_TRIGGER.casefold()):
        return None
    return stripped[len(AI_TRIGGER) :].strip()


def normalize_chat_completions_url(base_url: str) -> str:
    """Accept either a /v1 base URL or the full chat completions endpoint."""

    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/chat/completions"):
        return trimmed
    return f"{trimmed}/chat/completions"


def build_user_content(prompt: str, image_attachments: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    """Build OpenAI-compatible text or multimodal user content."""

    question = prompt.strip() or "请分析这张图片。"
    if not image_attachments:
        return question
    content: list[dict[str, Any]] = [{"type": "text", "text": question}]
    for attachment in image_attachments:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": attachment["data"]},
            }
        )
    return content


def load_dotenv(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE lines into os.environ when they are not set."""

    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
