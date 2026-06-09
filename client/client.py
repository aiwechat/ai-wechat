"""CLI chat client foundation.

Run from the project root with:

    python -m client.client --host 127.0.0.1 --port 9000
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field
import hashlib
import mimetypes
from pathlib import Path
import secrets
import socket
from typing import Any

from common.protocol import MessageType, ProtocolMessage, encode_frame, make_message

from .local_history import LocalHistory
from .receiver import Receiver


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9000
SOCKET_TIMEOUT_SECONDS = 1.0
FILE_CHUNK_BYTES = 64 * 1024


@dataclass(slots=True)
class ClientState:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    username: str | None = None
    login_confirmed: bool = False
    connected: bool = False
    current_chat_type: str | None = None
    current_target: str | None = None
    groups: set[str] = field(default_factory=set)
    user_status: dict[str, str] = field(default_factory=dict)


class ChatClient:
    """Manage socket connection, outgoing requests, and incoming message effects."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        self.state = ClientState(host=host, port=port)
        self.history = LocalHistory()
        self.sock: socket.socket | None = None
        self.receiver: Receiver | None = None
        self._pending_login: dict[str, str] = {}
        self._heartbeat_seq = 0

    def connect(self, host: str | None = None, port: int | None = None) -> None:
        if self.state.connected:
            return

        if host is not None:
            self.state.host = host
        if port is not None:
            self.state.port = port

        sock = socket.create_connection((self.state.host, self.state.port))
        sock.settimeout(SOCKET_TIMEOUT_SECONDS)
        self.sock = sock
        self.state.connected = True
        self.state.user_status.clear()
        self.receiver = Receiver(
            sock,
            self.handle_message,
            on_disconnect=self.handle_disconnect,
            on_error=self.handle_receiver_error,
        )
        self.receiver.start()
        print(f"connected to {self.state.host}:{self.state.port}")

    def disconnect(self) -> None:
        self.state.connected = False
        self.state.login_confirmed = False
        self.state.user_status.clear()
        receiver = self.receiver
        self.receiver = None
        if receiver is not None:
            receiver.stop()

        sock = self.sock
        self.sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        if receiver is not None:
            receiver.join(timeout=1.0)
        print("disconnected")

    def reconnect(self) -> None:
        self.disconnect()
        self.connect()

    def send_message(self, message: ProtocolMessage) -> str:
        if not self.state.connected or self.sock is None:
            raise RuntimeError("not connected")
        self.sock.sendall(encode_frame(message))
        return message.request_id

    def register(self, username: str, password: str) -> str:
        message = make_message(
            MessageType.REGISTER,
            payload={"username": username, "password": password},
        )
        return self.send_message(message)

    def login(self, username: str, password: str) -> str:
        message = make_message(
            MessageType.LOGIN,
            payload={"username": username, "password": password},
        )
        self.state.username = username
        self.state.login_confirmed = False
        self._pending_login[message.request_id] = username
        return self.send_message(message)

    def logout(self) -> str:
        request_id = self.send_message(
            make_message(MessageType.LOGOUT, sender=self.state.username)
        )
        self.state.login_confirmed = False
        return request_id

    def send_private(self, receiver: str, content: str) -> str:
        message = make_message(
            MessageType.PRIVATE_MSG,
            sender=self.state.username,
            receiver=receiver,
            payload={"content": content},
        )
        return self.send_message(message)

    def send_group(self, group_id: str, content: str) -> str:
        message = make_message(
            MessageType.GROUP_MSG,
            sender=self.state.username,
            group_id=group_id,
            payload={"content": content},
        )
        return self.send_message(message)

    def send_file(self, chat_type: str, target: str, path: str | Path) -> str:
        file_path = Path(path)
        if not file_path.is_file():
            raise ValueError(f"file not found: {file_path}")
        file_id = secrets.token_hex(16)
        filesize = file_path.stat().st_size
        mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        digest = self._sha256_file(file_path)
        start_payload = {
            "file_id": file_id,
            "filename": file_path.name,
            "filesize": filesize,
            "mime": mime,
            "sha256": digest,
        }
        if chat_type == "private":
            start = make_message(
                MessageType.FILE_START,
                sender=self.state.username,
                receiver=target,
                payload={**start_payload, "receiver": target},
            )
        elif chat_type == "group":
            start = make_message(
                MessageType.FILE_START,
                sender=self.state.username,
                group_id=target,
                payload={**start_payload, "group_id": target},
            )
        else:
            raise ValueError("chat_type must be private or group")

        self.send_message(start)
        offset = 0
        with file_path.open("rb") as fh:
            while True:
                chunk = fh.read(FILE_CHUNK_BYTES)
                if not chunk:
                    break
                self.send_message(
                    make_message(
                        MessageType.FILE_CHUNK,
                        sender=self.state.username,
                        payload={
                            "file_id": file_id,
                            "offset": offset,
                            "data": base64.b64encode(chunk).decode("ascii"),
                        },
                    )
                )
                offset += len(chunk)
        self.send_message(
            make_message(
                MessageType.FILE_END,
                sender=self.state.username,
                payload={"file_id": file_id, "sha256": digest},
            )
        )
        print(f"file upload queued: {file_path.name} ({filesize} bytes)")
        return file_id

    def recall_message(self, message_id: str) -> str:
        return self.send_message(
            make_message(
                MessageType.MESSAGE_RECALL,
                sender=self.state.username,
                payload={"message_id": message_id},
            )
        )

    def create_group(self, name: str) -> str:
        return self.send_message(
            make_message(
                MessageType.CREATE_GROUP,
                sender=self.state.username,
                payload={"name": name},
            )
        )

    def join_group(self, group_id: str) -> str:
        return self.send_message(
            make_message(
                MessageType.JOIN_GROUP,
                sender=self.state.username,
                group_id=group_id,
                payload={"group_id": group_id},
            )
        )

    def leave_group(self, group_id: str) -> str:
        return self.send_message(
            make_message(
                MessageType.LEAVE_GROUP,
                sender=self.state.username,
                group_id=group_id,
                payload={"group_id": group_id},
            )
        )

    def request_private_history(self, peer: str, limit: int = 50) -> str:
        return self.send_message(
            make_message(
                MessageType.HISTORY_REQUEST,
                sender=self.state.username,
                payload={"chat_type": "private", "peer": peer, "limit": limit},
            )
        )

    def request_group_history(self, group_id: str, limit: int = 50) -> str:
        return self.send_message(
            make_message(
                MessageType.HISTORY_REQUEST,
                sender=self.state.username,
                group_id=group_id,
                payload={"chat_type": "group", "group_id": group_id, "limit": limit},
            )
        )

    def heartbeat(self) -> str:
        self._heartbeat_seq += 1
        return self.send_message(
            make_message(
                MessageType.HEARTBEAT,
                sender=self.state.username,
                payload={"seq": self._heartbeat_seq},
            )
        )

    def handle_message(self, message: ProtocolMessage) -> None:
        if message.type == MessageType.ERROR:
            self._handle_error(message)
            return

        if message.type == MessageType.REGISTER:
            self._handle_register_response(message)
        elif message.type == MessageType.LOGIN:
            self._handle_login_response(message)
        elif message.type == MessageType.LOGOUT:
            self._handle_logout_response(message)
        elif message.type == MessageType.HEARTBEAT:
            self._handle_heartbeat_response(message)
        elif message.type in {MessageType.PRIVATE_MSG, MessageType.GROUP_MSG}:
            item = self.history.add_protocol_message(message, current_user=self.state.username)
            if item is not None:
                print(self.history.format_item(item, current_user=self.state.username))
        elif message.type == MessageType.MESSAGE_RECALL:
            item = self.history.recall_message(str(message.payload.get("message_id") or ""))
            if item is not None:
                print(self.history.format_item(item, current_user=self.state.username))
            else:
                print(f"message recalled: {message.payload.get('message_id')}")
        elif message.type == MessageType.HISTORY_RESPONSE:
            self._handle_history_response(message)
        elif message.type == MessageType.USER_STATUS:
            self._handle_user_status(message)
        elif message.type in {MessageType.CREATE_GROUP, MessageType.JOIN_GROUP, MessageType.LEAVE_GROUP}:
            self._handle_group_response(message)

        elif message.type in {MessageType.FILE_START, MessageType.FILE_CHUNK, MessageType.FILE_END}:
            self._handle_file_transfer_response(message)
        elif message.type not in {MessageType.PRIVATE_MSG, MessageType.GROUP_MSG}:
            print(f"received {message.type.value}: {message.payload}")

    def handle_disconnect(self, reason: str) -> None:
        self.state.connected = False
        self.state.login_confirmed = False
        self.state.user_status.clear()
        self.sock = None
        print(f"connection closed: {reason}")

    def handle_receiver_error(self, exc: Exception) -> None:
        print(f"receive error: {exc}")

    def _handle_error(self, message: ProtocolMessage) -> None:
        failed_username = self._pending_login.pop(message.request_id, None)
        if failed_username is not None and self.state.username == failed_username:
            self.state.username = None
            self.state.login_confirmed = False
        error_code = message.payload.get("error_code", "unknown")
        text = message.payload.get("message", "")
        print(f"error {error_code}: {text}")

    def _handle_register_response(self, message: ProtocolMessage) -> None:
        username = message.payload.get("username") or message.receiver
        display_name = message.payload.get("display_name") or username
        print(f"registered: {username} ({display_name})")

    def _handle_login_response(self, message: ProtocolMessage) -> None:
        username = self._pending_login.pop(message.request_id, None)
        username = username or message.payload.get("username") or message.sender
        if username:
            self.state.username = str(username)
        self.state.login_confirmed = True
        if self.state.username:
            self.state.user_status[self.state.username] = "online"

        online_users = message.payload.get("online_users", [])
        if isinstance(online_users, list):
            for user in online_users:
                self.state.user_status[str(user)] = "online"
        print(f"logged in as {self.state.username}")

    def _handle_logout_response(self, message: ProtocolMessage) -> None:
        username = message.payload.get("username") or self.state.username
        if username:
            self.state.user_status[str(username)] = "offline"
        if username == self.state.username:
            self.state.username = None
            self.state.login_confirmed = False
        print(f"logged out: {username or '-'}")

    def _handle_heartbeat_response(self, message: ProtocolMessage) -> None:
        print(f"heartbeat ok: seq={message.payload.get('seq')}")

    def _handle_history_response(self, message: ProtocolMessage) -> None:
        items = self.history.cache_history_response(message, current_user=self.state.username)
        if not items:
            print("history: no messages")
            return
        for line in self.history.format_items(items, current_user=self.state.username):
            print(line)

    def _handle_user_status(self, message: ProtocolMessage) -> None:
        payload = message.payload
        username = payload.get("username")
        status = payload.get("status")
        if username and status:
            self.state.user_status[str(username)] = str(status)

        statuses = payload.get("statuses")
        if isinstance(statuses, dict):
            for user, user_status in statuses.items():
                self.state.user_status[str(user)] = str(user_status)
        elif isinstance(statuses, list):
            self._merge_status_rows(statuses)

    def _merge_status_rows(self, rows: list[Any]) -> None:
        for row in rows:
            if not isinstance(row, dict):
                continue
            username = row.get("username")
            status = row.get("status")
            if username and status:
                self.state.user_status[str(username)] = str(status)

    def _handle_group_response(self, message: ProtocolMessage) -> None:
        group_id = str(message.group_id or message.payload.get("group_id") or "")
        if not group_id:
            return

        actor = message.payload.get("username")
        is_about_current_user = (
            message.receiver == self.state.username
            or actor == self.state.username
            or message.type == MessageType.CREATE_GROUP
        )

        if is_about_current_user and message.type in {MessageType.CREATE_GROUP, MessageType.JOIN_GROUP}:
            self.state.groups.add(group_id)
        elif is_about_current_user and message.type == MessageType.LEAVE_GROUP:
            self.state.groups.discard(group_id)

        if message.type == MessageType.CREATE_GROUP:
            name = message.payload.get("name", "")
            print(f"group created: {group_id} {name}".rstrip())
        elif message.type == MessageType.JOIN_GROUP:
            user = actor or message.receiver or "-"
            print(f"group joined: {group_id} by {user}")
        elif message.type == MessageType.LEAVE_GROUP:
            user = actor or message.receiver or "-"
            print(f"group left: {group_id} by {user}")

    def _handle_file_transfer_response(self, message: ProtocolMessage) -> None:
        file_id = message.payload.get("file_id", "-")
        status = message.payload.get("status")
        offset = message.payload.get("offset")
        filesize = message.payload.get("filesize")
        if message.type == MessageType.FILE_CHUNK:
            print(f"file chunk ack: {file_id} {offset}/{filesize}")
        elif message.type == MessageType.FILE_END:
            print(f"file finished: {file_id} {status or ''}".rstrip())
        else:
            print(f"file started: {file_id}")

    def _sha256_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the chat CLI client.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-connect", action="store_true", help="start CLI without connecting")
    return parser.parse_args()


def main() -> None:
    from .ui import run_cli

    args = parse_args()
    client = ChatClient(args.host, args.port)
    if not args.no_connect:
        try:
            client.connect()
        except OSError as exc:
            print(f"connect failed: {exc}")
    run_cli(client)


if __name__ == "__main__":
    main()
