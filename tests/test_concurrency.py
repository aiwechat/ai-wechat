"""Concurrency / load tests for the chat server.

Requirement: at least 50 concurrent clients. This file actually opens 60
real TCP sockets against a running `ChatServer`, has them log in and
exchange messages, and verifies every send was delivered.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from common.protocol import MessageType
from server.server import ChatServer
from tests._server_helpers import TestClient, free_port


NUM_CLIENTS = 60  # > 50 to satisfy the spec with margin


class ConcurrentClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "chat.db"
        # Heartbeat is generous so SQLite contention during the login storm
        # cannot trip the eviction sweep.
        self.server = ChatServer(
            host="127.0.0.1",
            port=free_port(),
            db_path=self.db_path,
            heartbeat_timeout=120.0,
            heartbeat_interval=10.0,
            recv_timeout=15.0,
        )
        self.port = self.server.start()

    def tearDown(self) -> None:
        self.server.stop(join_timeout=5.0)
        self.tmpdir.cleanup()

    def test_handles_60_concurrent_clients(self) -> None:
        # Pre-register users serially through the DB layer so the login storm
        # doesn't also include a registration storm.
        for i in range(NUM_CLIENTS):
            self.server.db.create_user(f"user{i:03d}", "shared-pw")

        start_barrier = threading.Barrier(NUM_CLIENTS)
        login_done_barrier = threading.Barrier(NUM_CLIENTS)
        results: list[object] = [None] * NUM_CLIENTS

        def worker(idx: int) -> None:
            username = f"user{idx:03d}"
            partner = f"user{(idx + 1) % NUM_CLIENTS:03d}"
            try:
                with TestClient("127.0.0.1", self.port, timeout=30.0) as client:
                    start_barrier.wait(timeout=15)
                    login_resp = client.login(username, "shared-pw")
                    self.assertEqual(login_resp.payload["username"], username)

                    # Hold here so every client is logged in before anyone
                    # tries to send — this guarantees the partner is online
                    # and the message will be forwarded immediately.
                    login_done_barrier.wait(timeout=30)

                    client.send_private(partner, f"hello from {username}")

                    # Each client expects two PRIVATE_MSG frames in any order:
                    # its own echo and the forward from its partner.
                    echo = None
                    forward = None
                    for _ in range(2):
                        msg = client.recv_of_type(
                            MessageType.PRIVATE_MSG,
                            max_messages=4 * NUM_CLIENTS,
                        )
                        if msg.meta.get("echo"):
                            echo = msg
                        else:
                            forward = msg
                    self.assertIsNotNone(echo, "missing echo for own send")
                    self.assertIsNotNone(forward, "missing forwarded message")
                    self.assertEqual(echo.payload["delivered"], True)
                    self.assertEqual(forward.sender, f"user{(idx - 1) % NUM_CLIENTS:03d}")
                    self.assertEqual(forward.payload["content"], f"hello from {forward.sender}")

                results[idx] = "ok"
            except Exception as exc:  # noqa: BLE001 - capture for assertion
                results[idx] = exc

        threads = [
            threading.Thread(target=worker, args=(i,), name=f"worker-{i}", daemon=True)
            for i in range(NUM_CLIENTS)
        ]
        t_start = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=90)
        elapsed = time.monotonic() - t_start

        failures = [(i, r) for i, r in enumerate(results) if r != "ok"]
        self.assertEqual(failures, [], f"workers failed: {failures}")

        # Sanity check: every user is recorded in the message history.
        sent_for_user = sum(
            1
            for i in range(NUM_CLIENTS)
            if self.server.db.get_private_history(
                f"user{i:03d}", f"user{(i + 1) % NUM_CLIENTS:03d}"
            )
        )
        self.assertEqual(sent_for_user, NUM_CLIENTS)

        # A trailing log line is handy when running the suite at -v.
        print(f"[concurrency] {NUM_CLIENTS} clients completed in {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()
