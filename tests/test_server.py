"""End-to-end tests for the chat server.

Each test spins up a real `ChatServer` bound to an ephemeral port, opens
real TCP sockets against it, and exchanges length-prefixed JSON frames.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from common.protocol import ErrorCode, MessageType, make_message
from server.server import ChatServer
from tests._server_helpers import TestClient, assert_no_error, free_port


class FakeAIService:
    assistant_name = "AI助手"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def answer(
        self,
        prompt: str,
        *,
        username: str | None = None,
        group_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> str:
        self.calls.append(
            {
                "prompt": prompt,
                "username": username,
                "group_id": group_id,
                "attachments": attachments or [],
            }
        )
        return f"AI reply to {username} in {group_id}: {prompt}"


class ServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "chat.db"
        self.ai_service = FakeAIService()
        self.server = ChatServer(
            host="127.0.0.1",
            port=free_port(),
            db_path=self.db_path,
            heartbeat_timeout=2.0,
            heartbeat_interval=0.5,
            recv_timeout=1.0,
        )
        self.port = self.server.start()

    def tearDown(self) -> None:
        self.server.stop(join_timeout=2.0)
        self.tmpdir.cleanup()

    def connect(self, **kwargs) -> TestClient:
        return TestClient("127.0.0.1", self.port, **kwargs)

    def _setup_users(self, *usernames: str) -> None:
        for name in usernames:
            with self.connect() as c:
                c.register(name, f"{name}-pw")


class AuthFlowTest(ServerTestCase):
    def test_register_then_login(self) -> None:
        with self.connect() as client:
            resp = client.register("alice", "alice-pw")
            assert_no_error(resp, "register")
            self.assertEqual(resp.payload["username"], "alice")

            login_resp = client.login("alice", "alice-pw")
            assert_no_error(login_resp, "login")
            self.assertEqual(login_resp.payload["username"], "alice")
            self.assertIn("alice", login_resp.payload["online_users"])

    def test_duplicate_register_returns_conflict(self) -> None:
        with self.connect() as client:
            client.register("alice", "alice-pw")
            client.send(
                make_message(
                    MessageType.REGISTER,
                    payload={"username": "alice", "password": "another"},
                )
            )
            resp = client.recv()
            self.assertEqual(resp.type, MessageType.ERROR)
            self.assertEqual(resp.payload["error_code"], ErrorCode.CONFLICT.value)

    def test_wrong_password_returns_auth_failed(self) -> None:
        with self.connect() as client:
            client.register("alice", "alice-pw")
            client.send(
                make_message(
                    MessageType.LOGIN,
                    payload={"username": "alice", "password": "wrong"},
                )
            )
            resp = client.recv()
            self.assertEqual(resp.type, MessageType.ERROR)
            self.assertEqual(resp.payload["error_code"], ErrorCode.AUTH_FAILED.value)

    def test_message_before_login_rejected(self) -> None:
        with self.connect() as client:
            client.send(
                make_message(
                    MessageType.PRIVATE_MSG,
                    receiver="bob",
                    payload={"content": "hi"},
                )
            )
            resp = client.recv()
            self.assertEqual(resp.type, MessageType.ERROR)
            self.assertEqual(resp.payload["error_code"], ErrorCode.AUTH_FAILED.value)

    def test_second_login_kicks_first_session(self) -> None:
        with self.connect() as first:
            first.register("alice", "alice-pw")
            first.login("alice", "alice-pw")

            with self.connect() as second:
                second.login("alice", "alice-pw")
                # First session should receive an error frame and then EOF.
                kick = first.recv_of_type(MessageType.ERROR)
                self.assertEqual(kick.payload["error_code"], ErrorCode.CONFLICT.value)


class PrivateMessageTest(ServerTestCase):
    def _register_and_login(self, client: TestClient, username: str) -> None:
        client.register(username, f"{username}-pw")
        client.login(username, f"{username}-pw")

    def test_private_message_is_forwarded_and_echoed(self) -> None:
        with self.connect() as alice, self.connect() as bob:
            self._register_and_login(alice, "alice")
            self._register_and_login(bob, "bob")

            alice.send_private("bob", "hello bob")

            forward = bob.recv_of_type(MessageType.PRIVATE_MSG)
            self.assertEqual(forward.sender, "alice")
            self.assertEqual(forward.payload["content"], "hello bob")
            self.assertNotIn("echo", forward.meta)

            echo = alice.recv_of_type(MessageType.PRIVATE_MSG)
            self.assertEqual(echo.meta.get("echo"), True)
            self.assertEqual(echo.payload["delivered"], True)

    def test_private_message_to_offline_user_persists(self) -> None:
        # alice connects; bob is registered but not connected
        with self.connect() as alice:
            self._register_and_login(alice, "alice")
            # register bob via a throwaway connection
            with self.connect() as throwaway:
                throwaway.register("bob", "bob-pw")

            alice.send_private("bob", "offline-msg")
            echo = alice.recv_of_type(MessageType.PRIVATE_MSG)
            self.assertEqual(echo.payload["delivered"], False)

        # Now bob connects and pulls history.
        with self.connect() as bob:
            bob.login("bob", "bob-pw")
            bob.send(
                make_message(
                    MessageType.HISTORY_REQUEST,
                    payload={"chat_type": "private", "peer": "alice", "limit": 10},
                )
            )
            history = bob.recv_of_type(MessageType.HISTORY_RESPONSE)
            contents = [m["content"] for m in history.payload["messages"]]
            self.assertIn("offline-msg", contents)

    def test_private_message_to_unknown_receiver(self) -> None:
        with self.connect() as alice:
            self._register_and_login(alice, "alice")
            alice.send_private("ghost", "are you there")
            resp = alice.recv()
            self.assertEqual(resp.type, MessageType.ERROR)
            self.assertEqual(resp.payload["error_code"], ErrorCode.NOT_FOUND.value)


class GroupChatTest(ServerTestCase):
    def test_create_join_and_broadcast(self) -> None:
        self._setup_users("alice", "bob", "carol")

        with self.connect() as alice, self.connect() as bob, self.connect() as carol:
            alice.login("alice", "alice-pw")
            bob.login("bob", "bob-pw")
            carol.login("carol", "carol-pw")

            alice.send(
                make_message(
                    MessageType.CREATE_GROUP,
                    payload={"name": "net-class", "group_id": "g1"},
                )
            )
            create_resp = alice.recv_of_type(MessageType.CREATE_GROUP)
            self.assertEqual(create_resp.payload["group_id"], "g1")

            for client in (bob, carol):
                client.send(make_message(MessageType.JOIN_GROUP, payload={"group_id": "g1"}))
                client.recv_of_type(MessageType.JOIN_GROUP)

            # Alice should receive notifications about bob and carol joining.
            joiners = []
            for _ in range(2):
                msg = alice.recv_of_type(MessageType.JOIN_GROUP)
                joiners.append(msg.payload["username"])
            self.assertCountEqual(joiners, ["bob", "carol"])

            alice.send(
                make_message(
                    MessageType.GROUP_MSG,
                    group_id="g1",
                    payload={"content": "hi group"},
                )
            )

            # Every member (including alice) should receive the message.
            for client in (alice, bob, carol):
                forward = client.recv_of_type(MessageType.GROUP_MSG)
                self.assertEqual(forward.payload["content"], "hi group")
                self.assertEqual(forward.sender, "alice")
                self.assertEqual(forward.group_id, "g1")

    def test_non_member_cannot_send_group_message(self) -> None:
        self._setup_users("alice", "outsider")
        with self.connect() as alice:
            alice.login("alice", "alice-pw")
            alice.send(
                make_message(
                    MessageType.CREATE_GROUP,
                    payload={"name": "private-club", "group_id": "g2"},
                )
            )
            alice.recv_of_type(MessageType.CREATE_GROUP)

        with self.connect() as outsider:
            outsider.login("outsider", "outsider-pw")
            outsider.send(
                make_message(
                    MessageType.GROUP_MSG,
                    group_id="g2",
                    payload={"content": "intrusion attempt"},
                )
            )
            resp = outsider.recv()
            self.assertEqual(resp.type, MessageType.ERROR)
            self.assertEqual(resp.payload["error_code"], ErrorCode.AUTH_FAILED.value)

    def test_bad_group_message_is_blocked_with_warning(self) -> None:
        self._setup_users("alice", "bob")
        with self.connect() as alice, self.connect() as bob:
            alice.login("alice", "alice-pw")
            bob.login("bob", "bob-pw")
            alice.send(
                make_message(
                    MessageType.CREATE_GROUP,
                    payload={"name": "moderated", "group_id": "mod-g1"},
                )
            )
            alice.recv_of_type(MessageType.CREATE_GROUP)
            bob.send(make_message(MessageType.JOIN_GROUP, payload={"group_id": "mod-g1"}))
            bob.recv_of_type(MessageType.JOIN_GROUP)

            alice.send(
                make_message(
                    MessageType.GROUP_MSG,
                    group_id="mod-g1",
                    payload={"content": "这里包含违规词1"},
                )
            )

            warning = alice.recv_of_type(MessageType.MODERATION_WARNING)
            self.assertEqual(warning.payload["action"], "block")
            self.assertIn("违规词1", warning.payload["matched_words"])
            self.assertEqual(self.server.db.get_group_history("mod-g1"), [])

    def test_group_image_attachment_is_forwarded_and_persisted(self) -> None:
        self._setup_users("alice", "bob")
        attachment = {
            "kind": "image",
            "mime": "image/png",
            "name": "tiny.png",
            "size": 68,
            "data": "data:image/png;base64,iVBORw0KGgo=",
        }
        with self.connect() as alice, self.connect() as bob:
            alice.login("alice", "alice-pw")
            bob.login("bob", "bob-pw")
            alice.send(
                make_message(
                    MessageType.CREATE_GROUP,
                    payload={"name": "media-room", "group_id": "media-g1"},
                )
            )
            alice.recv_of_type(MessageType.CREATE_GROUP)
            bob.send(make_message(MessageType.JOIN_GROUP, payload={"group_id": "media-g1"}))
            bob.recv_of_type(MessageType.JOIN_GROUP)

            alice.send(
                make_message(
                    MessageType.GROUP_MSG,
                    group_id="media-g1",
                    payload={"attachment": attachment},
                )
            )

            forward = bob.recv_of_type(MessageType.GROUP_MSG)
            self.assertEqual(forward.payload["content"], "")
            self.assertEqual(forward.payload["attachment"]["kind"], "image")
            history = self.server.db.get_group_history("media-g1")
            self.assertEqual(history[-1]["payload"]["attachment"]["name"], "tiny.png")


class AIFeatureTest(ServerTestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "chat.db"
        self.ai_service = FakeAIService()
        self.server = ChatServer(
            host="127.0.0.1",
            port=free_port(),
            db_path=self.db_path,
            heartbeat_timeout=2.0,
            heartbeat_interval=0.5,
            recv_timeout=1.0,
            ai_service=self.ai_service,
            ai_cooldown_seconds=0.0,
        )
        self.port = self.server.start()

    def test_group_ai_mention_sends_async_assistant_reply(self) -> None:
        self._setup_users("alice", "bob")
        with self.connect() as alice, self.connect() as bob:
            alice.login("alice", "alice-pw")
            bob.login("bob", "bob-pw")
            alice.send(
                make_message(
                    MessageType.CREATE_GROUP,
                    payload={"name": "ai-room", "group_id": "ai-g1"},
                )
            )
            alice.recv_of_type(MessageType.CREATE_GROUP)
            bob.send(make_message(MessageType.JOIN_GROUP, payload={"group_id": "ai-g1"}))
            bob.recv_of_type(MessageType.JOIN_GROUP)

            alice.send(
                make_message(
                    MessageType.GROUP_MSG,
                    group_id="ai-g1",
                    payload={"content": "@AI 请解释一下 TCP 和 UDP 的区别"},
                )
            )

            user_message = bob.recv_of_type(MessageType.GROUP_MSG)
            self.assertEqual(user_message.sender, "alice")
            self.assertEqual(user_message.payload["content"], "@AI 请解释一下 TCP 和 UDP 的区别")

            ai_reply = bob.recv_of_type(MessageType.GROUP_MSG)
            self.assertEqual(ai_reply.sender, "AI助手")
            self.assertTrue(ai_reply.payload["ai"])
            self.assertIn("TCP 和 UDP", ai_reply.payload["content"])

    def test_group_ai_mention_passes_image_attachment_to_ai_service(self) -> None:
        self._setup_users("alice", "bob")
        attachment = {
            "kind": "image",
            "mime": "image/png",
            "name": "diagram.png",
            "size": 68,
            "data": "data:image/png;base64,iVBORw0KGgo=",
        }
        with self.connect() as alice, self.connect() as bob:
            alice.login("alice", "alice-pw")
            bob.login("bob", "bob-pw")
            alice.send(
                make_message(
                    MessageType.CREATE_GROUP,
                    payload={"name": "ai-room", "group_id": "ai-image-g1"},
                )
            )
            alice.recv_of_type(MessageType.CREATE_GROUP)
            bob.send(make_message(MessageType.JOIN_GROUP, payload={"group_id": "ai-image-g1"}))
            bob.recv_of_type(MessageType.JOIN_GROUP)

            alice.send(
                make_message(
                    MessageType.GROUP_MSG,
                    group_id="ai-image-g1",
                    payload={"content": "@AI 看看这张图", "attachment": attachment},
                )
            )

            bob.recv_of_type(MessageType.GROUP_MSG)
            bob.recv_of_type(MessageType.GROUP_MSG)
            self.assertEqual(self.ai_service.calls[-1]["attachments"][0]["name"], "diagram.png")


class HeartbeatTest(ServerTestCase):
    def test_heartbeat_echo(self) -> None:
        with self.connect() as alice:
            alice.register("alice", "alice-pw")
            alice.login("alice", "alice-pw")
            alice.send(make_message(MessageType.HEARTBEAT, payload={"seq": 7}))
            pong = alice.recv_of_type(MessageType.HEARTBEAT)
            self.assertEqual(pong.payload["seq"], 7)

    def test_idle_connection_is_evicted(self) -> None:
        # Heartbeat timeout in setUp is 2 seconds; wait long enough that the
        # sweeper kicks the idle connection.
        with self.connect(timeout=5.0) as alice:
            alice.register("alice", "alice-pw")
            alice.login("alice", "alice-pw")
            # Don't send anything; wait past the heartbeat timeout.
            time.sleep(3.0)
            # Server should have closed the socket; subsequent recv returns EOF
            # (possibly after an ERROR frame).
            messages = []
            try:
                while True:
                    messages.append(alice.recv())
            except (EOFError, OSError):
                pass
            # At least one error frame is expected before EOF.
            error_codes = [m.payload.get("error_code") for m in messages if m.type == MessageType.ERROR]
            self.assertTrue(any(code == ErrorCode.SERVER_ERROR.value for code in error_codes))


class OnlineStatusTest(ServerTestCase):
    def test_status_broadcast_when_someone_logs_in(self) -> None:
        with self.connect() as alice, self.connect() as bob:
            alice.register("alice", "alice-pw")
            alice.login("alice", "alice-pw")
            bob.register("bob", "bob-pw")
            bob.login("bob", "bob-pw")

            # Alice should receive a USER_STATUS for bob coming online.
            status = alice.recv_of_type(MessageType.USER_STATUS)
            self.assertEqual(status.payload, {"username": "bob", "status": "online"})


if __name__ == "__main__":
    unittest.main()
