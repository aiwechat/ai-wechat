from __future__ import annotations

import base64
import socket
import tempfile
import unittest
from pathlib import Path

from common.protocol import MessageType, ProtocolMessage, make_message
from server.relay import ChatRelayService
from server.server import ChatServer
from server.web_server import WebChatServer, encode_client_ws_text, read_ws_message
from tests._server_helpers import AllowAllModeration, TestClient, free_port


class WebSocketTestClient:
    def __init__(self, host: str, port: int, *, timeout: float = 5.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._pending: list[ProtocolMessage] = []
        key = base64.b64encode(b"ai-wechat-relay-test").decode("ascii")
        request = (
            "GET /ws HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = self._read_http_headers()
        if b"101 Switching Protocols" not in response:
            raise AssertionError(f"websocket handshake failed: {response!r}")

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "WebSocketTestClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def send(self, message: ProtocolMessage) -> None:
        self.sock.sendall(encode_client_ws_text(message.to_json()))

    def recv(self) -> ProtocolMessage:
        if self._pending:
            return self._pending.pop(0)
        while True:
            frame = read_ws_message(self.sock)
            if frame is None:
                raise EOFError("websocket closed")
            opcode, payload = frame
            if opcode == 0x8:
                raise EOFError("websocket closed")
            if opcode != 0x1:
                continue
            return ProtocolMessage.from_json(payload)

    def recv_of_type(self, wanted: MessageType, *, max_messages: int = 16) -> ProtocolMessage:
        skipped: list[ProtocolMessage] = []
        try:
            for _ in range(max_messages):
                message = self.recv()
                if message.type == wanted:
                    return message
                skipped.append(message)
            raise AssertionError(
                f"did not receive {wanted.value} within {max_messages} frames; "
                f"saw {[message.type.value for message in skipped]}"
            )
        finally:
            self._pending = skipped + self._pending

    def _read_http_headers(self) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data


class RelayServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.relay = ChatRelayService(
            db_path=Path(self.tmpdir.name) / "chat.db",
            heartbeat_timeout=10.0,
            heartbeat_interval=1.0,
            moderation=AllowAllModeration(),
        )
        self.tcp_server = ChatServer(
            host="127.0.0.1",
            port=free_port(),
            relay=self.relay,
            recv_timeout=1.0,
        )
        self.web_server = WebChatServer(
            host="127.0.0.1",
            port=0,
            relay=self.relay,
        )
        self.tcp_port = self.tcp_server.start()
        self.web_port = self.web_server.start()
        self.relay.db.create_user("cli-user", "cli-pw")
        self.relay.db.create_user("web-user", "web-pw")

    def tearDown(self) -> None:
        self.tcp_server.stop(join_timeout=2.0)
        self.web_server.stop(join_timeout=2.0)
        self.relay.stop(join_timeout=2.0)
        self.tmpdir.cleanup()

    def test_tcp_and_websocket_clients_share_live_chat(self) -> None:
        with TestClient("127.0.0.1", self.tcp_port) as cli, WebSocketTestClient(
            "127.0.0.1", self.web_port
        ) as web:
            cli.login("cli-user", "cli-pw")

            web.send(
                make_message(
                    MessageType.LOGIN,
                    payload={"username": "web-user", "password": "web-pw"},
                )
            )
            web_login = web.recv_of_type(MessageType.LOGIN)
            self.assertEqual(web_login.payload["username"], "web-user")

            cli.send_private("web-user", "hello from tcp")
            web_forward = web.recv_of_type(MessageType.PRIVATE_MSG)
            self.assertEqual(web_forward.sender, "cli-user")
            self.assertEqual(web_forward.receiver, "web-user")
            self.assertEqual(web_forward.payload["content"], "hello from tcp")

            cli_echo = cli.recv_of_type(MessageType.PRIVATE_MSG)
            self.assertTrue(cli_echo.meta.get("echo"))
            self.assertTrue(cli_echo.payload["delivered"])

            web.send(
                make_message(
                    MessageType.PRIVATE_MSG,
                    receiver="cli-user",
                    payload={"content": "hello from websocket"},
                )
            )
            cli_forward = cli.recv_of_type(MessageType.PRIVATE_MSG)
            self.assertEqual(cli_forward.sender, "web-user")
            self.assertEqual(cli_forward.receiver, "cli-user")
            self.assertEqual(cli_forward.payload["content"], "hello from websocket")


if __name__ == "__main__":
    unittest.main()
