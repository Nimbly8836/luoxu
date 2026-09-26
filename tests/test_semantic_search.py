"""Unit and optional PostgreSQL/PGroonga/pgvector integration tests.

LUOXU_TEST_DATABASE_URL must point to a disposable test database, never production.
Model inference is deterministic/faked here; no downloads or GPU are required.
"""

import datetime
import importlib.util
import json
import os
import unittest
import uuid
from pathlib import Path
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock

import asyncpg
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer  # type: ignore[import-not-found]

from luoxu.auth import Principal
from luoxu.db import PostgreStore
from luoxu.semantic import (  # type: ignore[import-not-found]
  DIMENSIONS, MAX_DOCUMENT_CHARS, MODEL_ID, STRIP_CHARS,
  EmbeddingClient, SemanticUnavailable, index_batch, vector_literal,
)
from luoxu.types import GroupNotFound, SearchQuery
from luoxu.storage import archive_sql
from luoxu.web import SearchHandler, setup_app

ROOT = Path(__file__).resolve().parents[1]
UTC = datetime.timezone.utc
ANON = Principal(None, None)
DATABASE_URL = os.environ.get("LUOXU_TEST_DATABASE_URL")


def vector(first=1.0, second=0.0):
  return [first, second] + [0.0] * (DIMENSIONS - 2)


def load_embedding_server():
  spec = importlib.util.spec_from_file_location("embedding_server", ROOT / "docker/embeddings/server.py")
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


class QueryTests(unittest.TestCase):
  def test_default_and_semantic_filters(self):
    handler = SearchHandler(None)
    self.assertEqual(handler._parse_query({"g": "123"}).mode, "keyword")
    self.assertEqual(SearchQuery(0, None, None, None, None).offset, 0)
    q = handler._parse_query({
      "mode": "semantic", "q": "工作压力", "sender": "1,2",
      "exclude_sender": "2,3", "offset": "50",
      "start": "100", "end": "200", "g": "123",
    })
    self.assertEqual((q.mode, q.offset, q.sender, q.exclude_sender),
                     ("semantic", 50, [1, 2], [2, 3]))
    assert q.start is not None
    self.assertEqual(q.start.timestamp(), 100)
    self.assertEqual(q.group, 123)

  def test_invalid_parameters(self):
    for params in (
      {"mode": "other"}, {"mode": "semantic"},
      {"mode": "semantic", "q": "   "},
      {"mode": "semantic", "q": "x" * 2001},
      {"mode": "semantic", "q": "x", "offset": "-1"},
      {"mode": "semantic", "q": "x", "offset": "1001"},
      {"offset": "1"}, {"start": "200", "end": "100"},
    ):
      with self.subTest(params=params), self.assertRaises(web.HTTPBadRequest):
        SearchHandler(None)._parse_query({"g": "123", **params})

  def test_vector_validation(self):
    self.assertEqual(json.loads(vector_literal(vector(2))), vector())
    for bad in ([0.0] * DIMENSIONS, [1], vector(float("nan")),
                vector(float("inf")), [True] * DIMENSIONS, ["1"] * DIMENSIONS):
      with self.subTest(bad=str(bad)[:30]), self.assertRaises(SemanticUnavailable):
        vector_literal(bad)


class ClientTests(unittest.IsolatedAsyncioTestCase):
  async def test_embedding_protocol_and_failure_validation(self):
    payload = {"model": MODEL_ID, "vectors": [vector()]}
    received = []

    async def respond(request):
      received.append(await request.json())
      return web.json_response(payload)

    app = web.Application()
    app.router.add_post("/embed", respond)
    server = TestServer(app)
    await server.start_server()
    embedder = EmbeddingClient(str(server.make_url("/embed")))
    try:
      self.assertEqual(await embedder.embed(["工作压力"], query=True), [vector_literal(vector())])
      self.assertEqual(received, [{"texts": ["工作压力"], "query": True}])
      for invalid in (
        {"model": "wrong", "vectors": [vector()]},
        {"model": MODEL_ID, "vectors": []},
        {"model": MODEL_ID, "vectors": [[0.0] * DIMENSIONS]},
      ):
        payload = invalid
        with self.assertRaises(SemanticUnavailable):
          await embedder.embed(["message"])
    finally:
      await embedder.close()
      await server.close()

  async def test_maximum_unicode_batch_fits_real_http_limit(self):
    module = load_embedding_server()
    received = []

    def encode(texts):
      received.extend(texts)
      return [vector() for _ in texts]

    server = TestServer(module.create_app(encode))
    await server.start_server()
    embedder = EmbeddingClient(str(server.make_url("/embed")))
    try:
      # 12-byte JSON escapes with ensure_ascii=True would exceed 2 MiB.
      # Control characters still need escapes even with Unicode serialization.
      for char in ("😀", "\x01"):
        texts = [char * MAX_DOCUMENT_CHARS] * 32
        received.clear()
        result = await embedder.embed(texts)
        self.assertEqual(len(result), 32)
        self.assertEqual(received, texts)
    finally:
      await embedder.close()
      await server.close()

  async def test_service_rejects_bad_input_and_applies_query_prefix(self):
    module = load_embedding_server()
    received = []

    def encode(texts):
      received.extend(texts)
      return [vector() for _ in texts]

    async with TestClient(TestServer(module.create_app(encode))) as client:
      response = await client.post("/embed", json={"texts": ["工作压力"], "query": True})
      self.assertEqual(response.status, 200)
      self.assertEqual((await response.json())["model"], MODEL_ID)
      self.assertEqual(received, [module.QUERY_PREFIX + "工作压力"])
      for body in ([], {"texts": []}, {"texts": [""]}, {"texts": [1]},
                   {"texts": ["x"] * 33}, {"texts": ["x"], "query": "yes"}):
        response = await client.post("/embed", json=body)
        self.assertEqual(response.status, 400)
      self.assertEqual((await client.get("/health")).status, 200)


class FakeEmbedder(EmbeddingClient):
  def __init__(self):
    super().__init__("http://unused/embed")
    self.before_return: Callable[[], Awaitable[None]] | None = None

  async def close(self):
    pass

  async def embed(self, texts, *, query=False):
    if self.before_return:
      await self.before_return()
    return [vector_literal(vector() if query or "job" in t else vector(0, 1)) for t in texts]


@unittest.skipUnless(DATABASE_URL, "requires disposable LUOXU_TEST_DATABASE_URL with pgvector")
class SemanticDatabaseTests(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    self.admin = await asyncpg.connect(DATABASE_URL)
    # Extensions live in public, not the per-test schema that teardown removes.
    for extension in ("pgroonga", "pgcrypto", "vector"):
      await self.admin.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")
    self.schema = "semantic_test_" + uuid.uuid4().hex
    await self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
    self.db = PostgreStore({"url": DATABASE_URL})
    self.db.pool = await asyncpg.create_pool(
      DATABASE_URL, min_size=1, max_size=4,
      server_settings={"search_path": f"{self.schema},public"},
    )
    async with self.db.get_conn() as conn:
      # Fixed, repository-owned DDL, not user-provided SQL.
      # pi-lens-ignore: python-sql-injection
      await conn.execute((ROOT / "dbsetup.sql").read_text())
      # pi-lens-ignore: python-sql-injection
      await conn.execute((ROOT / "migrations/005_semantic_search.sql").read_text())
      # Replay optional migration must be safe too.
      # pi-lens-ignore: python-sql-injection
      await conn.execute((ROOT / "migrations/005_semantic_search.sql").read_text())
      public = await self.db._ensure_conversation(conn, "group", "channel", 123, "Public", legacy_group_id=123)
      self.public = public["id"]
      self.archive_id = public["archive_id"]
      self.messages = archive_sql("{messages}", self.archive_id)
      self.embeddings = archive_sql("{embeddings}", self.archive_id)
      await conn.execute("INSERT INTO tg_groups (group_id, name, conversation_id) VALUES (123, 'Public', $1)", self.public)
      private = await self.db._ensure_conversation(conn, "private_chat", "user", 456, "Private")
      self.private = private["id"]
    await self.db.grant_public(self.public)
    self.embedder = FakeEmbedder()
    self.db.embedder = self.embedder

  async def asyncTearDown(self):
    await self.db.close()
    await self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
    await self.admin.close()

  async def seed(self, mid, text="job pressure", *, cid=None, sender: int | None = 100, year=2025):
    cid = cid or self.public
    async with self.db.get_conn() as conn:
      group_id = await conn.fetchval("SELECT legacy_group_id FROM conversations WHERE id=$1", cid)
      conn = await self.db.archive_conn(conn, cid)
      # ArchiveConnection allows only UUID-derived identifiers; values are bound.
      # pi-lens-ignore: python-sql-injection
      await conn.execute(
        """INSERT INTO {messages}
          (conversation_id, group_id, msgid, from_user, from_user_name, text, created_at)
          VALUES ($1, $2, $3, $4, 'Sender', $5, $6)""",
        cid, group_id, mid, sender, text,
        datetime.datetime(year, 1, 2, tzinfo=UTC),
      )

  async def backfill(self):
    for archive in await self.db.list_archives():
      while await index_batch(self.db, self.embedder, archive_id=archive["id"]):
        pass

  async def search(self, **kwargs):
    if kwargs.get("conversation_id"):
      kwargs.setdefault("group", 0)
    q = SearchQuery(123, "job", None, None, None, mode="semantic")._replace(**kwargs)
    _, rows = await self.db.search(q, ANON)
    return rows

  def client(self):
    return TestClient(TestServer(setup_app(self.db, None, "/tmp", "nobody.jpg", "ghost.jpg")))

  async def test_whitespace_cannot_poison_http_backfill(self):
    await self.seed(1, "\n\t")
    await self.seed(2, STRIP_CHARS)
    await self.seed(3, "\n\t\u3000" * MAX_DOCUMENT_CHARS + "job")
    await self.seed(4, "job")
    received = []

    def encode(texts):
      received.extend(texts)
      return [vector() for _ in texts]

    server = TestServer(load_embedding_server().create_app(encode))
    await server.start_server()
    embedder = EmbeddingClient(str(server.make_url("/embed")))
    try:
      self.assertEqual(await index_batch(self.db, embedder, archive_id=self.archive_id), 2)
      self.assertEqual(await index_batch(self.db, embedder, archive_id=self.archive_id), 0)
      self.assertEqual(received, ["job", "job"])
      self.assertEqual({r["msgid"] for r in await self.search()}, {3, 4})
    finally:
      await embedder.close()
      await server.close()

  async def test_rank_across_years_not_by_date_and_keep_keyword_mode(self):
    await self.seed(1, "job pressure", year=2020)
    await self.seed(2, "food", year=2025)
    await self.backfill()
    rows = await self.search()
    self.assertEqual([r["msgid"] for r in rows], [1, 2])
    self.assertAlmostEqual(rows[0]["score"], 1)
    self.assertAlmostEqual(rows[1]["score"], 0)
    q = SearchQuery(123, "food", None, None, None)
    _, rows = await self.db.search(q, ANON)
    self.assertEqual([r["msgid"] for r in rows], [2])
    self.assertEqual(await index_batch(self.db, self.embedder, archive_id=self.archive_id), 0)

  async def test_acl_private_topic_admin_and_revocation(self):
    await self.seed(1)
    await self.seed(2, cid=self.private)
    async with self.db.get_conn() as conn:
      topic = await self.db._ensure_conversation(conn, "topic", "channel", 123, "Topic", topic_id=42, legacy_group_id=123)
    await self.seed(3, cid=topic["id"])
    await self.backfill()
    self.assertEqual({r["msgid"] for r in await self.search()}, {1, 3})
    with self.assertRaises(GroupNotFound):
      await self.search(conversation_id=str(self.private))
    admin = await self.db.create_user("administrator", "unused", True)
    principal = Principal(str(admin["id"]), "administrator", True, False)
    q = SearchQuery(123, "job", None, None, None, mode="semantic")
    _, rows = await self.db.search(q, principal)
    self.assertEqual({r["msgid"] for r in rows}, {1, 3})
    await self.db.grant_conversation(admin["id"], self.private)
    private_query = q._replace(group=0, conversation_id=str(self.private))
    _, rows = await self.db.search(private_query, principal)
    self.assertEqual({r["msgid"] for r in rows}, {2})
    _, rows = await self.db.search(q, principal)
    self.assertEqual({r["msgid"] for r in rows}, {1, 3})
    await self.db.revoke_conversation(admin["id"], self.private)
    await self.db.revoke_public(self.public)
    with self.assertRaises(GroupNotFound):
      await self.search()
    with self.assertRaises(GroupNotFound):
      await self.db.search(q, principal)
    with self.assertRaises(GroupNotFound):
      await self.db.search(private_query, principal)

  async def test_sender_exclusions_dates_and_group_filter_before_limit(self):
    await self.seed(1, sender=100, year=2020)
    await self.seed(2, sender=200)
    await self.seed(3, sender=None)
    await self.backfill()
    self.assertEqual([r["msgid"] for r in await self.search(sender=[100, 200], exclude_sender=[100, 999])], [2])
    self.assertEqual([r["msgid"] for r in await self.search(exclude_sender=[100, 200])], [3])
    self.assertEqual([r["msgid"] for r in await self.search(group=123, end=datetime.datetime(2024, 1, 1, tzinfo=UTC))], [1])
    self.assertEqual({r["msgid"] for r in await self.search(start=datetime.datetime(2024, 1, 1, tzinfo=UTC))}, {2, 3})

  async def test_edits_deletes_blank_and_physical_delete_invalidate_vectors(self):
    await self.seed(1)
    await self.seed(2, "   ")
    await self.backfill()
    self.assertEqual([r["msgid"] for r in await self.search()], [1])
    async with self.db.get_conn() as conn:
      await conn.execute(f"UPDATE {self.messages} SET text = 'food' WHERE msgid = 1")
      self.assertEqual(await conn.fetchval(f"SELECT count(*) FROM {self.embeddings}"), 0)
    self.assertEqual(await self.search(), [])
    await self.backfill()
    self.assertAlmostEqual((await self.search())[0]["score"], 0)
    await self.db.delete_messages(123, [1], peer_type="channel")
    async with self.db.get_conn() as conn:
      self.assertEqual(await conn.fetchval(f"SELECT count(*) FROM {self.embeddings}"), 0)
    self.assertEqual(await self.search(), [])
    await self.seed(3)
    await self.backfill()
    async with self.db.get_conn() as conn:
      await conn.execute(f"DELETE FROM {self.messages} WHERE msgid = 3")
      self.assertEqual(await conn.fetchval(f"SELECT count(*) FROM {self.embeddings}"), 0)

  async def test_change_during_inference_does_not_save_stale_vector(self):
    await self.seed(1)

    async def change():
      async with self.db.get_conn() as conn:
        await conn.execute(f"UPDATE {self.messages} SET text = 'food' WHERE msgid = 1")

    self.embedder.before_return = change
    await index_batch(self.db, self.embedder, archive_id=self.archive_id)
    async with self.db.get_conn() as conn:
      self.assertEqual(await conn.fetchval(f"SELECT count(*) FROM {self.embeddings}"), 0)
    self.embedder.before_return = None
    await self.backfill()
    self.assertAlmostEqual((await self.search())[0]["score"], 0)

  async def test_disabled_outage_and_missing_migration_are_503_not_fallback(self):
    await self.seed(1)
    async with self.client() as client:
      self.db.embedder = None
      response = await client.get("/search?g=123&mode=semantic&q=job")
      self.assertEqual(response.status, 503)
      self.assertEqual((await client.get("/search?g=123&q=job")).status, 200)
      self.db.embedder = AsyncMock()
      self.db.embedder.embed.side_effect = SemanticUnavailable("embedding service unavailable")
      response = await client.get("/search?g=123&mode=semantic&q=job")
      self.assertEqual(response.status, 503)
      self.assertEqual((await client.get("/search?g=123&q=job")).status, 200)
      self.db.embedder = self.embedder
      async with self.db.get_conn() as conn:
        await conn.execute(f"DROP TABLE {self.embeddings}")
      response = await client.get("/search?g=123&mode=semantic&q=job")
      self.assertEqual(response.status, 503)

  async def test_api_scores_pagination_and_html_escaping(self):
    for i in range(52):
      await self.seed(i + 1, "<script>job</script>")
    await self.backfill()
    async with self.client() as client:
      response = await client.get("/search?g=123&mode=semantic&q=job")
      body = await response.json()
      self.assertEqual(response.status, 200)
      self.assertEqual(len(body["messages"]), 50)
      self.assertTrue(body["has_more"])
      self.assertEqual(body["next_offset"], 50)
      self.assertEqual(body["mode"], "semantic")
      self.assertEqual(body["messages"][0]["html"], "&lt;script&gt;job&lt;/script&gt;")
      self.assertAlmostEqual(body["messages"][0]["score"], 1)
      self.assertEqual(response.headers["Cache-Control"], "private, no-store")
      second = await client.get("/search?g=123&mode=semantic&q=job&offset=50")
      page = await second.json()
      self.assertFalse(page["has_more"])
      self.assertIsNone(page["next_offset"])
      self.assertEqual(len(page["messages"]), 2)
      self.assertFalse({m["id"] for m in body["messages"]} & {m["id"] for m in page["messages"]})


if __name__ == "__main__":
  unittest.main()
