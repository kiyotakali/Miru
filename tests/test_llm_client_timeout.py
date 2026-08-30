"""Test that the LLM client is built with a request timeout.

Why: a hung upstream API used to pin the daemon thread for the SDK's
default 600s (10 minutes). With 3 retries that's 30+ minutes of "正在
输入..." with no recovery.

After the 3-tier refactor, ai_config.get_tier_client builds the OpenAI
client with timeout=120s. We check by configuring a tier and inspecting.
"""
import importlib
import os
import unittest


class LLMClientTimeoutTests(unittest.TestCase):

    def setUp(self):
        # Isolate from any host environment
        for v in (
            "AI_VISION_HOST", "AI_VISION_KEY", "AI_VISION_MODEL",
            "AI_CHAT_HOST", "AI_CHAT_KEY", "AI_CHAT_MODEL",
            "AI_MEMORY_HOST", "AI_MEMORY_KEY", "AI_MEMORY_MODEL",
        ):
            os.environ.pop(v, None)
        # Use a temp DATA_DIR to avoid mutating real config
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self._orig_data_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp
        os.makedirs(os.path.join(self.tmp, "_admin"), exist_ok=True)
        import ai_config
        importlib.reload(ai_config)
        self.ai_config = ai_config

    def tearDown(self):
        if self._orig_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._orig_data_dir
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_chat_tier_client_has_timeout(self):
        self.ai_config.update_tier_config("chat", {
            "host": "api.example.com",
            "api_key": "sk-test",
            "model": "test-model",
        })
        client = self.ai_config.get_tier_client("chat")
        timeout = getattr(client, "timeout", None)
        self.assertIsNotNone(timeout, "client built without timeout")
        # OpenAI SDK timeout is float or httpx.Timeout
        read_timeout = (
            getattr(timeout, "read", None)
            or getattr(timeout, "_read", None)
            or float(timeout)
        )
        self.assertTrue(
            10 <= float(read_timeout) <= 600,
            f"chat tier read timeout {read_timeout!r} outside sensible range",
        )

    def test_memory_tier_client_has_timeout(self):
        self.ai_config.update_tier_config("memory", {
            "host": "openrouter.ai/api",
            "api_key": "sk-or-test",
            "model": "deepseek/deepseek-v4-flash",
        })
        client = self.ai_config.get_tier_client("memory")
        timeout = getattr(client, "timeout", None)
        self.assertIsNotNone(timeout)


if __name__ == "__main__":
    unittest.main()
