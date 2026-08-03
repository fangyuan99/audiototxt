import unittest

from key_pool import (
    DeterministicGeminiError,
    GeminiKeyPool,
    KeyPoolExhausted,
    mask_key,
)


class FakeClock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeHttpError(RuntimeError):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


class GeminiKeyPoolTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def test_round_robin_uses_each_healthy_key(self):
        pool = GeminiKeyPool(["key-a", "key-b"], clock=self.clock)
        self.assertEqual(
            [pool.acquire().key for _ in range(4)],
            ["key-a", "key-b", "key-a", "key-b"],
        )

    def test_auth_failure_disables_key_and_tries_next(self):
        pool = GeminiKeyPool(["bad-key", "good-key"], clock=self.clock)
        calls = []

        def operation(key):
            calls.append(key)
            if key == "bad-key":
                raise FakeHttpError(401, "invalid API key")
            return "ok"

        self.assertEqual(pool.run(operation), "ok")
        self.assertEqual(calls, ["bad-key", "good-key"])
        statuses = {item.masked_key: item.state for item in pool.statuses()}
        self.assertEqual(statuses[mask_key("bad-key")], "disabled")

    def test_rate_limit_cools_key_then_recovers(self):
        pool = GeminiKeyPool(["key-a", "key-b"], clock=self.clock)

        def operation(key):
            if key == "key-a":
                raise FakeHttpError(429, "quota exhausted")
            return key

        self.assertEqual(pool.run(operation), "key-b")
        self.assertEqual(pool.acquire().key, "key-b")
        self.clock.advance(3601)
        self.assertEqual(pool.acquire().key, "key-a")

    def test_deterministic_error_does_not_rotate(self):
        pool = GeminiKeyPool(["key-a", "key-b"], clock=self.clock)
        calls = []

        def operation(key):
            calls.append(key)
            raise FakeHttpError(400, "invalid media content")

        with self.assertRaises(DeterministicGeminiError):
            pool.run(operation)
        self.assertEqual(calls, ["key-a"])

    def test_sync_reenables_changed_configuration(self):
        pool = GeminiKeyPool(["bad-key"], clock=self.clock)
        with self.assertRaises(KeyPoolExhausted):
            pool.run(lambda key: (_ for _ in ()).throw(FakeHttpError(401, "bad")))

        pool.sync(["bad-key", "new-key"])
        self.assertEqual(pool.acquire().key, "new-key")

    def test_model_listing_is_cached_and_invalidated_by_key_change(self):
        pool = GeminiKeyPool(["key-a"], clock=self.clock, model_cache_ttl=600)
        calls = []

        def list_models(key):
            calls.append(key)
            return ["models/z", "models/a", "models/a"]

        self.assertEqual(pool.list_models(list_models), ["models/a", "models/z"])
        self.assertEqual(pool.list_models(list_models), ["models/a", "models/z"])
        self.assertEqual(calls, ["key-a"])

        pool.sync(["key-b"])
        self.assertEqual(pool.list_models(list_models), ["models/a", "models/z"])
        self.assertEqual(calls, ["key-a", "key-b"])

    def test_mask_key_never_returns_full_short_key(self):
        self.assertEqual(mask_key("abc"), "***")
        self.assertEqual(mask_key("abcdefghijk"), "abcd...hijk")


if __name__ == "__main__":
    unittest.main()
