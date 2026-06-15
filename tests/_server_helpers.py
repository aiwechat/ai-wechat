"""Helpers shared between server-side integration tests."""

from __future__ import annotations

import socket
from typing import Iterable

from common.protocol import (
    MessageType,
    ProtocolMessage,
    decode_frames,
    encode_frame,
    make_message,
)
from server.moderation import ModerationResult


class AllowAllModeration:
    def check(self, content: str) -> ModerationResult:
        return ModerationResult(allowed=True)


class KeywordRecallModeration:
    def __init__(self, keyword: str) -> None:
        self.keyword = keyword

    def check(self, content: str) -> ModerationResult:
        if self.keyword not in content:
            return ModerationResult(allowed=True)
        return ModerationResult(
            allowed=False,
            action="recall",
            reason="message failed safety review",
            matched_words=(self.keyword,),
            categories=("keyword",),
        )


class TestClient:
    """A thin, synchronous client wrapper for use in tests.

    Frames the same way real clients do (length-prefixed JSON) and buffers
    incoming frames so individual `.recv()` calls can pull them one at a time.
    """

    def __init__(self, host: str, port: int, *, timeout: float = 5.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buffer = b""
        self._pending: list[ProtocolMessage] = []

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "TestClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # --- I/O -----------------------------------------------------------------

    def send(self, message: ProtocolMessage) -> None:
        self.sock.sendall(encode_frame(message))

    def recv(self) -> ProtocolMessage:
        while not self._pending:
            messages, self._buffer = decode_frames(self._buffer)
            if messages:
                self._pending.extend(messages)
                break
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError("server closed connection")
            self._buffer += chunk
        return self._pending.pop(0)

    def recv_of_type(
        self,
        wanted: MessageType,
        *,
        max_messages: int = 16,
    ) -> ProtocolMessage:
        """Pull frames until we get one of the requested type. Buffers others.

        Stashes anything we skip past back into the pending queue so a later
        call (looking for a different type) can still find it.
        """

        skipped: list[ProtocolMessage] = []
        try:
            for _ in range(max_messages):
                msg = self.recv()
                if msg.type == wanted:
                    return msg
                skipped.append(msg)
            raise AssertionError(
                f"did not receive {wanted.value} within {max_messages} frames; "
                f"saw {[m.type.value for m in skipped]}"
            )
        finally:
            # Preserve the messages we walked past so later assertions can find them.
            self._pending = skipped + self._pending

    def drain(self, count: int) -> list[ProtocolMessage]:
        return [self.recv() for _ in range(count)]

    # --- high-level helpers --------------------------------------------------

    def register(self, username: str, password: str) -> ProtocolMessage:
        self.send(
            make_message(
                MessageType.REGISTER,
                payload={"username": username, "password": password},
            )
        )
        return self.recv_of_type(MessageType.REGISTER)

    def login(self, username: str, password: str) -> ProtocolMessage:
        self.send(
            make_message(
                MessageType.LOGIN,
                payload={"username": username, "password": password},
            )
        )
        return self.recv_of_type(MessageType.LOGIN)

    def send_private(self, receiver: str, content: str) -> None:
        self.send(
            make_message(
                MessageType.PRIVATE_MSG,
                receiver=receiver,
                payload={"content": content},
            )
        )


def assert_no_error(message: ProtocolMessage, context: str = "") -> None:
    if message.type == MessageType.ERROR:
        raise AssertionError(
            f"unexpected error response{(': ' + context) if context else ''}: "
            f"{message.payload}"
        )


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def usernames(prefix: str, count: int) -> Iterable[str]:
    for i in range(count):
        yield f"{prefix}{i:03d}"
