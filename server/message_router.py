"""Dispatch incoming `ProtocolMessage`s to the right business handler.

The router is the single seam between the framing/connection code in
`server.server` and the stateful managers (`UserManager`, `GroupManager`,
`ChatDatabase`). Every handler is a method on this class, looked up by
`MessageType`. Handlers raise `ProtocolError` to report business errors;
the dispatch loop converts those into wire-format error responses so the
individual handlers stay short.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from common.protocol import (
    ErrorCode,
    MessageType,
    ProtocolError,
    ProtocolMessage,
    make_error,
    make_message,
)
from server.database import ChatDatabase
from server.group_manager import GroupManager
from server.user_manager import ClientSession, UserManager


logger = logging.getLogger(__name__)


def _require(payload: dict[str, Any], field: str, request_id: str | None = None) -> Any:
    if field not in payload or payload[field] in (None, ""):
        raise ProtocolError(
            ErrorCode.MISSING_FIELD,
            f"missing required field: {field}",
            detail={"field": field},
            request_id=request_id,
        )
    return payload[field]


def _require_str(payload: dict[str, Any], field: str, request_id: str | None = None) -> str:
    value = _require(payload, field, request_id=request_id)
    if not isinstance(value, str):
        raise ProtocolError(
            ErrorCode.INVALID_FIELD,
            f"field {field} must be a string",
            detail={"field": field},
            request_id=request_id,
        )
    return value


class MessageRouter:
    def __init__(
        self,
        db: ChatDatabase,
        user_manager: UserManager,
        group_manager: GroupManager,
    ) -> None:
        self.db = db
        self.users = user_manager
        self.groups = group_manager
        self._handlers: dict[MessageType, Callable[[ClientSession, ProtocolMessage], None]] = {
            MessageType.REGISTER: self._handle_register,
            MessageType.LOGIN: self._handle_login,
            MessageType.LOGOUT: self._handle_logout,
            MessageType.HEARTBEAT: self._handle_heartbeat,
            MessageType.PRIVATE_MSG: self._handle_private_msg,
            MessageType.GROUP_MSG: self._handle_group_msg,
            MessageType.CREATE_GROUP: self._handle_create_group,
            MessageType.JOIN_GROUP: self._handle_join_group,
            MessageType.LEAVE_GROUP: self._handle_leave_group,
            MessageType.HISTORY_REQUEST: self._handle_history_request,
        }

    # --- public entrypoint -----------------------------------------------

    def dispatch(self, session: ClientSession, message: ProtocolMessage) -> None:
        self.users.update_heartbeat(session)
        handler = self._handlers.get(message.type)
        if handler is None:
            session.send(
                make_error(
                    ErrorCode.INVALID_MESSAGE_TYPE,
                    f"unsupported message type: {message.type.value}",
                    request_id=message.request_id,
                    detail={"type": message.type.value},
                )
            )
            return

        try:
            handler(session, message)
        except ProtocolError as exc:
            exc.request_id = exc.request_id or message.request_id
            session.send(exc.to_message())
        except Exception:
            logger.exception("unhandled error while processing %s", message.type)
            session.send(
                make_error(
                    ErrorCode.SERVER_ERROR,
                    "internal server error",
                    request_id=message.request_id,
                )
            )

    # --- auth handlers ---------------------------------------------------

    def _handle_register(self, session: ClientSession, message: ProtocolMessage) -> None:
        username = _require_str(message.payload, "username", message.request_id)
        password = _require_str(message.payload, "password", message.request_id)
        display_name = message.payload.get("display_name")
        user = self.users.register_user(username, password, display_name=display_name)
        session.send(
            make_message(
                MessageType.REGISTER,
                sender="server",
                receiver=username,
                payload={"username": user["username"], "display_name": user["display_name"]},
                request_id=message.request_id,
            )
        )

    def _handle_login(self, session: ClientSession, message: ProtocolMessage) -> None:
        username = _require_str(message.payload, "username", message.request_id)
        password = _require_str(message.payload, "password", message.request_id)

        def build_login_response(user: dict[str, Any]) -> ProtocolMessage:
            # Snapshot online users *plus* the one logging in now, so the
            # response carries a consistent view even though the session has
            # not yet been added to the broadcast index.
            roster = self.users.online_users()
            if user["username"] not in roster:
                roster.append(user["username"])
            return make_message(
                MessageType.LOGIN,
                sender="server",
                receiver=user["username"],
                payload={
                    "username": user["username"],
                    "display_name": user["display_name"],
                    "online_users": sorted(roster),
                },
                request_id=message.request_id,
            )

        user = self.users.login(
            session,
            username,
            password,
            pre_attach_send=build_login_response,
        )
        self._broadcast_status(user["username"], "online", exclude=session)

    def _handle_logout(self, session: ClientSession, message: ProtocolMessage) -> None:
        username = self.users.logout(session)
        session.send(
            make_message(
                MessageType.LOGOUT,
                sender="server",
                receiver=username,
                payload={"username": username},
                request_id=message.request_id,
            )
        )
        if username is not None:
            self._broadcast_status(username, "offline", exclude=session)

    def _handle_heartbeat(self, session: ClientSession, message: ProtocolMessage) -> None:
        # Touching last_seen is already done in dispatch(); echo the seq back
        # so the client can detect lost heartbeats.
        session.send(
            make_message(
                MessageType.HEARTBEAT,
                sender="server",
                receiver=session.username,
                payload={"seq": message.payload.get("seq")},
                request_id=message.request_id,
            )
        )

    # --- chat handlers ---------------------------------------------------

    def _handle_private_msg(self, session: ClientSession, message: ProtocolMessage) -> None:
        self._require_auth(session, message)
        receiver = message.receiver or _require_str(message.payload, "receiver", message.request_id)
        content = _require_str(message.payload, "content", message.request_id)

        if receiver == session.username:
            raise ProtocolError(
                ErrorCode.INVALID_FIELD,
                "cannot send a private message to yourself",
                request_id=message.request_id,
            )
        if self.db.get_user(receiver) is None:
            raise ProtocolError(
                ErrorCode.NOT_FOUND,
                f"user not found: {receiver}",
                detail={"receiver": receiver},
                request_id=message.request_id,
            )

        record = self.db.save_message(
            message_type=MessageType.PRIVATE_MSG.value,
            sender=session.username,
            receiver=receiver,
            content=content,
            payload={"content": content},
        )

        forward = make_message(
            MessageType.PRIVATE_MSG,
            sender=session.username,
            receiver=receiver,
            payload={
                "content": content,
                "message_id": record["message_id"],
                "created_at": record["created_at"],
            },
            request_id=message.request_id,
        )

        target = self.users.get_session(receiver)
        delivered = False
        if target is not None:
            delivered = target.send(forward)

        # echo back to the sender so its UI can show the confirmed message
        session.send(
            make_message(
                MessageType.PRIVATE_MSG,
                sender=session.username,
                receiver=receiver,
                payload={
                    "content": content,
                    "message_id": record["message_id"],
                    "created_at": record["created_at"],
                    "delivered": delivered,
                },
                request_id=message.request_id,
                meta={"echo": True},
            )
        )

    def _handle_group_msg(self, session: ClientSession, message: ProtocolMessage) -> None:
        self._require_auth(session, message)
        group_id = message.group_id or _require_str(message.payload, "group_id", message.request_id)
        content = _require_str(message.payload, "content", message.request_id)

        members = self.groups.member_usernames(group_id)
        if not members:
            raise ProtocolError(
                ErrorCode.NOT_FOUND,
                f"group not found: {group_id}",
                detail={"group_id": group_id},
                request_id=message.request_id,
            )
        if session.username not in members:
            raise ProtocolError(
                ErrorCode.AUTH_FAILED,
                f"{session.username} is not a member of {group_id}",
                detail={"group_id": group_id},
                request_id=message.request_id,
            )

        record = self.db.save_message(
            message_type=MessageType.GROUP_MSG.value,
            sender=session.username,
            group_id=group_id,
            content=content,
            payload={"content": content},
        )

        forward = make_message(
            MessageType.GROUP_MSG,
            sender=session.username,
            group_id=group_id,
            payload={
                "content": content,
                "message_id": record["message_id"],
                "created_at": record["created_at"],
            },
            request_id=message.request_id,
        )

        for member in members:
            target = self.users.get_session(member)
            if target is None:
                continue
            target.send(forward)

    # --- group handlers --------------------------------------------------

    def _handle_create_group(self, session: ClientSession, message: ProtocolMessage) -> None:
        self._require_auth(session, message)
        name = _require_str(message.payload, "name", message.request_id)
        group_id = message.payload.get("group_id")
        if group_id is not None and not isinstance(group_id, str):
            raise ProtocolError(
                ErrorCode.INVALID_FIELD,
                "group_id must be a string",
                request_id=message.request_id,
            )
        group = self.groups.create_group(name, session.username, group_id=group_id)
        session.send(
            make_message(
                MessageType.CREATE_GROUP,
                sender="server",
                receiver=session.username,
                group_id=group["group_id"],
                payload=group,
                request_id=message.request_id,
            )
        )

    def _handle_join_group(self, session: ClientSession, message: ProtocolMessage) -> None:
        self._require_auth(session, message)
        group_id = message.group_id or _require_str(message.payload, "group_id", message.request_id)
        result = self.groups.join_group(group_id, session.username)
        session.send(
            make_message(
                MessageType.JOIN_GROUP,
                sender="server",
                receiver=session.username,
                group_id=group_id,
                payload=result,
                request_id=message.request_id,
            )
        )
        # Tell currently-online members someone joined.
        notification = make_message(
            MessageType.JOIN_GROUP,
            sender="server",
            group_id=group_id,
            payload={"group_id": group_id, "username": session.username},
        )
        for member in self.groups.member_usernames(group_id):
            if member == session.username:
                continue
            target = self.users.get_session(member)
            if target is not None:
                target.send(notification)

    def _handle_leave_group(self, session: ClientSession, message: ProtocolMessage) -> None:
        self._require_auth(session, message)
        group_id = message.group_id or _require_str(message.payload, "group_id", message.request_id)
        # Snapshot members *before* removal so we can notify the leaver's peers.
        prior_members = self.groups.member_usernames(group_id)
        self.groups.leave_group(group_id, session.username)
        session.send(
            make_message(
                MessageType.LEAVE_GROUP,
                sender="server",
                receiver=session.username,
                group_id=group_id,
                payload={"group_id": group_id, "username": session.username},
                request_id=message.request_id,
            )
        )
        notification = make_message(
            MessageType.LEAVE_GROUP,
            sender="server",
            group_id=group_id,
            payload={"group_id": group_id, "username": session.username},
        )
        for member in prior_members:
            if member == session.username:
                continue
            target = self.users.get_session(member)
            if target is not None:
                target.send(notification)

    # --- history --------------------------------------------------------

    def _handle_history_request(self, session: ClientSession, message: ProtocolMessage) -> None:
        self._require_auth(session, message)
        chat_type = _require_str(message.payload, "chat_type", message.request_id)
        limit = message.payload.get("limit", 50)
        if not isinstance(limit, int) or limit <= 0 or limit > 500:
            raise ProtocolError(
                ErrorCode.INVALID_FIELD,
                "limit must be a positive integer <= 500",
                request_id=message.request_id,
            )

        if chat_type == "private":
            peer = _require_str(message.payload, "peer", message.request_id)
            messages = self.db.get_private_history(session.username, peer, limit=limit)
        elif chat_type == "group":
            group_id = _require_str(message.payload, "group_id", message.request_id)
            if not self.groups.is_member(group_id, session.username):
                raise ProtocolError(
                    ErrorCode.AUTH_FAILED,
                    f"{session.username} is not a member of {group_id}",
                    detail={"group_id": group_id},
                    request_id=message.request_id,
                )
            messages = self.db.get_group_history(group_id, limit=limit)
        else:
            raise ProtocolError(
                ErrorCode.INVALID_FIELD,
                "chat_type must be 'private' or 'group'",
                detail={"chat_type": chat_type},
                request_id=message.request_id,
            )

        session.send(
            make_message(
                MessageType.HISTORY_RESPONSE,
                sender="server",
                receiver=session.username,
                payload={"messages": messages, "chat_type": chat_type},
                request_id=message.request_id,
            )
        )

    # --- helpers ---------------------------------------------------------

    def _require_auth(self, session: ClientSession, message: ProtocolMessage) -> None:
        if not session.authenticated:
            raise ProtocolError(
                ErrorCode.AUTH_FAILED,
                "login required",
                request_id=message.request_id,
            )

    def _broadcast_status(
        self,
        username: str,
        status: str,
        exclude: ClientSession | None = None,
    ) -> None:
        notification = make_message(
            MessageType.USER_STATUS,
            sender="server",
            payload={"username": username, "status": status},
        )
        for peer in self.users.authenticated_sessions():
            if peer is exclude:
                continue
            peer.send(notification)
