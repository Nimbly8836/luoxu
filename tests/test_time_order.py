"""Chronological search direction through real HTTP and isolated PostgreSQL."""

import datetime
from typing import Any

import test_topics as fixtures
import test_text_search as text_search_tests
from test_search_deleted import DeletedSearchFixture, PREFIX, UTC

from luoxu.auth import Principal
from luoxu.types import SearchQuery


class TimeOrderTests(DeletedSearchFixture):
  options = {"order": "asc"}

  async def search(self, params=None, **kwargs):
    return await super().search({**self.options, **(params or {})}, **kwargs)

  test_real_ascending_timeout_is_503 = text_search_tests.TextSearchTests.test_real_statement_timeout_is_503_without_fallback

  async def test_default_and_explicit_descending_preserve_messages(self):
    for mid in (1, 2, 3):
      await self.seed(mid, "needle")
    _, default = await super().search({"q": "needle"})
    _, descending = await self.search({"q": "needle", "order": "desc"})
    self.assertEqual(default["messages"], descending["messages"])
    self.assertEqual([r["id"] for r in default["messages"]], [3, 2, 1])
    self.assertNotIn("order", default)
    self.assertNotIn("ranking", default)
    self.assertEqual(descending["order"], "desc")
    self.assertEqual(descending["ranking"], "time")
    self.assertNotIn("score", descending["messages"][0])
    self.assertEqual(SearchQuery(0, None, None, None, None).order, "desc")

  async def test_ascending_selects_oldest_before_limit_and_combines_snapshots(self):
    for mid in range(1, 76):
      await self.seed(mid, "needle")
    # All timestamps tie: reversing the newest 50 would incorrectly yield 26..75.
    for deleted in (False, True):
      if deleted:
        await self.delete(*range(2, 76, 2))
      for query in ({}, {"q": ""}, {"q": "needle"}, {"q": "needle", "match": "literal"}):
        response, body = await self.search({**query, "include_deleted": str(deleted).lower()})
        self.assertEqual([r["id"] for r in body["messages"]], list(range(1, 51)))
        self.assertTrue(body["has_more"])
        self.assertNotIn("next_offset", body)
        self.assertEqual(body["order"], "asc")
        self.assertEqual(body["ranking"], "time")
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.assertEqual(response.headers["Vary"], "Authorization")
        if deleted:
          self.assertEqual([r["id"] for r in body["messages"] if r["deleted"]], list(range(2, 51, 2)))
          self.assertTrue(all(r["content_source"] == "delete_snapshot"
                              for r in body["messages"] if r["deleted"]))
    _, live_only = await self.search({"q": "needle"})
    self.assertEqual([r["id"] for r in live_only["messages"]], list(range(1, 76, 2)))

  async def test_original_creation_time_not_id_edit_or_deletion_time(self):
    await self.seed(900, "needle old", year=2024)
    await self.seed(1, "needle new", year=2025)
    await self.delete(900)
    for match in ("query", "literal"):
      _, body = await self.search({"q": "needle", "match": match,
                                  "start": "", "end": "", "include_deleted": "true"})
      self.assertEqual([r["id"] for r in body["messages"]], [900, 1])
      self.assertLess(body["messages"][0]["t"], body["messages"][1]["t"])
      self.assertGreater(body["messages"][0]["deleted_at"], body["messages"][1]["t"])
      _, filtered = await self.search({"q": "needle", "match": match, "include_deleted": "true"})
      self.assertEqual([r["id"] for r in filtered["messages"]], [1])

  async def test_time_pagination_in_both_directions(self):
    for mid in range(1, 76):
      await self.seed(mid, "needle")
    async with self.db.get_conn() as raw:
      conn = await self.db.archive_conn(raw, self.cid)
      await conn.execute("UPDATE {messages} SET created_at=created_at+msgid*interval '1 second'")
    await self.delete(*range(2, 76, 2))
    for match in ("query", "literal"):
      for order, bound, expected in (("asc", "start", list(range(1, 76))),
                                     ("desc", "end", list(range(75, 0, -1)))):
        params = {"q": "needle", "match": match, "order": order, "include_deleted": "true"}
        _, first = await self.search(params)
        _, second = await self.search({**params, bound: int(first["messages"][-1]["t"])})
        self.assertEqual([r["id"] for r in first["messages"] + second["messages"]], expected)
        self.assertFalse(second["has_more"])

  async def test_full_physical_key_ties_across_topics_and_snapshots(self):
    async with self.db.get_conn() as conn:
      topic = await self.fixture.seed_topic(conn, 42)
      await self.fixture.seed_message(conn, topic, 1, topic_id=42, text="needle topic")
    await self.seed(1, "needle root")
    # Same msgid and timestamp in different conversations: deterministic UUID tie.
    for deleted in (False, True):
      if deleted:
        await self.delete(1)
      for match in ("query", "literal"):
        _, body = await self.search({"q": "needle", "match": match,
                                    "include_deleted": str(deleted).lower()})
        self.assertEqual([r["conversation_id"] for r in body["messages"]],
                         sorted([str(self.cid), str(topic)]))

  async def test_acl_and_sender_exclusions_before_limit(self):
    await self.db.revoke_public(self.cid)
    user, headers = await self.user("chronology-reader")
    await self.seed(1, "needle parent secret")
    async with self.db.get_conn() as conn:
      topic = await self.fixture.seed_topic(conn, 42)
      hidden = await self.fixture.seed_topic(conn, 43)
      await self.fixture.seed_message(conn, hidden, 1, topic_id=43, text="needle hidden")
      for mid in range(1, 66):
        await self.fixture.seed_message(conn, topic, mid, topic_id=42, text="needle excluded")
      for mid in (100, 101):
        await self.fixture.seed_message(conn, topic, mid, topic_id=42, text="needle visible")
      archive = await self.db.archive_conn(conn, topic)
      # Fixed archive template (registry UUID only); conversation is a bound value.
      # pi-lens-ignore: python-sql-injection
      await archive.execute("""UPDATE {messages}
        SET from_user=CASE WHEN msgid=100 THEN 200 ELSE NULL END
        WHERE conversation_id=$1 AND msgid IN (100,101)""", topic)
    await self.db.grant_conversation(user["id"], topic)
    params = {"conversation_id": str(topic), "q": "needle", "exclude_sender": "100"}
    for match in ("query", "literal"):
      _, body = await self.search({**params, "match": match}, headers=headers)
      self.assertEqual([r["id"] for r in body["messages"]], [100, 101])
      self.assertEqual(body["groupinfo"], {})
      self.assertTrue(all(r["conversation_id"] == str(topic) for r in body["messages"]))
      _, included = await self.search({**params, "match": match, "sender": "100,200"}, headers=headers)
      self.assertEqual([r["id"] for r in included["messages"]], [100])
    await self.assert_denied({"g": fixtures.GROUP_ID, "q": "needle", "order": "asc"}, headers=headers)
    await self.db.revoke_conversation(user["id"], topic)
    await self.assert_denied({**params, "order": "asc"}, headers=headers)

  async def test_missing_snapshot_browsing_and_history_gates(self):
    await self.seed(1, "needle residual", deleted=datetime.datetime.now(UTC))
    _, body = await self.search({"include_deleted": "true"})
    self.assertEqual(body["messages"][0]["content_source"], "unavailable")
    self.assertIsNone(body["messages"][0]["html"])
    for match in ("query", "literal"):
      _, body = await self.search({"include_deleted": "true", "q": "needle", "match": match})
      self.assertEqual(body["messages"], [])
    disabled = await self.api_client(history_enabled=False)
    for client in (disabled, self.client):
      if client is self.client:
        self.db.history_enabled = False
      response = await client.get(PREFIX + "/search", params={
        "g": fixtures.GROUP_ID, "include_deleted": "true", "order": "asc"})
      self.assertEqual(response.status, 400)

  async def test_validation_and_unchanged_relevance_and_semantic(self):
    await self.seed(1, "needle")
    invalid: list[dict[str, Any]] = [
      {"order": value} for value in ("", "ASC", " desc", "ascending", "asc; SELECT 1")
    ]
    invalid += [{"order": "asc", "sort": "relevance"}, {"order": "asc", "mode": "semantic"},
                {"order": "asc", "offset": 1}]
    for options in invalid:
      response = await self.client.get(PREFIX + "/search", params={
        "g": fixtures.GROUP_ID, "q": "needle", **options})
      self.assertEqual(response.status, 400, (options, await response.text()))
      with self.assertRaises(ValueError):
        await self.db.search(SearchQuery(fixtures.GROUP_ID, "needle", None, None, None, **options), Principal(None, None))
    response = await self.client.get(PREFIX + "/search", params={"q": "needle", "order": "asc"})
    self.assertEqual(response.status, 400)
    _, old = await super().search({"q": "needle", "sort": "relevance"})
    _, explicit = await self.search({"q": "needle", "sort": "relevance", "order": "desc"})
    self.assertEqual(old, explicit)
    for options in ({}, {"order": "desc"}):
      response = await self.client.get(PREFIX + "/search", params={
        "g": fixtures.GROUP_ID, "q": "needle", "mode": "semantic", **options})
      self.assertEqual(response.status, 503)
      self.assertEqual((await response.json())["error"], "semantic search is not enabled")
