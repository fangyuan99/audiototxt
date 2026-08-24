import unittest
from types import SimpleNamespace

from key_pool import GeminiKeyPool
from model_catalog import list_current_channel_models
from service_config import GlobalSettings


class FakeClient:
    def __init__(self, models):
        self.returned_models = models
        self.list_configs = []
        self.closed = False
        self.models = self

    def list(self, *, config=None):
        self.list_configs.append(config)
        return iter(self.returned_models)

    def close(self):
        self.closed = True


def fake_model(name, actions=()):
    return SimpleNamespace(name=name, supported_actions=list(actions))


class ModelCatalogTest(unittest.TestCase):
    def test_vertex_lists_base_models_with_global_and_normalizes_names(self):
        client = FakeClient(
            [
                fake_model("publishers/google/models/gemini-current"),
                fake_model("publishers/google/models/gemini-other"),
                fake_model("publishers/google/models/gemini-other"),
                fake_model("publishers/google/models/gemini-embedding-2"),
                fake_model("publishers/google/models/gemini-image-preview"),
                fake_model("publishers/google/models/gemini-tts"),
                fake_model("publishers/google/models/gemini-live-audio"),
                fake_model("publishers/google/models/text-bison"),
            ]
        )
        captured = {}

        def factory(config, timeout_seconds=None):
            captured["config"] = config
            captured["timeout"] = timeout_seconds
            return client

        models = list_current_channel_models(
            GlobalSettings(
                auth_mode="vertex_ai_json",
                vertex_json='{"project_id":"demo"}',
                vertex_project="demo",
                vertex_location="",
                model_name="gemini-current",
            ),
            GeminiKeyPool(),
            client_factory=factory,
        )

        self.assertEqual(models, ["gemini-current", "gemini-other"])
        self.assertEqual(captured["config"].vertex_location, "global")
        self.assertEqual(captured["timeout"], 20.0)
        self.assertEqual(
            client.list_configs,
            [{"page_size": 100, "query_base": True}],
        )
        self.assertTrue(client.closed)

    def test_gemini_uses_generate_content_models_and_key_pool_cache(self):
        clients = []

        def factory(config, timeout_seconds=None):
            client = FakeClient(
                [
                    fake_model("models/gemini-good", ["generateContent"]),
                    fake_model("models/gemini-count-only", ["countTokens"]),
                    fake_model("models/gemini-good-image", ["generateContent"]),
                ]
            )
            clients.append((config.api_key, client))
            return client

        settings = GlobalSettings(
            auth_mode="gemini_api_key",
            gemini_api_keys=["key-a", "key-b"],
        )
        pool = GeminiKeyPool()

        first = list_current_channel_models(
            settings,
            pool,
            client_factory=factory,
        )
        second = list_current_channel_models(
            settings,
            pool,
            client_factory=factory,
        )

        self.assertEqual(first, ["models/gemini-good"])
        self.assertEqual(second, first)
        self.assertEqual([item[0] for item in clients], ["key-a"])
        self.assertTrue(clients[0][1].closed)


if __name__ == "__main__":
    unittest.main()
