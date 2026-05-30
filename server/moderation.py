"""Basic content moderation helpers.

The first implementation deliberately stays local and deterministic: a small
keyword filter catches clearly abusive or unsafe terms before a message is
persisted or broadcast. The list can be extended through an environment
variable without changing code.
"""

from __future__ import annotations

from dataclasses import dataclass
import os


DEFAULT_BAD_WORDS = [
    "违规词1",
    "违规词2",
    "攻击性词汇",
    "傻逼",
    "去死",
    "kill yourself",
    "terrorist attack",
]


@dataclass(frozen=True, slots=True)
class ModerationResult:
    allowed: bool
    action: str = "allow"
    reason: str = ""
    matched_words: tuple[str, ...] = ()


class ModerationService:
    """Keyword-based moderation used by the server router."""

    def __init__(self, bad_words: list[str] | tuple[str, ...] | None = None) -> None:
        configured_words = _words_from_env()
        words = bad_words if bad_words is not None else configured_words or DEFAULT_BAD_WORDS
        self.bad_words = tuple(word.strip() for word in words if word and word.strip())

    def check(self, content: str) -> ModerationResult:
        normalized = content.casefold()
        matched = tuple(word for word in self.bad_words if word.casefold() in normalized)
        if not matched:
            return ModerationResult(allowed=True)
        return ModerationResult(
            allowed=False,
            action="block",
            reason="message contains blocked keywords",
            matched_words=matched,
        )


def _words_from_env() -> list[str]:
    raw = os.getenv("AI_WECHAT_BAD_WORDS", "")
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]
