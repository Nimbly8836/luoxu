"""Search sender exclusion regression tests."""

import unittest
from unittest.mock import AsyncMock

from aiohttp import web

from luoxu.db import PostgreStore
from luoxu.types import SearchQuery
from luoxu.web import SearchHandler


class SearchExclusionTests(unittest.TestCase):
  def test_multiple_senders_and_whitespace(self):
    query = SearchHandler(None)._parse_query(
      {"g": "123", "sender": "123,456", "exclude_sender": " 456, 789 "}
    )
    self.assertEqual(query.sender, [123, 456])
    self.assertEqual(query.exclude_sender, [456, 789])

  def test_optional_filter(self):
    for params in ({"g": "123"}, {"g": "123", "exclude_sender": ""}):
      self.assertIsNone(SearchHandler(None)._parse_query(params).exclude_sender)
    self.assertIsNone(SearchQuery(0, None, None, None, None).exclude_sender)

  def test_invalid_id(self):
    with self.assertRaises(web.HTTPBadRequest):
      SearchHandler(None)._parse_query({"g": "123", "exclude_sender": "123,invalid"})


class SearchExclusionSQLTests(unittest.IsolatedAsyncioTestCase):
  async def test_exclusion_is_parameterized_before_limit(self):
    conn = AsyncMock()
    conn.fetch.return_value = []

    store = PostgreStore({"url": "unused"})
    query = SearchQuery(123, None, [123], None, None, exclude_sender=[123, 456])
    await store._search_keywords(conn, query, [])
    sql, *args = conn.fetch.call_args.args
    self.assertEqual(args[6], [123])
    self.assertEqual(args[8], [123, 456])
    self.assertIn("m.from_user IS NULL", sql)
    self.assertIn("NOT (m.from_user = ANY($9))", sql)
    self.assertLess(sql.index("ANY($9)"), sql.index("LIMIT"))


if __name__ == "__main__":
  unittest.main()
