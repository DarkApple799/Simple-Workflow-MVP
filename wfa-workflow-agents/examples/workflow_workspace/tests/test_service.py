"""示例模块的单元测试。"""

import json
import unittest

from app.service import load_config, normalize_records, summarize


class NormalizeRecordsTest(unittest.TestCase):
    def test_valid_records(self):
        self.assertEqual(normalize_records(['{"value": 1}']), [{"value": 1}])

    def test_invalid_record_is_skipped(self):
        self.assertEqual(normalize_records(["not-json"]), [])


class SummarizeTest(unittest.TestCase):
    def test_total(self):
        self.assertEqual(summarize([{"value": 2}, {"value": 3}])["total"], 5)

    def test_count(self):
        self.assertEqual(summarize([])["count"], 0)


class LoadConfigTest(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(load_config("not-exists.json"), {})


if __name__ == "__main__":
    unittest.main()
