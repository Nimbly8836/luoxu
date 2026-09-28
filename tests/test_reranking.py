"""Deterministic reranking: real HTTP and disposable PostgreSQL, no model downloads."""

import asyncio
import datetime
import importlib.util
import io
import threading
import unittest
from unittest.mock import patch
from collections.abc import Awaitable, Callable

import test_semantic_search as semantic_tests

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from luoxu.db import PostgreStore
from luoxu.rerank import MODEL_ID, RerankerClient
from luoxu.semantic import (
  MAX_DOCUMENT_CHARS, MAX_QUERY_CHARS, SemanticUnavailable,
  search_vectors, revalidate_candidates, vector_literal,
)
from luoxu.types import GroupNotFound, SearchQuery
from test_semantic_search import DATABASE_URL, ROOT, UTC


def load_server():
  spec = importlib.util.spec_from_file_location("reranker_server", ROOT / "docker/reranker/server.py")
  assert spec and spec.loader
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


class ConfigurationTests(unittest.TestCase):
  def test_opt_in_and_positional_compatibility(self):
    self.assertIsNone(PostgreStore({"url": "unused"}).reranker)
    self.assertIsNone(PostgreStore({"url": "unused", "semantic": {"rerank": {"enabled": False}}}).reranker)
    db = PostgreStore({"url": "unused", "semantic": {"rerank": {"enabled": True}}})
    self.assertIsNotNone(db.reranker)
    self.assertIsNone(db.embedder)
    q = SearchQuery(1, "x", None, None, None, None, None, "semantic", 20, False)
    self.assertIsNone(q.min_score)
    self.assertEqual(q.offset, 20)

  def test_configuration_validation(self):
    for config in (None, [], {"enabled": "true"}, {"enabled": 1}, {"endpoint": 1},
                   {"endpoint": "file:///x"}, {"candidates": 0}, {"candidates": 201},
                   {"candidates": True}, {"candidates": 1.5}, {"page_size": 0},
                   {"page_size": 51}, {"page_size": "20"}, {"min_score": True},
                   {"min_score": "0.5"}, {"min_score": -0.1}, {"min_score": 1.1},
                   {"min_score": float("nan")}, {"min_score": float("inf")}):
      with self.subTest(config=config), self.assertRaises(ValueError):
        RerankerClient(config)
    for enabled in ("false", 1, None):
      with self.assertRaises(ValueError):
        PostgreStore({"url": "unused", "semantic": {"enabled": enabled}})


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
  async def test_malformed_model_count_scores_and_order(self):
    payload = {"model": MODEL_ID, "scores": [{"index": 0, "score": 0.8}]}
    async def respond(request):
      await request.json()
      return web.json_response(payload)
    app = web.Application()
    app.router.add_post("/rerank", respond)
    server = TestServer(app)
    await server.start_server()
    client = RerankerClient({"endpoint": str(server.make_url("/rerank"))})
    try:
      self.assertEqual(await client.rerank("q", ["text"]), [0.8])
      invalid = [[], {"model": "wrong", "scores": []}, {"model": MODEL_ID, "scores": []},
                 {"model": MODEL_ID, "scores": [0.8]}]
      for value in (True, "0.8", None, -0.1, 1.1, float("nan"), float("inf")):
        invalid.append({"model": MODEL_ID, "scores": [{"index": 0, "score": value}]})
      for index in (True, 1, "0", None):
        invalid.append({"model": MODEL_ID, "scores": [{"index": index, "score": 0.8}]})
      for payload in invalid:
        with self.subTest(payload=payload), self.assertRaises(SemanticUnavailable):
          await client.rerank("q", ["text"])
    finally:
      await client.close()
      await server.close()

  async def test_maximum_unicode_control_payloads_and_batch_order(self):
    received = []
    def predict(pairs):
      received.append(pairs)
      return [0.75] * len(pairs)
    server = TestServer(load_server().create_app(predict))
    await server.start_server()
    client = RerankerClient({"endpoint": str(server.make_url("/rerank"))})
    try:
      for char in ("😀", "\x01"):
        received.clear()
        query, text = char * MAX_QUERY_CHARS, char * MAX_DOCUMENT_CHARS
        self.assertEqual(await client.rerank(query, [text] * 200), [0.75] * 200)
        self.assertEqual([len(pairs) for pairs in received], [32] * 6 + [8])
        self.assertTrue(all(pair == (query, text) for pairs in received for pair in pairs))
    finally:
      await client.close()
      await server.close()

  async def test_input_limits_health_and_inference_failure(self):
    async with TestClient(TestServer(load_server().create_app(lambda pairs: [float("nan")]*len(pairs)))) as client:
      self.assertEqual((await (await client.get("/health")).json())["model"], MODEL_ID)
      for body in ([], {}, {"query": "q", "texts": []}, {"query": "q", "texts": [1]},
                   {"query": True, "texts": ["t"]}, {"query": " ", "texts": ["t"]},
                   {"query": "x" * 2001, "texts": ["t"]}, {"query": "q", "texts": ["t"] * 33},
                   {"query": "q", "texts": ["x" * 8193]}):
        self.assertEqual((await client.post("/rerank", json=body)).status, 400)
      self.assertEqual((await client.post("/rerank", data=io.BytesIO(b"x" * (2 * 1024 * 1024 + 1)))).status, 413)
      self.assertEqual((await client.post("/rerank", json={"query": "q", "texts": ["t"]})).status, 503)

  async def test_timeout_bounds_all_batches_and_slot_wait(self):
    calls = []
    async def respond(request):
      data = await request.json()
      calls.append(data)
      await asyncio.sleep(0.035)
      return web.json_response({"model": MODEL_ID, "scores": [
        {"index": i, "score": 0.8} for i in range(len(data["texts"]))
      ]})
    app = web.Application()
    app.router.add_post("/rerank", respond)
    server = TestServer(app)
    await server.start_server()
    client = RerankerClient({"endpoint": str(server.make_url("/rerank"))})
    try:
      with patch("luoxu.rerank.REQUEST_TIMEOUT", 0.06):
        with self.assertRaises(SemanticUnavailable):
          await client.rerank("q", ["t"] * 65)
        self.assertEqual(len(calls), 2)
        async with client.slots, client.slots:
          with self.assertRaises(SemanticUnavailable):
            await client.rerank("q", ["t"])
        self.assertEqual(len(calls), 2)
    finally:
      await client.close()
      await server.close()

  async def test_busy_and_repeated_cancellation_keep_cpu_admission(self):
    started, release = threading.Event(), threading.Event()
    handlers = []
    def predict(pairs):
      started.set()
      release.wait(5)
      return [0.8] * len(pairs)
    @web.middleware
    async def capture(request, handler):
      handlers.append(asyncio.current_task())
      return await handler(request)
    app = load_server().create_app(predict)
    app.middlewares.append(capture)
    async with TestClient(TestServer(app)) as client:
      first = asyncio.create_task(client.post("/rerank", json={"query": "q", "texts": ["t"]}))
      try:
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        response = await client.post("/rerank", json={"query": "q", "texts": ["t"]})
        self.assertEqual(response.status, 503)
        self.assertEqual(response.headers["Retry-After"], "1")
        handlers[0].cancel()
        handlers[0].cancel()
        await asyncio.sleep(0.01)
        self.assertEqual((await client.post("/rerank", json={"query": "q", "texts": ["t"]})).status, 503)
      finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
      await asyncio.sleep(0.05)
      self.assertEqual((await client.post("/rerank", json={"query": "q", "texts": ["t"]})).status, 200)


@unittest.skipUnless(DATABASE_URL, "requires disposable PostgreSQL")
class RerankingDatabaseTests(semantic_tests.SemanticDatabaseFixture):
  async def asyncSetUp(self):
    await super().asyncSetUp()
    self.received = []
    self.scores = {}
    self.before_return: Callable[[], Awaitable[None]] | None = None
    self.status = 200
    self.payload = None
    async def respond(request):
      data = await request.json()
      self.received.append(data)
      if self.before_return:
        await self.before_return()
      return web.json_response(self.payload or {"model": MODEL_ID, "scores": [
        {"index": i, "score": self.scores.get(text, 0.8)} for i, text in enumerate(data["texts"])
      ]}, status=self.status)
    app = web.Application()
    app.router.add_post("/rerank", respond)
    self.server = TestServer(app)
    await self.server.start_server()
    self.db.reranker = RerankerClient({"enabled": True, "endpoint": str(self.server.make_url("/rerank"))})

  async def asyncTearDown(self):
    await super().asyncTearDown()
    await self.server.close()

  async def test_reverses_cosine_and_api_metadata(self):
    await self.seed(1, "job")
    await self.seed(2, "food")
    await self.backfill()
    self.scores = {"job": 0.6, "food": 0.9}
    async with self.client() as client:
      response = await client.get("/search?g=123&mode=semantic&q=job")
      self.assertEqual(response.status, 200)
      body = await response.json()
      self.assertEqual([m["id"] for m in body["messages"]], [2, 1])
      self.assertEqual([m["score"] for m in body["messages"]], [0.9, 0.6])
      self.assertEqual([m["vector_score"] for m in body["messages"]], [0, 1])
      self.assertEqual((body["ranking"], body["min_score"], body["page_size"], body["candidates"]),
                       ("reranker", 0.5, 20, 50))
      self.assertFalse(body["has_more"])
      self.assertIsNone(body["next_offset"])

  async def test_cutoff_empty_no_fill_override_and_validation(self):
    await self.seed(1)
    await self.backfill()
    self.scores = {"job pressure": 0.1}
    async with self.client() as client:
      body = await (await client.get("/search?g=123&mode=semantic&q=job")).json()
      self.assertEqual(body["messages"], [])
      self.assertFalse(body["has_more"])
      body = await (await client.get("/search?g=123&mode=semantic&q=job&min_score=0.1")).json()
      self.assertEqual(len(body["messages"]), 1)
      self.assertEqual(body["min_score"], 0.1)
      for value in ("-1", "1.01", "nan", "inf", "true", "", "x"):
        self.assertEqual((await client.get(f"/search?g=123&mode=semantic&q=job&min_score={value}")).status, 400)
      self.assertEqual((await client.get("/search?g=123&q=job&min_score=0.1")).status, 400)
      assert self.db.reranker is not None
      await self.db.reranker.close()
      self.db.reranker = None
      self.assertEqual((await client.get("/search?g=123&mode=semantic&q=job&min_score=0.1")).status, 400)

  async def test_fixed_window_pagination_ties_and_no_unscored_fill(self):
    for mid in range(1, 56):
      await self.seed(mid, f"job {mid}")
    await self.backfill()
    self.scores = {f"job {i}": 0.9 if i % 2 else 0.8 for i in range(1, 56)}
    expected = sorted(range(6, 56), key=lambda i: (-self.scores[f"job {i}"], -i))
    async with self.client() as client:
      actual = []
      for offset in (0, 20, 40, 60):
        body = await (await client.get(f"/search?g=123&mode=semantic&q=job&offset={offset}")).json()
        actual.extend(m["id"] for m in body["messages"])
        self.assertEqual(body["next_offset"], offset + 20 if offset < 40 else None)
      self.assertEqual(actual, expected)
    windows = [sum((r["texts"] for r in self.received[i:i+2]), []) for i in range(0, 8, 2)]
    self.assertEqual(windows, [[f"job {i}" for i in range(55, 5, -1)]] * 4)

  async def test_filters_and_acl_before_candidate_limit_and_scoring(self):
    await self.seed(1, "job allowed", sender=100, year=2020)
    for i in range(2, 54):
      await self.seed(i, "job excluded", sender=200)
    await self.seed(54, "job private", cid=self.private)
    async with self.db.get_conn() as conn:
      topic = await self.db._ensure_conversation(conn, "topic", "channel", 123, "Topic", topic_id=42, legacy_group_id=123)
    await self.seed(55, "job topic", cid=topic["id"], sender=100, year=2020)
    await self.backfill()
    assert self.db.reranker is not None
    self.db.reranker.candidates = 1
    rows = await self.search(sender=[100, 200], exclude_sender=[200],
                             start=datetime.datetime(2019, 1, 1, tzinfo=UTC),
                             end=datetime.datetime(2021, 1, 1, tzinfo=UTC),
                             conversation_id=str(self.public), group=123)
    self.assertEqual([r["msgid"] for r in rows], [1])
    self.assertEqual(self.received[0]["texts"], ["job allowed"])
    with self.assertRaises(GroupNotFound):
      await self.search(conversation_id=str(self.private))
    self.assertEqual(len(self.received), 1)
    rows = await self.search(conversation_id=str(topic["id"]), sender=[100])
    self.assertEqual([r["msgid"] for r in rows], [55])
    self.assertEqual(self.received[-1]["texts"], ["job topic"])

  async def test_inflight_edit_delete_sender_changes_and_fresh_metadata_without_locks(self):
    for i in range(1, 7):
      await self.seed(i, f"job {i}")
    await self.backfill()
    async def change():
      async with self.db.get_conn() as conn:
        await conn.execute("SET LOCAL lock_timeout = '300ms'")
        # ACCESS EXCLUSIVE conflicts even with SELECT's AccessShareLock.
        await conn.execute(f"LOCK TABLE {self.messages} IN ACCESS EXCLUSIVE MODE")
        await conn.execute(f"UPDATE {self.messages} SET text='edited' WHERE msgid=1")
        await conn.execute(f"UPDATE {self.messages} SET deleted_at=now() WHERE msgid=2")
        await conn.execute(f"DELETE FROM {self.messages} WHERE msgid=3")
        await conn.execute(f"UPDATE {self.messages} SET from_user=200 WHERE msgid=4")
        await conn.execute(f"UPDATE {self.messages} SET from_user_name='Fresh' WHERE msgid=5")
    self.before_return = change
    rows = await self.search(sender=[100], exclude_sender=[200])
    self.assertEqual([r["msgid"] for r in rows], [6, 5])
    self.assertEqual(rows[1]["from_user_name"], "Fresh")

  async def test_mutation_does_not_fill_with_unscored_candidates(self):
    for i in range(1, 4):
      await self.seed(i, f"job {i}")
    await self.backfill()
    assert self.db.reranker is not None
    self.db.reranker.candidates = 2
    async def change():
      async with self.db.get_conn() as conn:
        await conn.execute(f"UPDATE {self.messages} SET text='edited' WHERE msgid=3")
    self.before_return = change
    self.assertEqual([r["msgid"] for r in await self.search()], [2])
    self.assertEqual(self.received[0]["texts"], ["job 3", "job 2"])

  async def test_physical_message_variants_do_not_share_scores(self):
    await self.seed(1, "job old", year=2020)
    await self.seed(1, "job new", year=2025)
    await self.backfill()
    self.scores = {"job old": 0.9, "job new": 0.6}
    rows = await self.search()
    self.assertEqual([(r["text"], r["score"]) for r in rows], [("job old", 0.9), ("job new", 0.6)])
    async def change():
      async with self.db.get_conn() as conn:
        await conn.execute(f"UPDATE {self.messages} SET text='changed' WHERE text='job new'")
    self.before_return = change
    self.assertEqual([r["text"] for r in await self.search()], ["job old"])

  async def test_private_grant_revocation_and_timeout_at_http_boundary(self):
    from luoxu.auth import Principal
    await self.seed(1, "job private", cid=self.private)
    await self.backfill()
    user = await self.db.create_user("rerank-reader", "unused", True)
    await self.db.grant_conversation(user["id"], self.private)
    principal = Principal(str(user["id"]), "rerank-reader", True, False)
    q = SearchQuery(0, "job", None, None, None, conversation_id=str(self.private), mode="semantic")
    _, rows = await self.db.search(q, principal)
    self.assertEqual([r["text"] for r in rows], ["job private"])
    async def revoke():
      await self.db.revoke_conversation(user["id"], self.private)
    self.before_return = revoke
    with self.assertRaises(GroupNotFound):
      await self.db.search(q, principal)
    await self.seed(2)
    await self.backfill()
    async def delay():
      await asyncio.sleep(0.2)
    self.before_return = delay
    async with self.client() as client:
      with patch("luoxu.rerank.REQUEST_TIMEOUT", 0.04):
        self.assertEqual((await client.get("/search?g=123&mode=semantic&q=job")).status, 503)

  async def test_revocation_is_rechecked_even_all_scores_low(self):
    await self.seed(1)
    await self.backfill()
    self.scores = {"job pressure": 0.01}
    async def revoke():
      await self.db.revoke_public(self.public)
    self.before_return = revoke
    with self.assertRaises(GroupNotFound):
      await self.search()

  async def test_exactly_one_ranking_and_bounded_revalidation(self):
    for mid in range(1, 7):
      await self.seed(mid, f"job {mid}")
    await self.backfill()
    assert self.db.reranker is not None
    self.db.reranker.candidates = 3
    with patch("luoxu.db.search_vectors", wraps=search_vectors) as rank, \
         patch("luoxu.db.revalidate_candidates", wraps=revalidate_candidates) as recheck:
      rows = await self.search()
    self.assertEqual([r["msgid"] for r in rows], [6, 5, 4])
    self.assertEqual(rank.await_count, 1)
    self.assertEqual(recheck.await_count, 1)
    self.assertEqual([r["msgid"] for r in recheck.call_args.args[3]], [6, 5, 4])

  async def test_new_superior_vector_does_not_evict_initial_window_or_replace_score(self):
    await self.seed(1, "food")
    await self.seed(2, "food two")
    await self.backfill()
    assert self.db.reranker is not None
    self.db.reranker.candidates = 2
    async def change():
      await self.seed(3, "job superior")
      await self.backfill()
      async with self.db.get_conn() as conn:
        # self.embeddings is the fixture's registry-UUID-derived identifier.
        # pi-lens-ignore: python-sql-injection
        await conn.execute(f"UPDATE {self.embeddings} SET embedding=$1::text::vector WHERE msgid=1",
                           vector_literal(semantic_tests.vector()))
    self.before_return = change
    rows = await self.search()
    self.assertEqual([r["msgid"] for r in rows], [2, 1])
    self.assertEqual([r["vector_score"] for r in rows], [0, 0])
    self.assertEqual(self.received[0]["texts"], ["food two", "food"])
    self.before_return = None
    # A later request gets its own current window; no snapshot across pages.
    self.assertEqual([r["msgid"] for r in await self.search()], [3, 1])

  async def test_inflight_embedding_metadata_model_hash_and_deletion(self):
    for mid in range(1, 5):
      await self.seed(mid, f"job {mid}")
    await self.backfill()
    async def change():
      async with self.db.get_conn() as conn:
        await conn.execute(f"UPDATE {self.embeddings} SET model='other' WHERE msgid=1")
        await conn.execute(f"UPDATE {self.embeddings} SET content_hash='stale' WHERE msgid=2")
        await conn.execute(f"DELETE FROM {self.embeddings} WHERE msgid=3")
    self.before_return = change
    self.assertEqual([r["msgid"] for r in await self.search()], [4])

  async def test_inflight_time_topic_group_filters_and_reindexed_text(self):
    async with self.db.get_conn() as conn:
      topic = await self.db._ensure_conversation(
        conn, "topic", "channel", 123, "Topic", topic_id=42, legacy_group_id=123,
      )
    for mid in range(1, 6):
      await self.seed(mid, f"job {mid}")
    await self.backfill()
    async def change():
      async with self.db.get_conn() as conn:
        await conn.execute(f"DELETE FROM {self.embeddings} WHERE msgid IN (1, 2)")
        await conn.execute(f"UPDATE {self.messages} SET created_at='2020-01-02' WHERE msgid=1")
        await conn.execute(f"UPDATE {self.messages} SET conversation_id=$1 WHERE msgid=2", topic["id"])
        await conn.execute(f"UPDATE {self.messages} SET group_id=NULL WHERE msgid=3")
        await conn.execute(f"UPDATE {self.messages} SET text='job changed' WHERE msgid=4")
      # Even a freshly eligible replacement vector cannot authorize old scores.
      await self.backfill()
    self.before_return = change
    rows = await self.search(group=123, conversation_id=str(self.public),
                             start=datetime.datetime(2024, 1, 1, tzinfo=UTC))
    self.assertEqual([r["msgid"] for r in rows], [5])

  async def test_topic_acl_is_rechecked_without_revoking_requested_parent(self):
    async with self.db.get_conn() as conn:
      topic = await self.db._ensure_conversation(
        conn, "topic", "channel", 123, "Topic", topic_id=42, legacy_group_id=123,
      )
    await self.seed(1)
    await self.seed(2, cid=topic["id"])
    await self.backfill()
    async def change():
      # A topic no longer belonging to the granted parent loses inherited ACL.
      async with self.db.get_conn() as conn:
        await conn.execute("UPDATE conversations SET telegram_peer_id=999 WHERE id=$1", topic["id"])
    self.before_return = change
    self.assertEqual([r["msgid"] for r in await self.search()], [1])

  async def test_empty_window_still_rechecks_authorization(self):
    async def rank_then_revoke(*args):
      rows = await search_vectors(*args)
      self.assertEqual(rows, [])
      await self.db.revoke_public(self.public)
      return rows
    with patch("luoxu.db.search_vectors", side_effect=rank_then_revoke) as rank:
      with self.assertRaises(GroupNotFound):
        await self.search()
    self.assertEqual(rank.await_count, 1)
    self.assertEqual(self.received, [])

  async def test_unavailable_and_malformed_are_503_without_fallback(self):
    await self.seed(1)
    await self.backfill()
    async with self.client() as client:
      for status, payload in ((503, None), (200, {"model": "wrong"}),
                              (200, {"model": MODEL_ID, "scores": []}),
                              (200, {"model": MODEL_ID, "scores": [{"index": 0, "score": True}]})):
        self.status, self.payload = status, payload
        self.assertEqual((await client.get("/search?g=123&mode=semantic&q=job")).status, 503)
        self.assertEqual((await client.get("/search?g=123&q=job")).status, 200)
      assert self.db.reranker is not None
      self.db.reranker.endpoint = "http://127.0.0.1:1/rerank"
      self.assertEqual((await client.get("/search?g=123&mode=semantic&q=job")).status, 503)


if __name__ == "__main__":
  unittest.main()
