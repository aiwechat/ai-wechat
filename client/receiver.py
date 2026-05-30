"""Socket receive loop for framed protocol messages."""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable

from common.protocol import ProtocolError, ProtocolMessage, decode_frames


MessageHandler = Callable[[ProtocolMessage], None]
DisconnectHandler = Callable[[str], None]
ErrorHandler = Callable[[Exception], None]


class Receiver:
    """Read bytes from a socket, decode protocol frames, and dispatch messages."""

    def __init__(
        self,
        sock: socket.socket,
        on_message: MessageHandler,
        *,
        on_disconnect: DisconnectHandler | None = None,
        on_error: ErrorHandler | None = None,
        recv_size: int = 4096,
    ) -> None:
        self.sock = sock
        self.on_message = on_message
        self.on_disconnect = on_disconnect
        self.on_error = on_error
        self.recv_size = recv_size
        self._buffer = b""
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self.run, name="chat-receiver", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                chunk = self.sock.recv(self.recv_size)
                if not chunk:
                    self._notify_disconnect("server closed connection")
                    return

                self._buffer += chunk
                messages, self._buffer = decode_frames(self._buffer)
                for message in messages:
                    self.on_message(message)

            except socket.timeout:
                continue
            except ProtocolError as exc:
                self._buffer = b""
                self._notify_error(exc)
            except OSError as exc:
                if not self._stop_event.is_set():
                    self._notify_disconnect(str(exc))
                return
            except Exception as exc:  # Keep the receive thread from dying silently.
                self._notify_error(exc)

    def _notify_disconnect(self, reason: str) -> None:
        if self.on_disconnect is not None:
            self.on_disconnect(reason)

    def _notify_error(self, exc: Exception) -> None:
        if self.on_error is not None:
            self.on_error(exc)
