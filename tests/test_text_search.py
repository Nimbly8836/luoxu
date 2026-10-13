"""Database-only text search through real HTTP and disposable PGroonga tables."""

import asyncio
import datetime
import unittest
from html import escape
from unittest.mock import AsyncMock

import asyncpg
import test_topics as fixtures
import test_search_deleted as deleted
from test_search_deleted import PREFIX, UTC

from luoxu.auth import Principal
from luoxu.indexing import _query_transform
from luoxu.text_search import set_text_search_timeout
from luoxu.types import SearchQuery


@unittest.skipUnless(fixtures.DATABASE_URL, "set LUOXU_TEST_DATABASE_URL")
class TextSearchTests(deleted.DeletedSearchFixture):
  options = {"sort": "relevance"}

  async def search(self, params=None, **kwargs):
    return await super().search({**self.options, **(params or {})}, **kwargs)

  async def assert_denied(self, params, **kwargs):
    await super().assert_denied({**self.options, **params}, **kwargs)

  # Exercise existing real authorization/snapshot regressions through the new
  # database path, not just the unchanged legacy keyword implementation.
  test_scope_is_required_and_conflicting_archive_is_rejected = deleted.DeletedSearchTests.test_scope_is_required_and_conflicting_archive_is_rejected
  test_grants_and_revocation = deleted.DeletedSearchTests.test_public_personal_grants_admin_non_bypass_and_immediate_revocation
  test_topic_grants = deleted.DeletedSearchTests.test_topic_only_grant_does_not_expose_parent_or_sibling_snapshots
  test_private_grants = deleted.DeletedSearchTests.test_private_chat_snapshots_require_an_individual_grant
  test_sender_filters = deleted.DeletedSearchTests.test_sender_group_conversation_and_time_filters_apply_before_limit
  test_latest_delete_snapshot = deleted.DeletedSearchTests.test_latest_delete_snapshot_wins_and_edit_revisions_are_not_searchable
  test_each_physical_key_snapshot = deleted.DeletedSearchTests.test_each_saved_year_uses_its_own_delete_snapshot
  test_concurrent_delete = deleted.DeletedSearchTests.test_concurrent_deletion_does_not_duplicate_a_search_result

  async def test_global_ranking_offset_and_no_model_calls(self):
    embedder, reranker = AsyncMock(), AsyncMock()
    self.db.embedder, self.db.reranker = embedder, reranker
    for mid in range(1, 62):
      await self.seed(mid, "needle needle needle" if mid == 1 else "needle")
    async with self.db.get_conn() as conn:
      archive = await self.db.archive_conn(conn, self.cid)
      await archive.execute("UPDATE {messages} SET created_at = created_at - interval '1 hour' WHERE msgid=1")
    await self.delete(*range(2, 62, 2))
    response, first = await self.search({"q": "needle", "include_deleted": "true"})
    self.assertEqual([r["id"] for r in first["messages"]], [1, *range(61, 12, -1)])
    self.assertEqual([r["score"] for r in first["messages"]], [3] + [1] * 49)
    self.assertEqual(first["next_offset"], 50)
    self.assertTrue(first["has_more"])
    self.assertEqual(first["ranking"], "database_lexical")
    self.assertEqual(first["score_kind"], "literal_occurrences" if self.options.get("match") == "literal" else "pgroonga_hit_spans")
    _, second = await self.search({"q": "needle", "include_deleted": "true", "offset": 50})
    self.assertEqual([r["id"] for r in second["messages"]], list(range(12, 1, -1)))
    self.assertFalse(second["has_more"])
    self.assertIsNone(second["next_offset"])
    _, bound = await self.search({"q": "needle", "offset": 1000})
    self.assertEqual(bound["messages"], [])
    self.assertEqual(response.headers["Cache-Control"], "private, no-store")
    self.assertEqual(response.headers["Vary"], "Authorization")
    embedder.embed.assert_not_called()
    reranker.rerank.assert_not_called()

  async def test_full_key_ties_and_open_time_boundaries(self):
    async with self.db.get_conn() as conn:
      topic = await self.fixture.seed_topic(conn, 42)
      await self.fixture.seed_message(conn, topic, 1, topic_id=42, text="needle")
    await self.seed(1, "needle")
    await self.seed(1, "needle", year=2024)
    _, body = await self.search({"q": "needle", "start": "1704067200"})
    self.assertEqual([r["conversation_id"] for r in body["messages"][:2]],
                     sorted([str(self.cid), str(topic)], reverse=True))
    self.assertEqual(len(body["messages"]), 3)
    boundary = int(datetime.datetime(2025, 1, 2, tzinfo=UTC).timestamp())
    for field in ("start", "end"):
      _, body = await self.search({"q": "needle", field: boundary})
      self.assertEqual(body["messages"], [])

  async def test_missing_snapshot_never_reads_residual_or_edit(self):
    await self.seed(1, "needle residual", deleted=datetime.datetime.now(UTC))
    async with self.db.get_conn() as conn:
      await conn.execute("""INSERT INTO message_revisions
        (conversation_id,msgid,revision_type,text,from_user_name,created_at)
        VALUES ($1,1,'edit','needle edit','Sender',$2)""",
        self.cid, datetime.datetime(2025, 1, 2, tzinfo=UTC))
    _, body = await self.search({"q": "needle", "include_deleted": "true"})
    self.assertEqual(body["messages"], [])

  async def test_history_gate_and_combined_sentinel(self):
    for mid in range(1, 51):
      await self.seed(mid, "needle")
    await self.delete(*range(2, 51, 2))
    _, body = await self.search({"q": "needle", "include_deleted": "true"})
    self.assertEqual(len(body["messages"]), 50)
    self.assertFalse(body["has_more"])
    disabled = await self.api_client(history_enabled=False)
    for client in (disabled, self.client):
      if client is self.client:
        self.db.history_enabled = False
      response = await client.get(PREFIX + "/search", params={
        "g": fixtures.GROUP_ID, "q": "needle", "include_deleted": "true", **self.options})
      self.assertEqual(response.status, 400)

  async def test_invalid_options_http_and_direct_store(self):
    invalid = [
      {"match": "bad"}, {"sort": "bad"}, {"sort": "time", "offset": 1},
      {"offset": -1}, {"offset": 1001}, {"q": ""}, {"q": "\0"},
      {"q": "a" * 2001}, {"mode": "semantic", "sort": "relevance"},
      {"mode": "semantic", "match": "literal"}, {"min_score": "0.5"},
    ]
    for change in invalid:
      params = {"q": "needle", **self.options, **change}
      response = await self.client.get(PREFIX + "/search", params={"g": fixtures.GROUP_ID, **params})
      self.assertEqual(response.status, 400, (params, await response.text()))
      kwargs = {k: v for k, v in params.items() if k != "q"}
      if "min_score" in kwargs:
        kwargs["min_score"] = float(kwargs["min_score"])
      with self.assertRaises(ValueError):
        await self.db.search(SearchQuery(fixtures.GROUP_ID, params["q"], None, None, None, **kwargs), Principal(None, None))
    invalid_queries = [{}, {"q": " "}]
    if _query_transform is not None:
      invalid_queries.append({"q": "()"})
    for extra in invalid_queries:
      response = await self.client.get(PREFIX + "/search", params={"g": fixtures.GROUP_ID, "sort": "relevance", **extra})
      self.assertEqual(response.status, 400)
    for options in ({"sort": "relevance"}, {"match": "literal"}):
      response = await self.client.get(PREFIX + "/search", params={"q": "needle", **options})
      self.assertEqual(response.status, 400)
      with self.assertRaises(ValueError):
        await self.db.search(SearchQuery(0, "needle", None, None, None,
                                              match=options.get("match", "query"),
                                              sort=options.get("sort", "time")), Principal(None, None))

  async def test_maximum_query_length_and_semantic_defaults(self):
    text = "x" * 2000
    await self.seed(1, text)
    _, body = await self.search({"q": text})
    self.assertEqual(body["messages"][0]["score"], 1)
    # The new parameters' defaults are accepted without enabling a disabled
    # semantic service or changing its established unavailable response.
    for options in ({}, {"match": "query", "sort": "time"}):
      response = await self.client.get(PREFIX + "/search", params={
        "g": fixtures.GROUP_ID, "q": "needle", "mode": "semantic", **options})
      self.assertEqual(response.status, 503)
      self.assertEqual((await response.json())["error"], "semantic search is not enabled")

  async def test_timeout_budget_is_local_and_preserves_stricter_owner(self):
    async with self.db.get_conn() as conn:
      original = await conn.fetchval("SHOW statement_timeout")
    for setting, expected in (("0", "10s"), ("20s", "10s"), ("250ms", "250ms")):
      async with self.db.get_conn() as conn:
        await conn.fetchval("SELECT set_config('statement_timeout',$1,true)", setting)
        await set_text_search_timeout(conn)
        self.assertEqual(await conn.fetchval("SHOW statement_timeout"), expected)
    async with self.db.get_conn() as conn:
      self.assertEqual(await conn.fetchval("SHOW statement_timeout"), original)

  async def test_real_statement_timeout_is_503_without_fallback(self):
    await self.seed(1, "needle")
    pool = self.db.pool
    assert pool is not None
    await pool.close()
    self.db.pool = await asyncpg.create_pool(fixtures.DATABASE_URL, min_size=1, max_size=2,
      server_settings={"search_path": f"{self.fixture.schema},public", "statement_timeout": "100ms"})
    async with self.fixture.admin.transaction():
      # An exclusive lock in this disposable schema forces the real SELECT to
      # wait beyond the owner's stricter budget, without artificial SQL mocks.
      await self.fixture.admin.execute(f'LOCK TABLE "{self.fixture.schema}".{self.fixture.messages} IN ACCESS EXCLUSIVE MODE')
      response = await asyncio.wait_for(self.client.get(PREFIX + "/search", params={
        "g": fixtures.GROUP_ID, "q": "needle", **self.options}), timeout=3)
      self.assertEqual(response.status, 503, await response.text())
      self.assertIn("timed out", (await response.json())["error"])
      self.assertEqual(response.headers["Vary"], "Authorization")
    _, body = await self.search({"q": "needle"})
    self.assertEqual([r["id"] for r in body["messages"]], [1])

  async def test_normal_and_forced_sequential_scores_agree(self):
    for mid, text in enumerate(("test", "test test test", "TEST", "test excluded", "other"), 1):
      await self.seed(mid, text)
    queries = [("test", [2, 4, 3, 1], [3, 1, 1, 1])]
    if _query_transform is not None:
      queries += [("test -excluded", [2, 3, 1], [3, 1, 1]),
                  ("-excluded", [5, 3, 2, 1], [0, 0, 0, 0])]
    for query, ids, scores in queries:
      for settings in ({}, {"enable_indexscan": "off", "enable_indexonlyscan": "off", "enable_bitmapscan": "off"}):
        pool = self.db.pool
        assert pool is not None
        await pool.close()
        self.db.pool = await asyncpg.create_pool(fixtures.DATABASE_URL, min_size=1, max_size=2,
          server_settings={"search_path": f"{self.fixture.schema},public", **settings})
        _, body = await self.search({"q": query, "match": "query"})
        self.assertEqual([r["id"] for r in body["messages"]], ids)
        self.assertEqual([r["score"] for r in body["messages"]], scores)

  async def test_literal_preserves_raw_query_and_safe_html(self):
    needles = ['AND OR -(x) [y] "q" \\ % _', '  ', '\n', ' leading ',
               'A\nB', '😀', '&', 'lt', '<script>', '&lt;', 'Aa', '简体', 'aaa']
    for mid, needle in enumerate(needles, 1):
      await self.seed(mid, 'prefix' + needle + needle + 'suffix')
      for sort in ("time", "relevance"):
        _, body = await self.search({"q": needle, "match": "literal", "sort": sort})
        row = next(r for r in body["messages"] if r["id"] == mid)
        expected = 'prefix' + ('<span class="keyword">' + escape(needle) + '</span>') * 2 + 'suffix'
        self.assertEqual(row["html"], expected)
        if sort == "relevance":
          self.assertEqual(row["score"], 2)
          self.assertIsInstance(row["score"], int)
        else:
          self.assertNotIn("score", row)
          self.assertIsNone(body["score_kind"])
    for needle in ('aa', '簡體', 'AA', 'leading   ', '\nA'):
      _, body = await self.search({"q": needle, "match": "literal"})
      if needle != 'aa':
        self.assertEqual(body["messages"], [], needle)
    _, body = await self.search({"q": "aaa", "match": "literal"})
    self.assertEqual(body["messages"][0]["score"], 2)  # not four overlapping matches

  async def test_legacy_empty_query_browses_and_no_embedding_tables_required(self):
    await self.seed(1, "unrelated")
    _, body = await self.search({"q": "", "match": "query", "sort": "time"})
    self.assertEqual([r["id"] for r in body["messages"]], [1])
    _, rows = await self.db.search(SearchQuery(fixtures.GROUP_ID, "", None, None, None), Principal(None, None))
    self.assertEqual([r["msgid"] for r in rows], [1])
    async with self.db.get_conn() as conn:
      self.assertFalse(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname=$1 AND tablename LIKE 'embeddings_%')", self.fixture.schema))

  async def test_live_query_predicate_can_use_pgroonga_index(self):
    await self.seed(1, "rarekeyword")
    async with self.db.get_conn() as conn:
      archive = await self.db.archive_conn(conn, self.cid)
      await archive.execute("""INSERT INTO {messages}
        (conversation_id,group_id,msgid,text,from_user_name,created_at)
        SELECT $1,$2,n,'unrelated filler','Sender',$3
        FROM generate_series(2,2000) n""", self.cid, fixtures.GROUP_ID,
        datetime.datetime(2025, 1, 2, tzinfo=UTC))
      # Fixed template routed only through the archive's validated UUID.
      # pi-lens-ignore: python-sql-injection
      await archive.execute("ANALYZE {messages}")
    plans = []

    class ExplainingConnection(asyncpg.Connection):
      async def fetch(self, query, *args, **kwargs):
        if "WITH eligible AS" in query:
          plan = await super().fetch("EXPLAIN " + query, *args)
          plans.extend(r[0] for r in plan)
        return await super().fetch(query, *args, **kwargs)

    pool = self.db.pool
    assert pool is not None
    await pool.close()
    self.db.pool = await asyncpg.create_pool(fixtures.DATABASE_URL, min_size=1, max_size=2,
      connection_class=ExplainingConnection,
      server_settings={"search_path": f"{self.fixture.schema},public", "enable_seqscan": "off"})
    _, body = await self.search({"q": "rarekeyword", "match": "query"})
    self.assertEqual(body["messages"][0]["score"], 1)
    self.assertTrue(any("Index Cond:" in line and "&@~" in line for line in plans), plans)
    self.assertFalse(any("CTE Scan on eligible" in line for line in plans), plans)

  @unittest.skipIf(_query_transform is None, "requires native querytrans; also run in application image")
  async def test_default_syntax_and_metadata_remain_legacy(self):
    for mid, text in enumerate(("test alpha", "test beta", "test excluded", "簡體"), 1):
      await self.seed(mid, text)
    _, legacy = await self.search({"q": "test (alpha OR beta) -excluded", "sort": "time", "match": "query"})
    self.assertEqual([r["id"] for r in legacy["messages"]], [2, 1])
    self.assertNotIn("ranking", legacy)
    self.assertNotIn("score", legacy["messages"][0])
    _, normalized = await self.search({"q": "简体", "match": "query"})
    self.assertEqual([r["id"] for r in normalized["messages"]], [4])


class LiteralRelevanceTests(TextSearchTests):
  options = {"match": "literal", "sort": "relevance"}
