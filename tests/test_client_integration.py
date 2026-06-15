"""Integration tests for the real CLI client core against ChatServer."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import tempfile
import time
import unittest
from pathlib import Path

from client.client import ChatClient
from server.server import ChatServer
from tests._server_helpers import AllowAllModeration, free_port


def wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition was not met before timeout")


class ClientIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.server = ChatServer(
            host="127.0.0.1",
            port=free_port(),
            db_path=Path(self.tmpdir.name) / "chat.db",
            heartbeat_timeout=5.0,
            heartbeat_interval=0.5,
            recv_timeout=1.0,
            moderation=AllowAllModeration(),
        )
        self.port = self.server.start()

    def tearDown(self) -> None:
        self.server.stop(join_timeout=2.0)
        self.tmpdir.cleanup()

    def make_client(self) -> ChatClient:
        client = ChatClient("127.0.0.1", self.port)
        client.connect()
        return client

    def close_clients(self, *clients: ChatClient) -> None:
        for client in clients:
            client.disconnect()

    def test_register_login_and_online_status(self) -> None:
        with redirect_stdout(io.StringIO()):
            alice = self.make_client()
            bob = self.make_client()
            try:
                alice.register("alice", "alice-pw")
                alice.login("alice", "alice-pw")
                wait_until(lambda: alice.state.login_confirmed)

                bob.register("bob", "bob-pw")
                bob.login("bob", "bob-pw")
                wait_until(lambda: bob.state.login_confirmed)
                wait_until(lambda: alice.state.user_status.get("bob") == "online")

                self.assertEqual(alice.state.username, "alice")
                self.assertEqual(bob.state.username, "bob")
                self.assertEqual(bob.state.user_status.get("alice"), "online")
            finally:
                self.close_clients(alice, bob)

    def test_private_message_and_history_cache(self) -> None:
        with redirect_stdout(io.StringIO()):
            alice = self.make_client()
            bob = self.make_client()
            try:
                alice.register("alice", "alice-pw")
                bob.register("bob", "bob-pw")
                alice.login("alice", "alice-pw")
                bob.login("bob", "bob-pw")
                wait_until(lambda: alice.state.login_confirmed and bob.state.login_confirmed)

                alice.send_private("bob", "hello bob")
                wait_until(lambda: len(alice.history.recent_private("bob")) == 1)
                wait_until(lambda: len(bob.history.recent_private("alice")) == 1)

                self.assertEqual(alice.history.recent_private("bob")[0].content, "hello bob")
                self.assertEqual(bob.history.recent_private("alice")[0].content, "hello bob")

                bob.request_private_history("alice", 10)
                wait_until(lambda: len(bob.history.recent_private("alice")) == 1)
            finally:
                self.close_clients(alice, bob)

    def test_group_lifecycle_and_group_message(self) -> None:
        with redirect_stdout(io.StringIO()):
            alice = self.make_client()
            bob = self.make_client()
            try:
                alice.register("alice", "alice-pw")
                bob.register("bob", "bob-pw")
                alice.login("alice", "alice-pw")
                bob.login("bob", "bob-pw")
                wait_until(lambda: alice.state.login_confirmed and bob.state.login_confirmed)

                alice.create_group("net-class")
                wait_until(lambda: len(alice.state.groups) == 1)
                group_id = next(iter(alice.state.groups))

                bob.join_group(group_id)
                wait_until(lambda: group_id in bob.state.groups)

                alice.send_group(group_id, "hello group")
                wait_until(lambda: len(alice.history.recent_group(group_id)) == 1)
                wait_until(lambda: len(bob.history.recent_group(group_id)) == 1)
                self.assertEqual(bob.history.recent_group(group_id)[0].content, "hello group")

                bob.leave_group(group_id)
                wait_until(lambda: group_id not in bob.state.groups)
            finally:
                self.close_clients(alice, bob)

    def test_heartbeat_response_keeps_connection(self) -> None:
        with redirect_stdout(io.StringIO()):
            alice = self.make_client()
            try:
                alice.register("alice", "alice-pw")
                alice.login("alice", "alice-pw")
                wait_until(lambda: alice.state.login_confirmed)

                request_id = alice.heartbeat()
                self.assertIsInstance(request_id, str)
                wait_until(lambda: alice.state.connected)
            finally:
                self.close_clients(alice)


if __name__ == "__main__":
    unittest.main()
