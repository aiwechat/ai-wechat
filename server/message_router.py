"""Dispatch incoming `ProtocolMessage`s to the right business handler.

The router is the single seam between the framing/connection code in
`server.server` and the stateful managers (`UserManager`, `GroupManager`,
`ChatDatabase`). Every handler is a method on this class, looked up by
`MessageType`. Handlers raise `ProtocolError` to report business errors;
the dispatch loop converts those into wire-format error responses so the
individual handlers stay short.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
from pathlib import Path
import secrets
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from common.protocol import (
    ErrorCode,
    MessageType,
    ProtocolError,
    ProtocolMessage,
    make_error,
    make_message,
)
from server.ai_service import AIResponder, AIRateLimiter, AIService, AIServiceError, extract_ai_prompt
from server.database import ChatDatabase
from server.group_manager import GroupManager
from server.moderation import ModerationService
from server.user_manager import ClientSession, UserManager


logger = logging.getLogger(__name__)
MAX_ATTACHMENT_DATA_CHARS = 7_500_000
ALLOWED_ATTACHMENT_KINDS = {"image", "audio"}
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024
MAX_FILE_CHUNK_BYTES = 64 * 1024


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


def _require_int(payload: dict[str, Any], field: str, request_id: str | None = None) -> int:
    value = _require(payload, field, request_id=request_id)
    if not isinstance(value, int):
        raise ProtocolError(
            ErrorCode.INVALID_FIELD,
            f"field {field} must be an integer",
            detail={"field": field},
            request_id=request_id,
        )
    return value


def _optional_str(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProtocolError(
            ErrorCode.INVALID_FIELD,
            f"field {field} must be a string",
            detail={"field": field},
        )
    return value


def _message_body(payload: dict[str, Any], request_id: str) -> tuple[str, dict[str, Any] | None]:
    content = _optional_str(payload, "content").strip()
    attachment = payload.get("attachment")
    if attachment is not None:
        attachment = _validate_attachment(attachment, request_id)
    if not content and attachment is None:
        raise ProtocolError(
            ErrorCode.MISSING_FIELD,
            "message must include content or attachment",
            detail={"required": ["content", "attachment"]},
            request_id=request_id,
        )
    return content, attachment


def _validate_attachment(raw: Any, request_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProtocolError(
            ErrorCode.INVALID_FIELD,
            "attachment must be an object",
            detail={"field": "attachment"},
            request_id=request_id,
        )
    kind = raw.get("kind")
    mime = raw.get("mime")
    name = raw.get("name", "")
    data = raw.get("data")
    size = raw.get("size", 0)
    if kind not in ALLOWED_ATTACHMENT_KINDS:
        raise ProtocolError(
            ErrorCode.INVALID_FIELD,
            "attachment kind must be image or audio",
            detail={"kind": kind},
            request_id=request_id,
        )
    if not isinstance(mime, str) or not mime.startswith(f"{kind}/"):
        raise ProtocolError(
            ErrorCode.INVALID_FIELD,
            "attachment mime does not match kind",
            detail={"kind": kind, "mime": mime},
            request_id=request_id,
        )
    if not isinstance(data, str) or not data:
        raise ProtocolError(
            ErrorCode.INVALID_FIELD,
            "attachment data must be a base64 data URL",
            detail={"field": "attachment.data"},
            request_id=request_id,
        )
    if len(data) > MAX_ATTACHMENT_DATA_CHARS:
        raise ProtocolError(
            ErrorCode.INVALID_FIELD,
            "attachment is too large",
            detail={"max_data_chars": MAX_ATTACHMENT_DATA_CHARS},
            request_id=request_id,
        )
    if not isinstance(name, str):
        raise ProtocolError(
            ErrorCode.INVALID_FIELD,
            "attachment name must be a string",
            request_id=request_id,
        )
    if not isinstance(size, int) or size < 0:
        raise ProtocolError(
            ErrorCode.INVALID_FIELD,
            "attachment size must be a non-negative integer",
            request_id=request_id,
        )
    return {
        "kind": kind,
        "mime": mime,
        "name": name,
        "size": size,
        "data": data,
    }


class MessageRouter:
    def __init__(
        self,
        db: ChatDatabase,
        user_manager: UserManager,
        group_manager: GroupManager,
        *,
        ai_service: AIResponder | None = None,
        moderation: ModerationService | None = None,
        ai_workers: int = 4,
        ai_cooldown_seconds: float = 3.0,
        upload_dir: str | Path | None = None,
    ) -> None:
        self.db = db
        self.users = user_manager
        self.groups = group_manager
        default_upload_dir = Path("data") / "uploads"
        if db.db_path != Path(":memory:"):
            default_upload_dir = db.db_path.parent / "uploads"
        self.upload_dir = Path(upload_dir) if upload_dir is not None else default_upload_dir
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.ai_service = ai_service or AIService()
        self.moderation = moderation or ModerationService()
        self.ai_rate_limiter = AIRateLimiter(cooldown_seconds=ai_cooldown_seconds)
        self._ai_executor = ThreadPoolExecutor(max_workers=ai_workers, thread_name_prefix="AIReply")
        self._ensure_assistant_user()
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
            MessageType.FILE_START: self._handle_file_start,
            MessageType.FILE_CHUNK: self._handle_file_chunk,
            MessageType.FILE_END: self._handle_file_end,
            MessageType.MESSAGE_RECALL: self._handle_message_recall,
        }

    def shutdown(self) -> None:
        self._ai_executor.shutdown(wait=False, cancel_futures=True)

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
                    "groups": self.db.list_user_groups(user["username"]),
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
        content, attachment = _message_body(message.payload, message.request_id)
        if content and not self._moderate_or_warn(session, content, message):
            return

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
            payload=_chat_payload(content, attachment),
        )

        payload = _chat_payload(content, attachment)
        payload.update(
            {
                "message_id": record["message_id"],
                "created_at": record["created_at"],
            }
        )
        forward = make_message(
            MessageType.PRIVATE_MSG,
            sender=session.username,
            receiver=receiver,
            payload=payload,
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
                payload={**payload, "delivered": delivered},
                request_id=message.request_id,
                meta={"echo": True},
            )
        )

    def _handle_group_msg(self, session: ClientSession, message: ProtocolMessage) -> None:
        self._require_auth(session, message)
        group_id = message.group_id or _require_str(message.payload, "group_id", message.request_id)
        content, attachment = _message_body(message.payload, message.request_id)
        if content and not self._moderate_or_warn(session, content, message, group_id=group_id):
            return

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
            payload=_chat_payload(content, attachment),
        )

        payload = _chat_payload(content, attachment)
        payload.update(
            {
                "message_id": record["message_id"],
                "created_at": record["created_at"],
            }
        )
        forward = make_message(
            MessageType.GROUP_MSG,
            sender=session.username,
            group_id=group_id,
            payload=payload,
            request_id=message.request_id,
        )

        for member in members:
            target = self.users.get_session(member)
            if target is None:
                continue
            target.send(forward)

        ai_prompt = extract_ai_prompt(content) if content else None
        if ai_prompt is not None:
            self._schedule_ai_reply(
                requester=session.username or "",
                group_id=group_id,
                prompt=ai_prompt,
                attachments=[attachment] if attachment and attachment.get("kind") == "image" else None,
                request_id=message.request_id,
            )

    # --- file transfer ----------------------------------------------------

    def _handle_file_start(self, session: ClientSession, message: ProtocolMessage) -> None:
        self._require_auth(session, message)
        filename = Path(_require_str(message.payload, "filename", message.request_id)).name
        filesize = _require_int(message.payload, "filesize", message.request_id)
        mime = _optional_str(message.payload, "mime") or "application/octet-stream"
        expected_sha = _optional_str(message.payload, "sha256")
        receiver = message.receiver or message.payload.get("receiver")
        group_id = message.group_id or message.payload.get("group_id")

        if not filename:
            raise ProtocolError(ErrorCode.INVALID_FIELD, "filename must not be empty", request_id=message.request_id)
        if filesize < 0 or filesize > MAX_FILE_SIZE_BYTES:
            raise ProtocolError(
                ErrorCode.INVALID_FIELD,
                "filesize is out of range",
                detail={"max_file_size": MAX_FILE_SIZE_BYTES},
                request_id=message.request_id,
            )
        if not isinstance(mime, str) or not mime:
            raise ProtocolError(ErrorCode.INVALID_FIELD, "mime must be a string", request_id=message.request_id)
        if expected_sha and (len(expected_sha) != 64 or any(c not in "0123456789abcdefABCDEF" for c in expected_sha)):
            raise ProtocolError(ErrorCode.INVALID_FIELD, "sha256 must be a hex digest", request_id=message.request_id)

        receiver_name: str | None = None
        group_name: str | None = None
        if receiver and group_id:
            raise ProtocolError(
                ErrorCode.INVALID_FIELD,
                "file transfer must target either receiver or group_id",
                request_id=message.request_id,
            )
        if receiver:
            if not isinstance(receiver, str):
                raise ProtocolError(ErrorCode.INVALID_FIELD, "receiver must be a string", request_id=message.request_id)
            self._validate_private_receiver(session, receiver, message.request_id)
            receiver_name = receiver
        elif group_id:
            if not isinstance(group_id, str):
                raise ProtocolError(ErrorCode.INVALID_FIELD, "group_id must be a string", request_id=message.request_id)
            self._validate_group_member(session, group_id, message.request_id)
            group_name = group_id
        else:
            raise ProtocolError(
                ErrorCode.MISSING_FIELD,
                "file transfer requires receiver or group_id",
                detail={"required": ["receiver", "group_id"]},
                request_id=message.request_id,
            )

        requested_file_id = message.payload.get("file_id")
        if requested_file_id is not None:
            if not isinstance(requested_file_id, str) or not requested_file_id.replace("-", "").replace("_", "").isalnum():
                raise ProtocolError(ErrorCode.INVALID_FIELD, "file_id must be alphanumeric", request_id=message.request_id)
            file_id = requested_file_id[:80]
        else:
            file_id = secrets.token_hex(16)
        storage_path = self.upload_dir / file_id
        transfer = self.db.create_file_transfer(
            file_id=file_id,
            sender=session.username or "",
            receiver=receiver_name,
            group_id=group_name,
            filename=filename,
            filesize=filesize,
            mime=mime,
            sha256=expected_sha or None,
            storage_path=str(storage_path),
        )
        part_path = self._part_path(transfer)
        if part_path.exists():
            part_path.unlink()
        part_path.touch()

        session.send(
            make_message(
                MessageType.FILE_START,
                sender="server",
                receiver=session.username,
                group_id=group_name,
                payload=self._file_transfer_payload(transfer),
                request_id=message.request_id,
            )
        )

    def _handle_file_chunk(self, session: ClientSession, message: ProtocolMessage) -> None:
        self._require_auth(session, message)
        file_id = _require_str(message.payload, "file_id", message.request_id)
        offset = _require_int(message.payload, "offset", message.request_id)
        data = _require_str(message.payload, "data", message.request_id)
        transfer = self._require_owned_transfer(session, file_id, message.request_id)
        if transfer["status"] not in {"started", "transferring"}:
            raise ProtocolError(
                ErrorCode.INVALID_FIELD,
                "file transfer is not accepting chunks",
                detail={"file_id": file_id, "status": transfer["status"]},
                request_id=message.request_id,
            )
        if offset != transfer["offset"]:
            raise ProtocolError(
                ErrorCode.INVALID_FIELD,
                "file chunk offset does not match transfer offset",
                detail={"expected": transfer["offset"], "actual": offset},
                request_id=message.request_id,
            )
        try:
            chunk = base64.b64decode(data.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as exc:
            raise ProtocolError(ErrorCode.INVALID_FIELD, "file chunk data must be base64", request_id=message.request_id) from exc
        if not chunk:
            raise ProtocolError(ErrorCode.INVALID_FIELD, "file chunk must not be empty", request_id=message.request_id)
        if len(chunk) > MAX_FILE_CHUNK_BYTES:
            raise ProtocolError(
                ErrorCode.INVALID_FIELD,
                "file chunk is too large",
                detail={"max_chunk_size": MAX_FILE_CHUNK_BYTES},
                request_id=message.request_id,
            )
        new_offset = offset + len(chunk)
        if new_offset > transfer["filesize"]:
            raise ProtocolError(ErrorCode.INVALID_FIELD, "file chunk exceeds declared filesize", request_id=message.request_id)

        part_path = self._part_path(transfer)
        part_path.parent.mkdir(parents=True, exist_ok=True)
        with part_path.open("ab") as fh:
            fh.write(chunk)
        transfer = self.db.update_file_transfer(file_id, status="transferring", offset=new_offset)
        session.send(
            make_message(
                MessageType.FILE_CHUNK,
                sender="server",
                receiver=session.username,
                group_id=transfer.get("group_id"),
                payload={"file_id": file_id, "offset": new_offset, "filesize": transfer["filesize"]},
                request_id=message.request_id,
            )
        )

    def _handle_file_end(self, session: ClientSession, message: ProtocolMessage) -> None:
        self._require_auth(session, message)
        file_id = _require_str(message.payload, "file_id", message.request_id)
        provided_sha = _optional_str(message.payload, "sha256")
        transfer = self._require_owned_transfer(session, file_id, message.request_id)
        if transfer["status"] not in {"started", "transferring"}:
            raise ProtocolError(
                ErrorCode.INVALID_FIELD,
                "file transfer cannot be finished",
                detail={"file_id": file_id, "status": transfer["status"]},
                request_id=message.request_id,
            )
        if transfer["offset"] != transfer["filesize"]:
            raise ProtocolError(
                ErrorCode.INVALID_FIELD,
                "file transfer is incomplete",
                detail={"offset": transfer["offset"], "filesize": transfer["filesize"]},
                request_id=message.request_id,
            )

        part_path = self._part_path(transfer)
        final_path = Path(str(transfer["storage_path"]))
        try:
            digest = self._sha256_file(part_path)
            expected_sha = provided_sha or transfer.get("sha256")
            if expected_sha and digest.lower() != str(expected_sha).lower():
                self.db.update_file_transfer(file_id, status="failed", error_reason="sha256 mismatch")
                raise ProtocolError(ErrorCode.INVALID_FIELD, "file sha256 mismatch", request_id=message.request_id)
            part_path.replace(final_path)
        except ProtocolError:
            raise
        except OSError as exc:
            self.db.update_file_transfer(file_id, status="failed", error_reason=str(exc))
            raise ProtocolError(ErrorCode.SERVER_ERROR, "could not finalize file transfer", request_id=message.request_id) from exc

        transfer = self.db.update_file_transfer(file_id, status="finished", sha256=digest, storage_path=str(final_path))
        file_payload = self._file_message_payload(transfer)
        record = self.db.save_message(
            message_type=MessageType.GROUP_MSG.value if transfer.get("group_id") else MessageType.PRIVATE_MSG.value,
            sender=session.username,
            receiver=transfer.get("receiver"),
            group_id=transfer.get("group_id"),
            content="",
            payload={"content": "", "file": file_payload},
        )
        transfer = self.db.update_file_transfer(file_id, status="finished", message_id=record["message_id"])
        payload = {"content": "", "file": file_payload, "message_id": record["message_id"], "created_at": record["created_at"]}
        self._deliver_chat_payload(
            session,
            payload,
            request_id=message.request_id,
            receiver=transfer.get("receiver"),
            group_id=transfer.get("group_id"),
        )
        session.send(
            make_message(
                MessageType.FILE_END,
                sender="server",
                receiver=session.username,
                group_id=transfer.get("group_id"),
                payload={**self._file_transfer_payload(transfer), "sha256": digest},
                request_id=message.request_id,
            )
        )

    def _handle_message_recall(self, session: ClientSession, message: ProtocolMessage) -> None:
        self._require_auth(session, message)
        message_id = _require_str(message.payload, "message_id", message.request_id)
        record = self.db.get_message(message_id)
        if record is None:
            raise ProtocolError(
                ErrorCode.NOT_FOUND,
                f"message not found: {message_id}",
                detail={"message_id": message_id},
                request_id=message.request_id,
            )
        if record.get("sender") != session.username:
            raise ProtocolError(
                ErrorCode.AUTH_FAILED,
                "only the sender can recall a message",
                detail={"message_id": message_id},
                request_id=message.request_id,
            )
        recalled = self.db.recall_message(message_id, session.username or "")
        payload = {
            "message_id": message_id,
            "recalled": True,
            "recalled_at": recalled.get("recalled_at"),
            "recalled_by": session.username,
        }
        notification = make_message(
            MessageType.MESSAGE_RECALL,
            sender="server",
            receiver=record.get("receiver"),
            group_id=record.get("group_id"),
            payload=payload,
            request_id=message.request_id,
        )
        if record.get("group_id"):
            for member in self.groups.member_usernames(str(record["group_id"])):
                target = self.users.get_session(member)
                if target is not None:
                    target.send(notification)
        else:
            recipients = {record.get("sender"), record.get("receiver")}
            for username in recipients:
                if not username:
                    continue
                target = self.users.get_session(str(username))
                if target is not None:
                    target.send(notification)

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

    def _validate_private_receiver(self, session: ClientSession, receiver: str, request_id: str) -> None:
        if receiver == session.username:
            raise ProtocolError(ErrorCode.INVALID_FIELD, "cannot send to yourself", request_id=request_id)
        if self.db.get_user(receiver) is None:
            raise ProtocolError(
                ErrorCode.NOT_FOUND,
                f"user not found: {receiver}",
                detail={"receiver": receiver},
                request_id=request_id,
            )

    def _validate_group_member(self, session: ClientSession, group_id: str, request_id: str) -> list[str]:
        members = self.groups.member_usernames(group_id)
        if not members:
            raise ProtocolError(
                ErrorCode.NOT_FOUND,
                f"group not found: {group_id}",
                detail={"group_id": group_id},
                request_id=request_id,
            )
        if session.username not in members:
            raise ProtocolError(
                ErrorCode.AUTH_FAILED,
                f"{session.username} is not a member of {group_id}",
                detail={"group_id": group_id},
                request_id=request_id,
            )
        return members

    def _require_owned_transfer(self, session: ClientSession, file_id: str, request_id: str) -> dict[str, Any]:
        transfer = self.db.get_file_transfer(file_id)
        if transfer is None:
            raise ProtocolError(
                ErrorCode.NOT_FOUND,
                f"file transfer not found: {file_id}",
                detail={"file_id": file_id},
                request_id=request_id,
            )
        if transfer.get("sender") != session.username:
            raise ProtocolError(
                ErrorCode.AUTH_FAILED,
                "only the sender can update this file transfer",
                detail={"file_id": file_id},
                request_id=request_id,
            )
        return transfer

    def _part_path(self, transfer: dict[str, Any]) -> Path:
        return Path(str(transfer["storage_path"]) + ".part")

    def _sha256_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _file_transfer_payload(self, transfer: dict[str, Any]) -> dict[str, Any]:
        return {
            "file_id": transfer["file_id"],
            "filename": transfer["filename"],
            "filesize": transfer["filesize"],
            "mime": transfer.get("mime") or "application/octet-stream",
            "status": transfer["status"],
            "offset": transfer["offset"],
        }

    def _file_message_payload(self, transfer: dict[str, Any]) -> dict[str, Any]:
        file_id = str(transfer["file_id"])
        token = str(transfer["download_token"])
        return {
            "file_id": file_id,
            "filename": transfer["filename"],
            "filesize": transfer["filesize"],
            "mime": transfer.get("mime") or "application/octet-stream",
            "sha256": transfer.get("sha256"),
            "download_url": f"/files/{file_id}?token={token}",
        }

    def _deliver_chat_payload(
        self,
        session: ClientSession,
        payload: dict[str, Any],
        *,
        request_id: str,
        receiver: str | None = None,
        group_id: str | None = None,
    ) -> None:
        if group_id:
            forward = make_message(
                MessageType.GROUP_MSG,
                sender=session.username,
                group_id=group_id,
                payload=payload,
                request_id=request_id,
            )
            for member in self.groups.member_usernames(group_id):
                target = self.users.get_session(member)
                if target is not None:
                    target.send(forward)
            return

        forward = make_message(
            MessageType.PRIVATE_MSG,
            sender=session.username,
            receiver=receiver,
            payload=payload,
            request_id=request_id,
        )
        delivered = False
        if receiver:
            target = self.users.get_session(receiver)
            if target is not None:
                delivered = target.send(forward)
        session.send(
            make_message(
                MessageType.PRIVATE_MSG,
                sender=session.username,
                receiver=receiver,
                payload={**payload, "delivered": delivered},
                request_id=request_id,
                meta={"echo": True},
            )
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

    def _moderate_or_warn(
        self,
        session: ClientSession,
        content: str,
        message: ProtocolMessage,
        *,
        group_id: str | None = None,
    ) -> bool:
        result = self.moderation.check(content)
        if result.allowed:
            return True

        session.send(
            make_message(
                MessageType.MODERATION_WARNING,
                sender="server",
                receiver=session.username,
                group_id=group_id,
                payload={
                    "action": result.action,
                    "reason": result.reason,
                    "message": "消息包含违规内容，已被拦截。",
                    "matched_words": list(result.matched_words),
                },
                request_id=message.request_id,
            )
        )
        return False

    def _schedule_ai_reply(
        self,
        *,
        requester: str,
        group_id: str,
        prompt: str,
        request_id: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        key = f"{group_id}:{requester}"
        if not self.ai_rate_limiter.allow(key):
            session = self.users.get_session(requester)
            if session is not None:
                session.send(
                    make_message(
                        MessageType.MODERATION_WARNING,
                        sender="server",
                        receiver=requester,
                        group_id=group_id,
                        payload={
                            "action": "rate_limited",
                            "reason": "too many AI requests",
                            "message": "AI 请求过于频繁，请稍后再试。",
                        },
                        request_id=request_id,
                    )
                )
            return

        future = self._ai_executor.submit(self._build_ai_reply, requester, group_id, prompt, attachments)
        future.add_done_callback(lambda done: self._send_ai_reply(done, group_id, request_id))

    def _build_ai_reply(
        self,
        requester: str,
        group_id: str,
        prompt: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> str:
        try:
            return self.ai_service.answer(
                prompt,
                username=requester,
                group_id=group_id,
                attachments=attachments,
            )
        except AIServiceError as exc:
            logger.warning("AI reply failed for group %s: %s", group_id, exc)
            if attachments and "support image input" in str(exc):
                return "当前 AI 接口不支持图片输入。可以先发送文字问题，或更换支持视觉输入的模型/API。"
            return "AI 服务暂时不可用，请稍后再试。"
        except Exception:
            logger.exception("unexpected AI reply error for group %s", group_id)
            return "AI 服务暂时不可用，请稍后再试。"

    def _send_ai_reply(self, future, group_id: str, request_id: str) -> None:  # noqa: ANN001 - Future callback
        try:
            content = future.result()
        except Exception:
            logger.exception("AI future failed before producing a fallback")
            content = "AI 服务暂时不可用，请稍后再试。"

        record = self.db.save_message(
            message_type=MessageType.AI_RESPONSE.value,
            sender=self.ai_service.assistant_name,
            group_id=group_id,
            content=content,
            payload={"content": content, "assistant": self.ai_service.assistant_name},
        )
        response = make_message(
            MessageType.GROUP_MSG,
            sender=self.ai_service.assistant_name,
            group_id=group_id,
            payload={
                "content": content,
                "message_id": record["message_id"],
                "created_at": record["created_at"],
                "ai": True,
            },
            request_id=request_id,
            meta={"ai_response": True},
        )
        for member in self.groups.member_usernames(group_id):
            target = self.users.get_session(member)
            if target is not None:
                target.send(response)

    def _ensure_assistant_user(self) -> None:
        assistant_name = self.ai_service.assistant_name
        if self.db.get_user(assistant_name) is not None:
            return
        try:
            self.db.create_user(assistant_name, secrets.token_urlsafe(32), display_name=assistant_name)
        except ValueError:
            pass


def _chat_payload(content: str, attachment: dict[str, Any] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"content": content}
    if attachment is not None:
        payload["attachment"] = attachment
    return payload
