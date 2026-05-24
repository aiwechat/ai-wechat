"""Online user registry and per-connection session state.

The user manager is the single source of truth for which usernames are
currently online and through which TCP connection they should be reached.
It also wraps `server.database.ChatDatabase` for the authentication paths
so the router never has to talk to SQLite directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import socket
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from common.protocol import (
    ErrorCode,
    ProtocolError,
    ProtocolMessage,
    encode_frame,
)
from server.database import ChatDatabase


logger = logging.getLogger(__name__)


@dataclass
class ClientSession:
    """A single live TCP connection.

    A session starts unauthenticated (`username is None`). After a successful
    login the username is bound and the session is also indexed by username
    in `UserManager`. `send()` is safe to call from any thread because it
    holds `send_lock` while writing to the socket.
    """

    sock: socket.socket
    address: tuple[str, int]
    client_id: str = field(default_factory=lambda: uuid4().hex)
    username: str | None = None
    last_seen: float = field(default_factory=time.monotonic)
    send_lock: threading.RLock = field(default_factory=threading.RLock)
    closed: bool = False

    @property
    def authenticated(self) -> bool:
        return self.username is not None

    def send(self, message: ProtocolMessage) -> bool:
        """Send one protocol frame. Returns False if the socket is gone."""

        with self.send_lock:
            if self.closed:
                return False
            try:
                self.sock.sendall(encode_frame(message))
                return True
            except OSError as exc:
                logger.debug("send failed for %s: %s", self.label, exc)
                self.closed = True
                return False

    def close(self) -> None:
        with self.send_lock:
            if self.closed:
                return
            self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    @property
    def label(self) -> str:
        return self.username or f"anon@{self.address[0]}:{self.address[1]}"


class UserManager:
    """Tracks online sessions and brokers DB-backed user operations."""

    def __init__(self, db: ChatDatabase) -> None:
        self.db = db
        self._lock = threading.RLock()
        self._sessions_by_id: dict[str, ClientSession] = {}
        self._sessions_by_username: dict[str, ClientSession] = {}

    # --- session registry -------------------------------------------------

    def register_session(self, session: ClientSession) -> None:
        with self._lock:
            self._sessions_by_id[session.client_id] = session

    def remove_session(self, session: ClientSession) -> str | None:
        """Drop a session from all indexes and mark the DB user offline.

        Returns the username that just went offline, or None if the session
        was never authenticated. Safe to call multiple times.
        """

        offline_user: str | None = None
        with self._lock:
            self._sessions_by_id.pop(session.client_id, None)
            if session.username is not None:
                bound = self._sessions_by_username.get(session.username)
                if bound is session:
                    self._sessions_by_username.pop(session.username, None)
                    offline_user = session.username
        if offline_user is not None:
            try:
                self.db.set_user_status(offline_user, "offline")
            except Exception:
                logger.exception("failed to mark %s offline", offline_user)
        session.close()
        return offline_user

    # --- auth -------------------------------------------------------------

    def register_user(
        self,
        username: str,
        password: str,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        if not username or not password:
            raise ProtocolError(
                ErrorCode.INVALID_FIELD,
                "username and password are required",
            )
        try:
            return self.db.create_user(username, password, display_name=display_name)
        except ValueError as exc:
            raise ProtocolError(
                ErrorCode.CONFLICT,
                f"username already exists: {username}",
                detail={"username": username},
            ) from exc

    def login(
        self,
        session: ClientSession,
        username: str,
        password: str,
        *,
        pre_attach_send: Callable[[dict[str, Any]], ProtocolMessage] | None = None,
    ) -> dict[str, Any]:
        """Authenticate and bind `username` to `session`.

        `pre_attach_send`, if provided, builds a `ProtocolMessage` that is
        flushed to the socket *while we hold the session's send lock* and
        *before* the session is added to the broadcast-visible username
        index. This makes it safe to broadcast user-status updates from
        other threads concurrently: any USER_STATUS frame they try to push
        to this socket queues on `send_lock` and lands after the LOGIN
        response.

        If the same username was already online on another connection the
        previous session is kicked. Returns the user profile dict from the
        database.
        """

        if not username or not password:
            raise ProtocolError(
                ErrorCode.INVALID_FIELD,
                "username and password are required",
            )
        if not self.db.authenticate_user(username, password):
            raise ProtocolError(ErrorCode.AUTH_FAILED, "invalid username or password")

        user = self.db.get_user(username)
        if user is None:
            raise ProtocolError(ErrorCode.NOT_FOUND, f"user not found: {username}")

        previous: ClientSession | None = None
        with session.send_lock:
            if session.closed:
                raise ProtocolError(ErrorCode.SERVER_ERROR, "session closed before login completed")

            if pre_attach_send is not None:
                response = pre_attach_send(user)
                if not session.send(response):
                    raise ProtocolError(ErrorCode.SERVER_ERROR, "failed to send login response")

            with self._lock:
                previous = self._sessions_by_username.get(username)
                if previous is session:
                    previous = None
                session.username = username
                session.touch()
                self._sessions_by_username[username] = session

        if previous is not None and previous is not session:
            logger.info("kicking previous session for %s (new login)", username)
            previous.send(
                ProtocolMessage.from_dict(
                    {
                        "type": "error",
                        "sender": "server",
                        "payload": {
                            "error_code": ErrorCode.CONFLICT.value,
                            "message": "logged in from another connection",
                            "detail": None,
                        },
                    }
                )
            )
            self.remove_session(previous)

        try:
            self.db.set_user_status(username, "online")
        except Exception:
            logger.exception("failed to mark %s online", username)
        return user

    def logout(self, session: ClientSession) -> str | None:
        """Unbind a session's username without closing the socket."""

        username = session.username
        if username is None:
            return None
        with self._lock:
            bound = self._sessions_by_username.get(username)
            if bound is session:
                self._sessions_by_username.pop(username, None)
            session.username = None
        try:
            self.db.set_user_status(username, "offline")
        except Exception:
            logger.exception("failed to mark %s offline", username)
        return username

    # --- lookup -----------------------------------------------------------

    def get_session(self, username: str) -> ClientSession | None:
        with self._lock:
            return self._sessions_by_username.get(username)

    def is_online(self, username: str) -> bool:
        with self._lock:
            return username in self._sessions_by_username

    def online_users(self) -> list[str]:
        with self._lock:
            return sorted(self._sessions_by_username.keys())

    def all_sessions(self) -> list[ClientSession]:
        with self._lock:
            return list(self._sessions_by_id.values())

    def authenticated_sessions(self) -> list[ClientSession]:
        with self._lock:
            return list(self._sessions_by_username.values())

    # --- heartbeat --------------------------------------------------------

    def update_heartbeat(self, session: ClientSession) -> None:
        session.touch()

    def kick_session(self, session: ClientSession, reason: str) -> str | None:
        """Forcefully terminate a session.

        Sends a best-effort error frame, removes registry entries, and closes
        the socket. Idempotent — safe to call from the reader thread and the
        heartbeat thread concurrently.
        """

        logger.info("kicking session %s: %s", session.label, reason)
        session.send(
            ProtocolMessage.from_dict(
                {
                    "type": "error",
                    "sender": "server",
                    "payload": {
                        "error_code": ErrorCode.SERVER_ERROR.value,
                        "message": reason,
                        "detail": None,
                    },
                }
            )
        )
        return self.remove_session(session)
