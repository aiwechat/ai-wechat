"""Unit tests for the command-line client parser."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import unittest

from client.ui import handle_line


class FakeHistory:
    def recent_private(self, peer: str, limit: int | None = None) -> list[object]:
        return []

    def recent_group(self, group_id: str, limit: int | None = None) -> list[object]:
        return []

    def format_items(self, items: list[object], *, current_user: str | None = None) -> list[str]:
        return []


class FakeState:
    def __init__(self) -> None:
        self.connected = False
        self.username = "alice"
        self.login_confirmed = False
        self.current_chat_type: str | None = None
        self.current_target: str | None = None
        self.groups: set[str] = set()
        self.user_status: dict[str, str] = {}


class FakeClient:
    def __init__(self) -> None:
        self.state = FakeState()
        self.history = FakeHistory()
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def connect(self, host: str | None = None, port: int | None = None) -> None:
        self.calls.append(("connect", (host, port)))

    def reconnect(self) -> None:
        self.calls.append(("reconnect", ()))

    def register(self, username: str, password: str) -> None:
        self.calls.append(("register", (username, password)))

    def login(self, username: str, password: str) -> None:
        self.calls.append(("login", (username, password)))

    def logout(self) -> None:
        self.calls.append(("logout", ()))

    def send_private(self, receiver: str, content: str) -> None:
        self.calls.append(("send_private", (receiver, content)))

    def send_group(self, group_id: str, content: str) -> None:
        self.calls.append(("send_group", (group_id, content)))

    def send_file(self, chat_type: str, target: str, path: str) -> None:
        self.calls.append(("send_file", (chat_type, target, path)))

    def recall_message(self, message_id: str) -> None:
        self.calls.append(("recall_message", (message_id,)))

    def create_group(self, name: str) -> None:
        self.calls.append(("create_group", (name,)))

    def join_group(self, group_id: str) -> None:
        self.calls.append(("join_group", (group_id,)))

    def leave_group(self, group_id: str) -> None:
        self.calls.append(("leave_group", (group_id,)))

    def request_private_history(self, peer: str, limit: int = 50) -> None:
        self.calls.append(("request_private_history", (peer, limit)))

    def request_group_history(self, group_id: str, limit: int = 50) -> None:
        self.calls.append(("request_group_history", (group_id, limit)))

    def heartbeat(self) -> None:
        self.calls.append(("heartbeat", ()))


class ClientUiTest(unittest.TestCase):
    def test_auth_commands_are_dispatched(self) -> None:
        client = FakeClient()

        with redirect_stdout(io.StringIO()):
            self.assertTrue(handle_line(client, "/register alice pw"))
            self.assertTrue(handle_line(client, "/login alice pw"))
            self.assertTrue(handle_line(client, "/logout"))

        self.assertEqual(
            client.calls,
            [
                ("register", ("alice", "pw")),
                ("login", ("alice", "pw")),
                ("logout", ()),
            ],
        )

    def test_chat_target_and_bare_text(self) -> None:
        client = FakeClient()

        with redirect_stdout(io.StringIO()):
            handle_line(client, "/chat private bob")
            self.assertEqual(client.state.current_chat_type, "private")
            self.assertEqual(client.state.current_target, "bob")

            handle_line(client, "hello bob")
            self.assertEqual(client.calls[-1], ("send_private", ("bob", "hello bob")))

            handle_line(client, "/chat group g1")
            handle_line(client, "hello group")
            self.assertEqual(client.calls[-1], ("send_group", ("g1", "hello group")))

    def test_group_and_history_commands_are_dispatched(self) -> None:
        client = FakeClient()

        with redirect_stdout(io.StringIO()):
            handle_line(client, "/create-group net-class")
            handle_line(client, "/join g1")
            handle_line(client, "/leave g1")
            handle_line(client, "/history private bob 20")
            handle_line(client, "/history group g1")

        self.assertEqual(
            client.calls,
            [
                ("create_group", ("net-class",)),
                ("join_group", ("g1",)),
                ("leave_group", ("g1",)),
                ("request_private_history", ("bob", 20)),
                ("request_group_history", ("g1", 50)),
            ],
        )

    def test_file_and_recall_commands_are_dispatched(self) -> None:
        client = FakeClient()

        with redirect_stdout(io.StringIO()):
            handle_line(client, "/send-file private bob C:\\tmp\\a.txt")
            handle_line(client, "/send-file group g1 C:\\tmp\\b.txt")
            handle_line(client, "/recall msg-1")

        self.assertEqual(
            client.calls,
            [
                ("send_file", ("private", "bob", "C:\\tmp\\a.txt")),
                ("send_file", ("group", "g1", "C:\\tmp\\b.txt")),
                ("recall_message", ("msg-1",)),
            ],
        )

    def test_invalid_commands_raise_value_error(self) -> None:
        client = FakeClient()

        with self.assertRaises(ValueError):
            handle_line(client, "/msg bob")
        with self.assertRaises(ValueError):
            handle_line(client, "/chat channel bob")
        with self.assertRaises(ValueError):
            handle_line(client, "text without target")

    def test_quit_returns_false(self) -> None:
        self.assertFalse(handle_line(FakeClient(), "/quit"))


if __name__ == "__main__":
    unittest.main()
