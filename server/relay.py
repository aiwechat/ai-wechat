"""Shared chat relay core used by TCP and browser gateways.

The relay owns the stateful parts of the chat system: database access, online
session registry, group manager, message router, and heartbeat sweeper. Gateway
servers should attach client sessions to one relay when those clients need to
see each other live.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from server.ai_service import AIResponder
from server.database import DEFAULT_DB_PATH, init_db
from server.group_manager import GroupManager
from server.heartbeat import HeartbeatMonitor
from server.message_router import MessageRouter
from server.moderation import ModerationService
from server.user_manager import UserManager


class ChatRelayService:
    """Shared runtime state for all chat gateways in one process."""

    def __init__(
        self,
        *,
        db_path: str | Path = DEFAULT_DB_PATH,
        heartbeat_timeout: float = 60.0,
        heartbeat_interval: float = 15.0,
        ai_service: AIResponder | None = None,
        moderation: ModerationService | None = None,
        ai_workers: int = 4,
        ai_cooldown_seconds: float = 3.0,
    ) -> None:
        self.db = init_db(db_path)
        self.users = UserManager(self.db)
        self.groups = GroupManager(self.db)
        self.router = MessageRouter(
            self.db,
            self.users,
            self.groups,
            ai_service=ai_service,
            moderation=moderation,
            ai_workers=ai_workers,
            ai_cooldown_seconds=ai_cooldown_seconds,
        )
        self.heartbeat = HeartbeatMonitor(
            self.users,
            timeout_seconds=heartbeat_timeout,
            interval_seconds=heartbeat_interval,
        )
        self._lock = threading.RLock()
        self._started = False
        self._stopped = False

    def start(self) -> None:
        with self._lock:
            if self._stopped:
                raise RuntimeError("relay service cannot be restarted after stop")
            if self._started:
                return
            self.heartbeat.start()
            self._started = True

    def stop(self, *, join_timeout: float = 5.0) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._started = False

        self.heartbeat.stop(join_timeout=join_timeout)
        self.router.shutdown()
        for session in self.users.all_sessions():
            try:
                self.users.remove_session(session)
            except Exception:
                # Shutdown should continue even if one socket misbehaves.
                pass

    def stats(self) -> dict[str, Any]:
        return {
            "online_users": self.users.online_users(),
            "total_sessions": len(self.users.all_sessions()),
        }


__all__ = ["ChatRelayService"]
