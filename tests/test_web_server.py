from __future__ import annotations

import base64
import json
import socket
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

from common.protocol import MessageType, make_message
from server.web_server import WebChatServer, encode_client_ws_text, read_ws_frame


class WebServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.server = WebChatServer(
            host="127.0.0.1",
            port=0,
            db_path=Path(self.tmpdir.name) / "chat.db",
            heartbeat_timeout=10.0,
            heartbeat_interval=1.0,
        )
        self.port = self.server.start()

    def tearDown(self) -> None:
        self.server.stop(join_timeout=2.0)
        self.tmpdir.cleanup()

    def test_serves_index_html(self) -> None:
        with urlopen(f"http://127.0.0.1:{self.port}/", timeout=5.0) as response:
            body = response.read().decode("utf-8")
        self.assertIn("AI WeChat", body)
        self.assertIn("/app.js", body)

    def test_websocket_register_roundtrip(self) -> None:
        with socket.create_connection(("127.0.0.1", self.port), timeout=5.0) as sock:
            sock.settimeout(5.0)
            key = base64.b64encode(b"ai-wechat-test-key").decode("ascii")
            request = (
                "GET /ws HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            )
            sock.sendall(request.encode("ascii"))
            response = sock.recv(4096).decode("ascii")
            self.assertIn("101 Switching Protocols", response)

            outgoing = make_message(
                MessageType.REGISTER,
                payload={"username": "web-alice", "password": "alice-pw"},
            )
            sock.sendall(encode_client_ws_text(outgoing.to_json()))
            opcode, payload = read_ws_frame(sock)
            self.assertEqual(opcode, 0x1)
            incoming = json.loads(payload.decode("utf-8"))
            self.assertEqual(incoming["type"], MessageType.REGISTER.value)
            self.assertEqual(incoming["payload"]["username"], "web-alice")


if __name__ == "__main__":
    unittest.main()
