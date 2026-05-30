import unittest

from server.ai_service import AIConfig, AIService, normalize_chat_completions_url


class AIServiceTest(unittest.TestCase):
    def test_normalize_base_url_accepts_v1_root(self) -> None:
        self.assertEqual(
            normalize_chat_completions_url("https://api.xiaomimimo.com/v1"),
            "https://api.xiaomimimo.com/v1/chat/completions",
        )

    def test_local_fallback_without_api_key(self) -> None:
        service = AIService(AIConfig(api_key=None))
        self.assertIn("hello", service.answer("hello"))


if __name__ == "__main__":
    unittest.main()
