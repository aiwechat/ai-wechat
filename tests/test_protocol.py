import unittest

from common.protocol import (
    MessageType,
    ProtocolError,
    ProtocolMessage,
    decode_frame,
    decode_frames,
    encode_frame,
)


class ProtocolTest(unittest.TestCase):
    def test_message_json_round_trip(self):
        message = ProtocolMessage(
            type=MessageType.PRIVATE_MSG,
            sender="alice",
            receiver="bob",
            payload={"content": "hello"},
        )

        parsed = ProtocolMessage.from_json(message.to_json())

        self.assertEqual(parsed.type, MessageType.PRIVATE_MSG)
        self.assertEqual(parsed.sender, "alice")
        self.assertEqual(parsed.receiver, "bob")
        self.assertEqual(parsed.payload["content"], "hello")

    def test_single_frame_round_trip(self):
        frame = encode_frame(
            ProtocolMessage(type=MessageType.HEARTBEAT, sender="alice", payload={"seq": 1})
        )

        message, remaining = decode_frame(frame)

        self.assertEqual(remaining, b"")
        self.assertIsNotNone(message)
        self.assertEqual(message.type, MessageType.HEARTBEAT)
        self.assertEqual(message.payload["seq"], 1)

    def test_multiple_frames(self):
        first = ProtocolMessage(type=MessageType.LOGIN, sender="alice")
        second = ProtocolMessage(type=MessageType.LOGOUT, sender="alice")

        messages, remaining = decode_frames(encode_frame(first) + encode_frame(second))

        self.assertEqual(remaining, b"")
        self.assertEqual([msg.type for msg in messages], [MessageType.LOGIN, MessageType.LOGOUT])

    def test_partial_frame_returns_none(self):
        frame = encode_frame(ProtocolMessage(type=MessageType.HEARTBEAT))

        message, remaining = decode_frame(frame[:6])

        self.assertIsNone(message)
        self.assertEqual(remaining, frame[:6])

    def test_invalid_json_raises_protocol_error(self):
        with self.assertRaises(ProtocolError):
            decode_frame(len(b"{bad").to_bytes(4, "big") + b"{bad")

    def test_unknown_message_type_raises_protocol_error(self):
        with self.assertRaises(ProtocolError):
            ProtocolMessage.from_json('{"type":"unknown","payload":{}}')


if __name__ == "__main__":
    unittest.main()

