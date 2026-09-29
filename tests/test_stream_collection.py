import unittest
from types import SimpleNamespace

import main


def chunk(text="", finish_reason=None):
    candidate = SimpleNamespace(finish_reason=finish_reason, content=None)
    return SimpleNamespace(text=text, candidates=[candidate])


class ClosableStream:
    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self.consumed = 0
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        self.consumed += 1
        return next(self._chunks)

    def close(self):
        self.closed = True


class StreamCollectionTest(unittest.TestCase):
    def test_reports_finish_reason_enum_name(self):
        reasons = []
        stream = ClosableStream(
            [
                chunk("partial"),
                chunk(" text", SimpleNamespace(name="MAX_TOKENS")),
            ]
        )

        text = main._collect_stream_text(
            stream, on_chunk=lambda delta: None, on_finish=reasons.append
        )

        self.assertEqual(text, "partial text")
        self.assertEqual(reasons, ["MAX_TOKENS"])
        self.assertTrue(stream.closed)

    def test_raising_on_chunk_stops_reading_and_closes_stream(self):
        stream = ClosableStream([chunk("a"), chunk("b"), chunk("c")])

        def on_chunk(delta):
            raise RuntimeError("cancelled")

        with self.assertRaises(RuntimeError):
            main._collect_stream_text(stream, on_chunk=on_chunk)

        self.assertEqual(stream.consumed, 1)
        self.assertTrue(stream.closed)

    def test_missing_finish_reason_is_empty(self):
        reasons = []
        main._collect_stream_text(
            [SimpleNamespace(text="hi")],
            on_chunk=lambda delta: None,
            on_finish=reasons.append,
        )
        self.assertEqual(reasons, [""])


if __name__ == "__main__":
    unittest.main()
