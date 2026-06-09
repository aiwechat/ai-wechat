import tempfile
import unittest
from pathlib import Path

from server.database import init_db


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = init_db(Path(self.tmpdir.name) / "chat.db")
        self.db.create_user("alice", "alice-password")
        self.db.create_user("bob", "bob-password")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_register_duplicate_and_login(self):
        self.assertTrue(self.db.authenticate_user("alice", "alice-password"))
        self.assertFalse(self.db.authenticate_user("alice", "wrong-password"))

        with self.assertRaises(ValueError):
            self.db.create_user("alice", "another-password")

    def test_group_lifecycle(self):
        group = self.db.create_group("network-class", "alice", group_id="g1")
        self.assertEqual(group["group_id"], "g1")

        self.db.join_group("g1", "bob")
        members = self.db.list_group_members("g1")
        self.assertEqual([member["username"] for member in members], ["alice", "bob"])

        self.db.leave_group("g1", "bob")
        members = self.db.list_group_members("g1")
        self.assertEqual([member["username"] for member in members], ["alice"])

    def test_private_message_history(self):
        self.db.save_message(
            message_type="private_msg",
            sender="alice",
            receiver="bob",
            content="hello",
            payload={"content": "hello"},
        )

        history = self.db.get_private_history("alice", "bob")

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["sender"], "alice")
        self.assertEqual(history[0]["payload"]["content"], "hello")

    def test_group_message_history(self):
        self.db.create_group("network-class", "alice", group_id="g1")
        self.db.save_message(
            message_type="group_msg",
            sender="alice",
            group_id="g1",
            content="hello group",
            payload={"content": "hello group"},
        )

        history = self.db.get_group_history("g1")

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["group_id"], "g1")

    def test_file_transfer_status(self):
        transfer = self.db.create_file_transfer(
            sender="alice",
            receiver="bob",
            filename="test.pdf",
            filesize=1024,
            file_id="file-1",
        )
        self.assertEqual(transfer["status"], "started")

        updated = self.db.update_file_transfer("file-1", status="finished", offset=1024)

        self.assertEqual(updated["status"], "finished")
        self.assertEqual(updated["offset"], 1024)

    def test_recalled_message_hides_content_in_history(self):
        record = self.db.save_message(
            message_type="private_msg",
            sender="alice",
            receiver="bob",
            content="remove me",
            payload={"content": "remove me"},
        )

        recalled = self.db.recall_message(record["message_id"], "alice")
        history = self.db.get_private_history("alice", "bob")

        self.assertEqual(recalled["payload"]["recalled"], True)
        self.assertEqual(history[0]["content"], "")
        self.assertEqual(history[0]["payload"]["recalled"], True)


if __name__ == "__main__":
    unittest.main()

