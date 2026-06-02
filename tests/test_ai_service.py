import unittest

from server.ai_service import AIConfig, AIService, build_user_content, normalize_chat_completions_url


class AIServiceTest(unittest.TestCase):
    def test_normalize_base_url_accepts_v1_root(self) -> None:
        self.assertEqual(
            normalize_chat_completions_url("https://api.xiaomimimo.com/v1"),
            "https://api.xiaomimimo.com/v1/chat/completions",
        )

    def test_local_fallback_without_api_key(self) -> None:
        service = AIService(AIConfig(api_key=None))
        self.assertIn("hello", service.answer("hello"))

    def test_build_user_content_includes_image_url(self) -> None:
        content = build_user_content(
            "describe this",
            [
                {
                    "kind": "image",
                    "mime": "image/png",
                    "data": "data:image/png;base64,abc",
                }
            ],
        )
        self.assertIsInstance(content, list)
        self.assertEqual(content[0], {"type": "text", "text": "describe this"})
        self.assertEqual(content[1]["type"], "image_url")
        self.assertEqual(content[1]["image_url"]["url"], "data:image/png;base64,abc")


if __name__ == "__main__":
    unittest.main()
