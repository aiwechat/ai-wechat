"""Heartbeat / dead-connection sweeper.

A background thread periodically inspects every live session's `last_seen`
timestamp. Any session that hasn't sent *anything* for more than
`timeout_seconds` is kicked. `UserManager.update_heartbeat()` is called for
every received frame (not just `heartbeat` envelopes), so an actively
chatting client is never wrongly evicted.
"""

from __future__ import annotations

import logging
import threading
import time

from server.user_manager import UserManager


logger = logging.getLogger(__name__)


class HeartbeatMonitor:
    def __init__(
        self,
        user_manager: UserManager,
        *,
        timeout_seconds: float = 60.0,
        interval_seconds: float = 15.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        self.users = user_manager
        self.timeout_seconds = timeout_seconds
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="HeartbeatMonitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, join_timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)
        self._thread = None

    def sweep_once(self) -> list[str]:
        """Run one timeout sweep immediately. Returns kicked session labels.

        Exposed for tests; the background thread calls the same body.
        """

        now = time.monotonic()
        kicked: list[str] = []
        for session in self.users.all_sessions():
            if session.closed:
                continue
            if now - session.last_seen > self.timeout_seconds:
                label = session.label
                self.users.kick_session(session, "heartbeat timeout")
                kicked.append(label)
        return kicked

    def _run(self) -> None:
        logger.debug(
            "heartbeat monitor started (timeout=%.1fs, interval=%.1fs)",
            self.timeout_seconds,
            self.interval_seconds,
        )
        while not self._stop_event.wait(self.interval_seconds):
            try:
                kicked = self.sweep_once()
                if kicked:
                    logger.info("heartbeat kicked %d session(s): %s", len(kicked), kicked)
            except Exception:
                logger.exception("heartbeat sweep failed")
        logger.debug("heartbeat monitor stopped")
