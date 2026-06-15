"""Content moderation helpers.

Messages are allowed to reach the chat first. The server then calls an
OpenAI-compatible API to decide whether the message should stay visible or be
force-recalled. A deterministic keyword fallback keeps local development and
tests usable when no API key is configured.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from urllib import error, request

from server.ai_service import load_dotenv, normalize_chat_completions_url


DEFAULT_BAD_WORDS = [
    "违规词1",
    "违规词2",
    "攻击性词汇",
    "傻逼",
    "去死",
    "kill yourself",
    "terrorist attack",
]
DEFAULT_MODERATION_MODEL = "gpt-4o-mini"
DEFAULT_MIMO_MODEL = "mimo-v2.5-pro"
DEFAULT_MODERATION_TIMEOUT_SECONDS = 10.0


MODERATION_SYSTEM_PROMPT = """你是聊天应用的安全审查器。请判断用户发言是否不合理或不安全。
需要强制撤回的内容包括：辱骂骚扰、仇恨歧视、性剥削、暴力威胁、自残鼓励、违法犯罪协助、恐怖主义、隐私泄露、诈骗、恶意软件、明显色情或其他不适合公开聊天的内容。
只输出 JSON，不要输出 Markdown。格式：
{"allowed":true,"reason":"ok","categories":[]}
或
{"allowed":false,"reason":"简短中文原因","categories":["类别"]}"""


@dataclass(frozen=True, slots=True)
class ModerationResult:
    allowed: bool
    action: str = "allow"
    reason: str = ""
    matched_words: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModerationConfig:
    api_key: str | None = None
    base_url: str = "https://api.openai.com/v1/chat/completions"
    model: str = DEFAULT_MODERATION_MODEL
    timeout_seconds: float = DEFAULT_MODERATION_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> "ModerationConfig":
        load_dotenv()
        mimo_api_key = os.getenv("MIMO_API_KEY")
        api_key = (
            os.getenv("MODERATION_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("AI_API_KEY")
            or mimo_api_key
        )
        raw_base_url = (
            os.getenv("MODERATION_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or os.getenv("AI_BASE_URL")
            or os.getenv("MIMO_BASE_URL")
            or ("https://api.xiaomimimo.com/v1" if mimo_api_key else "https://api.openai.com/v1")
        )
        return cls(
            api_key=api_key,
            base_url=normalize_chat_completions_url(raw_base_url),
            model=(
                os.getenv("MODERATION_MODEL")
                or os.getenv("OPENAI_MODEL")
                or os.getenv("AI_MODEL")
                or os.getenv("MIMO_MODEL")
                or (DEFAULT_MIMO_MODEL if mimo_api_key else DEFAULT_MODERATION_MODEL)
            ),
            timeout_seconds=float(os.getenv("MODERATION_TIMEOUT_SECONDS", str(DEFAULT_MODERATION_TIMEOUT_SECONDS))),
        )


class ModerationService:
    """API-first moderation used by the server router."""

    def __init__(
        self,
        bad_words: list[str] | tuple[str, ...] | None = None,
        *,
        config: ModerationConfig | None = None,
    ) -> None:
        self.config = config or ModerationConfig.from_env()
        configured_words = _words_from_env()
        words = bad_words if bad_words is not None else configured_words or DEFAULT_BAD_WORDS
        self.bad_words = tuple(word.strip() for word in words if word and word.strip())

    def check(self, content: str) -> ModerationResult:
        if self.config.api_key:
            try:
                return self._check_with_api(content)
            except ModerationServiceError:
                return self._check_keywords(content, reason_prefix="moderation API unavailable; ")
        return self._check_keywords(content)

    def _check_keywords(self, content: str, *, reason_prefix: str = "") -> ModerationResult:
        normalized = content.casefold()
        matched = tuple(word for word in self.bad_words if word.casefold() in normalized)
        if not matched:
            return ModerationResult(allowed=True)
        return ModerationResult(
            allowed=False,
            action="recall",
            reason=f"{reason_prefix}message contains blocked keywords",
            matched_words=matched,
            categories=("keyword",),
        )

    def _check_with_api(self, content: str) -> ModerationResult:
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": MODERATION_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "temperature": 0,
            "top_p": 1,
            "stream": False,
            "max_completion_tokens": 200,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
            raw_content = str(data["choices"][0]["message"]["content"]).strip()
            verdict = _parse_json_object(raw_content)
        except (OSError, TimeoutError, error.URLError, error.HTTPError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ModerationServiceError(f"moderation API call failed: {exc}") from exc

        allowed = bool(verdict.get("allowed", True))
        reason = str(verdict.get("reason") or ("ok" if allowed else "内容不符合聊天安全规范"))
        categories = verdict.get("categories", [])
        if not isinstance(categories, list):
            categories = [str(categories)]
        if allowed:
            return ModerationResult(allowed=True, reason=reason)
        return ModerationResult(
            allowed=False,
            action="recall",
            reason=reason,
            categories=tuple(str(item) for item in categories if item),
        )


class ModerationServiceError(RuntimeError):
    """Raised when upstream moderation cannot produce a usable verdict."""


def _words_from_env() -> list[str]:
    raw = os.getenv("AI_WECHAT_BAD_WORDS", "")
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_json_object(raw: str) -> dict[str, object]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.casefold().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start : end + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("moderation verdict is not an object", text, 0)
    return parsed
