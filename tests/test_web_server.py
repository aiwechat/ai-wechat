from __future__ import annotations

import base64
import json
import socket
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

from common.protocol import MessageType, make_message
from server.web_server import WebChatServer, encode_client_ws_text, read_ws_frame, read_ws_message
from tests._server_helpers import AllowAllModeration


class WebServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.server = WebChatServer(
            host="127.0.0.1",
            port=0,
            db_path=Path(self.tmpdir.name) / "chat.db",
            heartbeat_timeout=10.0,
            heartbeat_interval=1.0,
            moderation=AllowAllModeration(),
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

    def test_file_download_by_token(self) -> None:
        upload_path = Path(self.tmpdir.name) / "uploads" / "file-web-1"
        upload_path.parent.mkdir(parents=True, exist_ok=True)
        upload_path.write_bytes(b"download me")
        self.server.db.create_user("alice", "alice-pw")
        self.server.db.create_user("bob", "bob-pw")
        transfer = self.server.db.create_file_transfer(
            sender="alice",
            receiver="bob",
            filename="note.txt",
            filesize=11,
            file_id="file-web-1",
            mime="text/plain",
            storage_path=str(upload_path),
            download_token="token-1",
        )
        self.server.db.update_file_transfer(transfer["file_id"], status="finished", offset=11)

        with urlopen(f"http://127.0.0.1:{self.port}/files/file-web-1?token=token-1", timeout=5.0) as response:
            self.assertEqual(response.read(), b"download me")
            self.assertEqual(response.headers["Content-Type"], "text/plain")


class WebSocketFrameTest(unittest.TestCase):
    def test_read_ws_message_reassembles_fragmented_text(self) -> None:
        first = _masked_frame(b'{"type":"', opcode=0x1, fin=False, mask=b"\x01\x02\x03\x04")
        second = _masked_frame(b'heartbeat"}', opcode=0x0, fin=True, mask=b"\x05\x06\x07\x08")
        left, right = socket.socketpair()
        try:
            left.sendall(first + second)
            opcode, payload = read_ws_message(right)
            self.assertEqual(opcode, 0x1)
            self.assertEqual(payload, b'{"type":"heartbeat"}')
        finally:
            left.close()
            right.close()


def _masked_frame(payload: bytes, *, opcode: int, fin: bool, mask: bytes) -> bytes:
    first = (0x80 if fin else 0) | opcode
    length = len(payload)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    if length < 126:
        return bytes([first, 0x80 | length]) + mask + masked
    if length <= 0xFFFF:
        return bytes([first, 0x80 | 126]) + length.to_bytes(2, "big") + mask + masked
    return bytes([first, 0x80 | 127]) + length.to_bytes(8, "big") + mask + masked


if __name__ == "__main__":
    unittest.main()
