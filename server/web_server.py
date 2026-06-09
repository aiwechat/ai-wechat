"""Browser GUI server for ai-wechat.

This module serves a small responsive web client and exposes a WebSocket
endpoint that speaks the same JSON envelope used by the TCP protocol. It keeps
the browser path dependency-free so the project can run with the Python
standard library only.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field
import hashlib
import logging
from pathlib import Path
import signal
import socket
import struct
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse
from uuid import uuid4

from common.protocol import ProtocolError, ProtocolMessage
from server.database import DEFAULT_DB_PATH
from server.relay import ChatRelayService


logger = logging.getLogger(__name__)

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
ROOT_DIR = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT_DIR / "web"
MAX_WS_FRAME_SIZE = 8 * 1024 * 1024
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


@dataclass
class WebClientSession:
    """WebSocket-backed session compatible with UserManager and MessageRouter."""

    connection: socket.socket
    address: tuple[str, int]
    client_id: str = field(default_factory=lambda: uuid4().hex)
    username: str | None = None
    last_seen: float = field(default_factory=time.monotonic)
    send_lock: threading.RLock = field(default_factory=threading.RLock)
    closed: bool = False

    @property
    def authenticated(self) -> bool:
        return self.username is not None

    @property
    def label(self) -> str:
        return self.username or f"web@{self.address[0]}:{self.address[1]}"

    def send(self, message: ProtocolMessage) -> bool:
        with self.send_lock:
            if self.closed:
                return False
            try:
                self.connection.sendall(encode_ws_text(message.to_json()))
                return True
            except OSError as exc:
                logger.debug("websocket send failed for %s: %s", self.label, exc)
                self.closed = True
                return False

    def close(self) -> None:
        with self.send_lock:
            if self.closed:
                return
            self.closed = True
            try:
                self.connection.sendall(encode_ws_close())
            except OSError:
                pass
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def touch(self) -> None:
        self.last_seen = time.monotonic()


class WebChatServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        *,
        db_path: str | Path = DEFAULT_DB_PATH,
        heartbeat_timeout: float = 60.0,
        heartbeat_interval: float = 15.0,
        relay: ChatRelayService | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.relay = relay or ChatRelayService(
            db_path=db_path,
            heartbeat_timeout=heartbeat_timeout,
            heartbeat_interval=heartbeat_interval,
        )
        self._owns_relay = relay is None
        self.db = self.relay.db
        self.users = self.relay.users
        self.groups = self.relay.groups
        self.router = self.relay.router
        self.heartbeat = self.relay.heartbeat
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._sessions: dict[str, WebClientSession] = {}
        self._sessions_lock = threading.Lock()

    def start(self) -> int:
        self.relay.start()
        handler_cls = self._make_handler()
        httpd = _ThreadingHTTPServer((self.host, self.port), handler_cls)
        httpd.chat_server = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="WebChatServer", daemon=True)
        self._thread.start()
        actual_port = httpd.server_address[1]
        logger.info("web chat server listening on http://%s:%d", self.host, actual_port)
        return actual_port

    def stop(self, *, join_timeout: float = 5.0) -> None:
        for session in self._gateway_sessions():
            self.users.remove_session(session)
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
            self._thread = None
        if self._owns_relay:
            self.relay.stop(join_timeout=join_timeout)

    def serve_forever(self) -> None:
        self.start()
        stop_event = threading.Event()

        def _signal_handler(signum, _frame):  # noqa: ANN001 - signal callback
            logger.info("received signal %s, shutting down web server", signum)
            stop_event.set()

        signal.signal(signal.SIGINT, _signal_handler)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _signal_handler)
        try:
            stop_event.wait()
        finally:
            self.stop()

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        chat_server = self

        class WebHandler(BaseHTTPRequestHandler):
            server_version = "AIWechatWeb/1.0"

            def do_GET(self) -> None:  # noqa: N802 - stdlib hook
                parsed = urlparse(self.path)
                if parsed.path == "/ws":
                    self._handle_websocket()
                    return
                if parsed.path.startswith("/files/"):
                    self._serve_file_download(parsed.path, parsed.query)
                    return
                self._serve_static()

            def log_message(self, fmt: str, *args: Any) -> None:
                logger.debug("%s - %s", self.address_string(), fmt % args)

            def _serve_static(self) -> None:
                path = self.path.split("?", 1)[0]
                if path in {"", "/"}:
                    path = "/index.html"
                safe_parts = [part for part in path.strip("/").split("/") if part and part not in {".", ".."}]
                file_path = WEB_DIR.joinpath(*safe_parts)
                if not file_path.exists() or not file_path.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND, "not found")
                    return
                body = file_path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", CONTENT_TYPES.get(file_path.suffix, "application/octet-stream"))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _serve_file_download(self, path: str, query: str) -> None:
                file_id = unquote(path.removeprefix("/files/")).strip("/")
                token = parse_qs(query).get("token", [""])[0]
                if not file_id or not token:
                    self.send_error(HTTPStatus.FORBIDDEN, "missing file token")
                    return
                transfer = chat_server.db.get_file_transfer_by_token(file_id, token)
                if transfer is None or transfer.get("status") != "finished":
                    self.send_error(HTTPStatus.NOT_FOUND, "file not found")
                    return
                storage_path = transfer.get("storage_path")
                if not storage_path:
                    self.send_error(HTTPStatus.NOT_FOUND, "file not found")
                    return
                file_path = Path(str(storage_path))
                try:
                    file_path = file_path.resolve(strict=True)
                except OSError:
                    self.send_error(HTTPStatus.NOT_FOUND, "file not found")
                    return
                if not file_path.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND, "file not found")
                    return
                filename = str(transfer.get("filename") or transfer["file_id"])
                mime = str(transfer.get("mime") or "application/octet-stream")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(file_path.stat().st_size))
                self.send_header("Content-Disposition", f'attachment; filename="{quote(filename)}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with file_path.open("rb") as fh:
                    while True:
                        chunk = fh.read(1024 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)

            def _handle_websocket(self) -> None:
                key = self.headers.get("Sec-WebSocket-Key")
                if not key:
                    self.send_error(HTTPStatus.BAD_REQUEST, "missing websocket key")
                    return
                accept = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
                self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()

                session = WebClientSession(
                    connection=self.connection,
                    address=(self.client_address[0], self.client_address[1]),
                )
                chat_server.users.register_session(session)
                chat_server._track_session(session)
                try:
                    while not session.closed:
                        frame = read_ws_message(self.connection)
                        if frame is None:
                            break
                        opcode, payload = frame
                        if opcode == 0x8:
                            break
                        if opcode == 0x9:
                            self.connection.sendall(encode_ws_frame(payload, opcode=0xA))
                            continue
                        if opcode != 0x1:
                            continue
                        try:
                            message = ProtocolMessage.from_json(payload)
                        except ProtocolError as exc:
                            session.send(exc.to_message())
                            continue
                        chat_server.router.dispatch(session, message)
                except (ConnectionError, OSError):
                    pass
                finally:
                    chat_server.users.remove_session(session)
                    chat_server._untrack_session(session)

        return WebHandler

    def _track_session(self, session: WebClientSession) -> None:
        with self._sessions_lock:
            self._sessions[session.client_id] = session

    def _untrack_session(self, session: WebClientSession) -> None:
        with self._sessions_lock:
            self._sessions.pop(session.client_id, None)

    def _gateway_sessions(self) -> list[WebClientSession]:
        with self._sessions_lock:
            return list(self._sessions.values())


class _ThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def read_ws_frame(sock: socket.socket) -> tuple[int, bytes] | None:
    frame = _read_ws_frame_parts(sock)
    if frame is None:
        return None
    fin, opcode, payload = frame
    if not fin:
        raise ConnectionError("fragmented websocket frames are not supported by read_ws_frame")
    return opcode, payload


def read_ws_message(sock: socket.socket) -> tuple[int, bytes] | None:
    """Read one complete WebSocket message, including fragmented messages."""

    first = _read_ws_frame_parts(sock)
    if first is None:
        return None
    fin, opcode, payload = first
    if opcode in {0x8, 0x9, 0xA}:
        return opcode, payload
    if fin:
        return opcode, payload

    parts = [payload]
    message_opcode = opcode
    while True:
        frame = _read_ws_frame_parts(sock)
        if frame is None:
            raise ConnectionError("socket closed during fragmented websocket message")
        fin, opcode, payload = frame
        if opcode in {0x8, 0x9, 0xA}:
            if opcode == 0x8:
                return opcode, payload
            continue
        if opcode != 0x0:
            raise ConnectionError("expected websocket continuation frame")
        parts.append(payload)
        if sum(len(part) for part in parts) > MAX_WS_FRAME_SIZE:
            raise ConnectionError("websocket message too large")
        if fin:
            return message_opcode, b"".join(parts)


def _read_ws_frame_parts(sock: socket.socket) -> tuple[bool, int, bytes] | None:
    header = _recv_exact(sock, 2)
    if not header:
        return None
    first, second = header
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    if length > MAX_WS_FRAME_SIZE:
        raise ConnectionError("websocket frame too large")
    mask = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return fin, opcode, payload


def encode_ws_text(text: str) -> bytes:
    return encode_ws_frame(text.encode("utf-8"), opcode=0x1)


def encode_ws_close() -> bytes:
    return encode_ws_frame(b"", opcode=0x8)


def encode_ws_frame(payload: bytes, *, opcode: int = 0x1) -> bytes:
    header = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        return header + bytes([length]) + payload
    if length <= 0xFFFF:
        return header + bytes([126]) + struct.pack("!H", length) + payload
    return header + bytes([127]) + struct.pack("!Q", length) + payload


def encode_client_ws_text(text: str, *, mask: bytes = b"\x01\x02\x03\x04") -> bytes:
    """Encode a masked text frame. Used by tests that emulate a browser."""

    payload = text.encode("utf-8")
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    length = len(payload)
    if length < 126:
        return bytes([0x81, 0x80 | length]) + mask + masked
    if length <= 0xFFFF:
        return bytes([0x81, 0x80 | 126]) + struct.pack("!H", length) + mask + masked
    return bytes([0x81, 0x80 | 127]) + struct.pack("!Q", length) + mask + masked


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("socket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="ai-wechat browser GUI server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path")
    parser.add_argument("--heartbeat-timeout", type=float, default=60.0)
    parser.add_argument("--heartbeat-interval", type=float, default=15.0)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)
    server = WebChatServer(
        host=args.host,
        port=args.port,
        db_path=args.db,
        heartbeat_timeout=args.heartbeat_timeout,
        heartbeat_interval=args.heartbeat_interval,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
