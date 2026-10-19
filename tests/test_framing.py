import unittest

from omo.host_client import JsonlBuffer


class TestJsonlBuffer(unittest.TestCase):
    def test_single_complete_line(self):
        buf = JsonlBuffer()
        self.assertEqual(buf.feed(b'{"a": 1}\n'), [{"a": 1}])

    def test_split_line(self):
        buf = JsonlBuffer()
        self.assertEqual(buf.feed(b'{"a"'), [])
        self.assertEqual(buf.feed(b': 1}\n'), [{"a": 1}])

    def test_multiple_lines_in_one_chunk(self):
        buf = JsonlBuffer()
        self.assertEqual(buf.feed(b'{"a": 1}\n{"b": 2}\n'), [{"a": 1}, {"b": 2}])

    def test_multibyte_character_split_across_chunks(self):
        data = '{"t": "çözgü"}\n'.encode("utf-8")
        buf = JsonlBuffer()
        out = []
        for i in range(len(data)):
            out.extend(buf.feed(data[i:i + 1]))
        self.assertEqual(out, [{"t": "çözgü"}])

    def test_blank_lines_are_ignored(self):
        buf = JsonlBuffer()
        self.assertEqual(buf.feed(b'\n\n{"a": 1}\n\n'), [{"a": 1}])


if __name__ == "__main__":
    unittest.main()
