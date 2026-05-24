"""TCP server entrypoint for the chat system.

One thread accepts new TCP connections; one reader thread per client
consumes frames and dispatches them through `MessageRouter`. Writes go
through `ClientSession.send` which serialises bytes per socket. This is a
deliberately simple thread-per-connection model: the project requirement
is 50 concurrent clients, and the GIL is not a bottleneck for socket I/O.
"""

from __future__ import annotations

import argparse
import logging
import signal
import socket
import threading
from pathlib import Path
from typing import Any

from common.protocol import (
    ProtocolError,
    decode_frames,
)
from server.database import DEFAULT_DB_PATH, init_db
from server.group_manager import GroupManager
from server.heartbeat import HeartbeatMonitor
from server.message_router import MessageRouter
from server.user_manager import ClientSession, UserManager


logger = logging.getLogger(__name__)


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 9000
RECV_CHUNK_SIZE = 4096


class ChatServer:
    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        db_path: str | Path = DEFAULT_DB_PATH,
        backlog: int = 128,
        heartbeat_timeout: float = 60.0,
        heartbeat_interval: float = 15.0,
        recv_timeout: float | None = 30.0,
    ) -> None:
        self.host = host
        self.port = port
        self.backlog = backlog
        self.recv_timeout = recv_timeout

        self.db = init_db(db_path)
        self.users = UserManager(self.db)
        self.groups = GroupManager(self.db)
        self.router = MessageRouter(self.db, self.users, self.groups)
        self.heartbeat = HeartbeatMonitor(
            self.users,
            timeout_seconds=heartbeat_timeout,
            interval_seconds=heartbeat_interval,
        )

        self._server_sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._client_threads: list[threading.Thread] = []
        self._client_threads_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._actual_port: int | None = None

    # --- lifecycle -------------------------------------------------------

    def start(self) -> int:
        """Bind, listen, and start background threads. Returns the bound port."""

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(self.backlog)
        sock.settimeout(1.0)  # so accept() loop can poll the stop flag
        self._server_sock = sock
        self._actual_port = sock.getsockname()[1]

        self.heartbeat.start()
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="AcceptLoop",
            daemon=True,
        )
        self._accept_thread.start()

        logger.info("chat server listening on %s:%d", self.host, self._actual_port)
        return self._actual_port

    def stop(self, *, join_timeout: float = 5.0) -> None:
        if self._stop_event.is_set():
            return
        logger.info("stopping chat server")
        self._stop_event.set()
        self.heartbeat.stop(join_timeout=join_timeout)

        sock = self._server_sock
        self._server_sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

        # Tear down every active session so reader threads exit.
        for session in self.users.all_sessions():
            try:
                self.users.remove_session(session)
            except Exception:
                logger.exception("error while closing session %s", session.label)

        if self._accept_thread is not None:
            self._accept_thread.join(timeout=join_timeout)
            self._accept_thread = None

        with self._client_threads_lock:
            threads = list(self._client_threads)
            self._client_threads.clear()
        for t in threads:
            t.join(timeout=join_timeout)

    @property
    def actual_port(self) -> int:
        if self._actual_port is None:
            raise RuntimeError("server is not running")
        return self._actual_port

    # --- accept / reader loops ------------------------------------------

    def _accept_loop(self) -> None:
        assert self._server_sock is not None
        while not self._stop_event.is_set():
            try:
                client_sock, addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # server socket closed during shutdown
            client_sock.settimeout(self.recv_timeout)
            thread = threading.Thread(
                target=self._client_loop,
                args=(client_sock, addr),
                name=f"Client-{addr[0]}:{addr[1]}",
                daemon=True,
            )
            with self._client_threads_lock:
                self._client_threads.append(thread)
            thread.start()
            self._reap_finished_threads()

    def _reap_finished_threads(self) -> None:
        with self._client_threads_lock:
            self._client_threads[:] = [t for t in self._client_threads if t.is_alive()]

    def _client_loop(self, sock: socket.socket, address: tuple[str, int]) -> None:
        session = ClientSession(sock=sock, address=address)
        self.users.register_session(session)
        logger.debug("client connected from %s", address)
        buffer = b""
        try:
            while not self._stop_event.is_set() and not session.closed:
                try:
                    chunk = sock.recv(RECV_CHUNK_SIZE)
                except socket.timeout:
                    # No traffic in `recv_timeout`s — keep looping so the
                    # heartbeat monitor can decide whether to kick.
                    continue
                except OSError as exc:
                    logger.debug("recv failed on %s: %s", session.label, exc)
                    break
                if not chunk:
                    break

                buffer += chunk
                try:
                    messages, buffer = decode_frames(buffer)
                except ProtocolError as exc:
                    session.send(exc.to_message())
                    # The buffer is now in an unknown state; safest is to drop.
                    buffer = b""
                    continue

                for message in messages:
                    if session.closed:
                        break
                    self.router.dispatch(session, message)
        except Exception:
            logger.exception("client loop crashed for %s", session.label)
        finally:
            self.users.remove_session(session)
            logger.debug("client disconnected: %s", session.label)

    # --- introspection (handy for tests) ---------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self._actual_port,
            "online_users": self.users.online_users(),
            "total_sessions": len(self.users.all_sessions()),
        }


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="ai-wechat chat server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path")
    parser.add_argument("--heartbeat-timeout", type=float, default=60.0)
    parser.add_argument("--heartbeat-interval", type=float, default=15.0)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)

    server = ChatServer(
        host=args.host,
        port=args.port,
        db_path=args.db,
        heartbeat_timeout=args.heartbeat_timeout,
        heartbeat_interval=args.heartbeat_interval,
    )
    server.start()

    stop_event = threading.Event()

    def _signal_handler(signum, _frame):  # noqa: ANN001 - signal callback
        logger.info("received signal %s, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _signal_handler)

    try:
        stop_event.wait()
    finally:
        server.stop()


if __name__ == "__main__":
    main()
