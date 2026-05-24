"""Group business logic, layered on top of `ChatDatabase`.

Routing code calls into this manager instead of touching SQL so that
business-rule errors (e.g. joining twice, leaving a group you aren't in)
can be normalised into `ProtocolError`s with sensible error codes.
"""

from __future__ import annotations

import logging
from typing import Any

from common.protocol import ErrorCode, ProtocolError
from server.database import ChatDatabase


logger = logging.getLogger(__name__)


class GroupManager:
    def __init__(self, db: ChatDatabase) -> None:
        self.db = db

    def create_group(
        self,
        name: str,
        owner_username: str,
        group_id: str | None = None,
    ) -> dict[str, Any]:
        if not name:
            raise ProtocolError(ErrorCode.INVALID_FIELD, "group name is required")
        try:
            return self.db.create_group(name, owner_username, group_id=group_id)
        except ValueError as exc:
            raise ProtocolError(
                ErrorCode.CONFLICT,
                f"cannot create group: {exc}",
                detail={"name": name, "group_id": group_id},
            ) from exc

    def join_group(self, group_id: str, username: str) -> dict[str, Any]:
        if not group_id:
            raise ProtocolError(ErrorCode.INVALID_FIELD, "group_id is required")
        try:
            return self.db.join_group(group_id, username)
        except ValueError as exc:
            # Could be already a member or could be a missing group / user.
            if self._group_exists(group_id):
                raise ProtocolError(
                    ErrorCode.CONFLICT,
                    f"already a member of group {group_id}",
                    detail={"group_id": group_id},
                ) from exc
            raise ProtocolError(
                ErrorCode.NOT_FOUND,
                f"group not found: {group_id}",
                detail={"group_id": group_id},
            ) from exc

    def leave_group(self, group_id: str, username: str) -> None:
        if not group_id:
            raise ProtocolError(ErrorCode.INVALID_FIELD, "group_id is required")
        if not self.is_member(group_id, username):
            raise ProtocolError(
                ErrorCode.NOT_FOUND,
                f"{username} is not a member of {group_id}",
                detail={"group_id": group_id},
            )
        self.db.leave_group(group_id, username)

    def list_members(self, group_id: str) -> list[dict[str, Any]]:
        if not group_id:
            raise ProtocolError(ErrorCode.INVALID_FIELD, "group_id is required")
        if not self._group_exists(group_id):
            raise ProtocolError(
                ErrorCode.NOT_FOUND,
                f"group not found: {group_id}",
                detail={"group_id": group_id},
            )
        return self.db.list_group_members(group_id)

    def member_usernames(self, group_id: str) -> list[str]:
        return [member["username"] for member in self.db.list_group_members(group_id)]

    def is_member(self, group_id: str, username: str) -> bool:
        return any(
            member["username"] == username
            for member in self.db.list_group_members(group_id)
        )

    def _group_exists(self, group_id: str) -> bool:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM groups WHERE group_id = ?",
                (group_id,),
            ).fetchone()
        return row is not None
