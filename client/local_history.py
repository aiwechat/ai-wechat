"""In-memory local chat history helpers.

This module only keeps a local session view. The server remains the source of
truth for durable history through history_request/history_response.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Deque

from common.protocol import MessageType, ProtocolMessage


HistoryKey = tuple[str, str]


@dataclass(slots=True)
class LocalHistoryItem:
    chat_type: str
    target: str
    sender: str | None
    content: str
    timestamp: str | None = None
    raw: ProtocolMessage | dict[str, Any] | None = None


class LocalHistory:
    """Keep recent private and group messages for display in the CLI."""

    def __init__(self, limit_per_chat: int = 200) -> None:
        self.limit_per_chat = limit_per_chat
        self._items: dict[HistoryKey, Deque[LocalHistoryItem]] = {}

    def add_protocol_message(
        self,
        message: ProtocolMessage,
        *,
        current_user: str | None = None,
    ) -> LocalHistoryItem | None:
        if message.type == MessageType.PRIVATE_MSG:
            target = self._private_target(message, current_user)
            item = LocalHistoryItem(
                chat_type="private",
                target=target,
                sender=message.sender,
                content=self._content_from_payload(message.payload),
                timestamp=message.timestamp,
                raw=message,
            )
            self._append(("private", target), item)
            return item

        if message.type == MessageType.GROUP_MSG:
            target = str(message.group_id or message.payload.get("group_id") or "")
            item = LocalHistoryItem(
                chat_type="group",
                target=target,
                sender=message.sender,
                content=self._content_from_payload(message.payload),
                timestamp=message.timestamp,
                raw=message,
            )
            self._append(("group", target), item)
            return item

        return None

    def add_history_response(
        self,
        message: ProtocolMessage,
        *,
        current_user: str | None = None,
    ) -> list[LocalHistoryItem]:
        """Cache best-effort items from history_response payload.

        The exact server shape is still not fixed, so this accepts common field
        names and ignores rows that cannot be classified.
        """

        raw_messages = message.payload.get("messages", [])
        if not isinstance(raw_messages, list):
            return []

        items: list[LocalHistoryItem] = []
        for row in raw_messages:
            if not isinstance(row, dict):
                continue
            item = self._item_from_history_row(row, message, current_user)
            if item is None:
                continue
            self._append((item.chat_type, item.target), item)
            items.append(item)
        return items

    def recent_private(self, peer: str, limit: int | None = None) -> list[LocalHistoryItem]:
        return self._recent(("private", peer), limit)

    def recent_group(self, group_id: str, limit: int | None = None) -> list[LocalHistoryItem]:
        return self._recent(("group", group_id), limit)

    def format_item(self, item: LocalHistoryItem, *, current_user: str | None = None) -> str:
        sender = "me" if current_user and item.sender == current_user else item.sender or "unknown"
        prefix = f"[{item.timestamp}] " if item.timestamp else ""
        if item.chat_type == "group":
            return f"{prefix}[group:{item.target}] {sender}: {item.content}"
        return f"{prefix}[private:{item.target}] {sender}: {item.content}"

    def format_items(
        self,
        items: list[LocalHistoryItem],
        *,
        current_user: str | None = None,
    ) -> list[str]:
        return [self.format_item(item, current_user=current_user) for item in items]

    def _append(self, key: HistoryKey, item: LocalHistoryItem) -> None:
        if key not in self._items:
            self._items[key] = deque(maxlen=self.limit_per_chat)
        self._items[key].append(item)

    def _recent(self, key: HistoryKey, limit: int | None) -> list[LocalHistoryItem]:
        items = list(self._items.get(key, ()))
        if limit is None:
            return items
        return items[-limit:]

    def _private_target(self, message: ProtocolMessage, current_user: str | None) -> str:
        if current_user and message.sender == current_user and message.receiver:
            return message.receiver
        if current_user and message.receiver == current_user and message.sender:
            return message.sender
        return str(message.receiver or message.sender or "")

    def _item_from_history_row(
        self,
        row: dict[str, Any],
        response: ProtocolMessage,
        current_user: str | None,
    ) -> LocalHistoryItem | None:
        chat_type = str(row.get("chat_type") or row.get("message_type") or "")
        group_id = row.get("group_id") or response.group_id or response.payload.get("group_id")
        sender = row.get("sender") or row.get("sender_username")
        receiver = row.get("receiver") or row.get("receiver_username")

        if group_id or chat_type == "group":
            target = str(group_id or "")
            resolved_type = "group"
        else:
            target = str(row.get("peer") or (receiver if sender == current_user else sender) or "")
            resolved_type = "private"

        if not target:
            return None

        return LocalHistoryItem(
            chat_type=resolved_type,
            target=target,
            sender=str(sender) if sender is not None else None,
            content=self._content_from_history_row(row),
            timestamp=row.get("created_at") or row.get("timestamp"),
            raw=row,
        )

    def _content_from_history_row(self, row: dict[str, Any]) -> str:
        if "content" in row:
            return str(row["content"])
        payload = row.get("payload")
        if isinstance(payload, dict):
            return self._content_from_payload(payload)
        return ""

    def _content_from_payload(self, payload: dict[str, Any]) -> str:
        content = payload.get("content", "")
        return str(content)
