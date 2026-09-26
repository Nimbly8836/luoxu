"""Storage isolation and upgrade tests; only run against a disposable test DB."""

import asyncio
import datetime
import json
import os
import unittest
import uuid
from pathlib import Path
from urllib.parse import quote

import asyncpg
from aiohttp.test_utils import TestClient, TestServer

from luoxu.auth import Principal
from luoxu.db import PostgreStore
from luoxu.storage import ArchiveConnection, archive_sql
from luoxu.types import GroupNotFound, SearchQuery
from luoxu.web import setup_app

ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = os.environ.get("LUOXU_TEST_DATABASE_URL")
ANON = Principal(None, None)
DATE = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)


class RoutingTests(unittest.TestCase):
  def test_only_uuid_identifiers_can_be_interpolated(self):
    aid = uuid.uuid4()
    self.assertEqual(archive_sql("SELECT * FROM {messages} WHERE msgid=$1", aid),
                     f'SELECT * FROM "messages_{aid.hex}" WHERE msgid=$1')
    for bad in (None, 'x"; DROP TABLE conversations; --', "123"):
      with self.assertRaises(ValueError):
        archive_sql("{messages}", bad)


@unittest.skipUnless(DATABASE_URL, "requires disposable LUOXU_TEST_DATABASE_URL")
class StorageTests(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    self.admin = await asyncpg.connect(DATABASE_URL)
    for extension in ("pgroonga", "pgcrypto", "vector"):
      await self.admin.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")
    self.schema = "storage_test_" + uuid.uuid4().hex
    await self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
    self.db = PostgreStore({"url": DATABASE_URL})
    self.db.pool = await asyncpg.create_pool(
      DATABASE_URL, min_size=1, max_size=4,
      server_settings={"search_path": f"{self.schema},public"},
    )

  async def asyncTearDown(self):
    await self.db.close()
    await self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
    await self.admin.close()

  async def sql_file(self, path):
    async with self.db.get_conn() as conn:
      # Repository-owned SQL only, never remote input.
      # pi-lens-ignore: python-sql-injection
      await conn.execute((ROOT / path).read_text())

  async def fresh(self, *, semantic=False):
    await self.sql_file("dbsetup.sql")
    if semantic:
      await self.sql_file("migrations/005_semantic_search.sql")

  async def conversation(self, kind, peer_type, peer_id, *, topic=None):
    async with self.db.get_conn() as conn:
      row = await self.db._ensure_conversation(
        conn, kind, peer_type, peer_id, f"{kind} {peer_id}", topic_id=topic,
        legacy_group_id=None if kind == "private_chat" else peer_id,
      )
      if kind == "group":
        # Literal SQL with separately bound values.
        # pi-lens-ignore: python-sql-injection
        await conn.execute(
          "INSERT INTO tg_groups(group_id,name,conversation_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
          peer_id, row["name"], row["id"],
        )
      return row

  async def seed(self, conversation, text="needle", msgid=1):
    async with self.db.get_conn() as raw:
      conn = ArchiveConnection(raw, conversation["archive_id"])
      # UUID-only table routing and separately bound values.
      # pi-lens-ignore: python-sql-injection
      await conn.execute(
        """INSERT INTO {messages}
           (conversation_id,group_id,msgid,from_user,from_user_name,text,created_at)
           VALUES($1,$2,$3,100,'Sender',$4,$5)""",
        conversation["id"], conversation["legacy_group_id"], msgid, text, DATE,
      )

  async def test_independent_tables_topics_share_peer_no_parent_no_partitions(self):
    await self.fresh(semantic=True)
    first = await self.conversation("group", "channel", 123)
    topic = await self.conversation("topic", "channel", 123, topic=42)
    second = await self.conversation("group", "chat", 456)
    private = await self.conversation("private_chat", "user", 123)
    self.assertEqual(first["archive_id"], topic["archive_id"])
    self.assertEqual(len({first["archive_id"], second["archive_id"], private["archive_id"]}), 3)
    for row in (first, second, private):
      await self.seed(row)
    async with self.db.get_conn() as conn:
      tables = await conn.fetch(
        "SELECT relname,relkind::text AS relkind,relispartition FROM pg_class WHERE relnamespace=$1::regnamespace AND relname LIKE 'messages_%' AND relkind IN ('r','p')",
        self.schema,
      )
      self.assertEqual(len(tables), 3)
      self.assertTrue(all(r["relkind"] == "r" and not r["relispartition"] for r in tables))
      self.assertIsNone(await conn.fetchval("SELECT to_regclass($1)", f"{self.schema}.messages"))
      # Same Telegram message ID in separate peers is independent.
      for row in (first, second, private):
        scoped = ArchiveConnection(conn, row["archive_id"])
        self.assertEqual(await scoped.fetchval("SELECT count(*) FROM {messages}"), 1)
        self.assertEqual(await scoped.fetchval("SELECT count(*) FROM {embeddings}"), 0)
    await self.db.grant_public(first["id"])
    await self.db.grant_public(second["id"])
    for gid, cid in ((123, first["id"]), (456, second["id"])):
      _, rows = await self.db.search(SearchQuery(gid, "needle", None, None, None), ANON)
      self.assertEqual([r["conversation_id"] for r in rows], [cid])
    with self.assertRaises(GroupNotFound):
      await self.db.search(SearchQuery(123, "needle", None, None, None, str(second["id"])), ANON)

  async def test_topic_grant_with_matching_group_never_expands_scope(self):
    await self.fresh()
    parent = await self.conversation("group", "channel", 123)
    topic = await self.conversation("topic", "channel", 123, topic=42)
    await self.seed(parent, "needle hidden")
    await self.seed(topic, "needle visible")
    await self.db.grant_public(topic["id"])
    info, rows = await self.db.search(
      SearchQuery(123, "needle", None, None, None, str(topic["id"])), ANON,
    )
    self.assertEqual(info, {})
    self.assertEqual([r["conversation_id"] for r in rows], [topic["id"]])
    with self.assertRaises(GroupNotFound):
      await self.db.search(SearchQuery(123, "needle", None, None, None), ANON)

  async def test_wrong_archive_inserts_fail_at_database_boundary(self):
    await self.fresh()
    first = await self.conversation("group", "channel", 123)
    other = await self.conversation("group", "channel", 456)
    with self.assertRaises(asyncpg.ForeignKeyViolationError):
      await self.seed(dict(other) | {"archive_id": first["archive_id"]})

  async def test_new_archives_provision_once_concurrently_and_semantic_opt_in(self):
    await self.fresh()
    async def create():
      return await self.conversation("private_chat", "user", 123)
    rows = await asyncio.gather(create(), create())
    self.assertEqual(rows[0]["archive_id"], rows[1]["archive_id"])
    self.assertEqual(rows[0]["id"], rows[1]["id"])
    async with self.db.get_conn() as conn:
      name = archive_sql("{embeddings}", rows[0]["archive_id"]).strip('"')
      self.assertIsNone(await conn.fetchval("SELECT to_regclass($1)", f"{self.schema}.{name}"))
    await self.sql_file("migrations/005_semantic_search.sql")
    second = await self.conversation("private_chat", "user", 456)
    async with self.db.get_conn() as conn:
      for row in (rows[0], second):
        self.assertEqual(await ArchiveConnection(conn, row["archive_id"]).fetchval("SELECT count(*) FROM {embeddings}"), 0)

  async def test_unscoped_content_queries_rejected_and_sender_index_tracks_deletion(self):
    await self.fresh()
    row = await self.conversation("group", "channel", 123)
    await self.seed(row)
    await self.db.grant_public(row["id"])
    app = setup_app(self.db, None, "/tmp", "nobody.jpg", "ghost.jpg")
    async with TestClient(TestServer(app)) as client:
      for path in ("/search?q=needle", "/search?mode=semantic&q=needle", "/names?q=Sender"):
        self.assertEqual((await client.get(path)).status, 400)
      self.assertEqual((await client.get("/search?g=123&q=needle")).status, 200)
      self.assertEqual((await client.get("/names?g=123&q=Sender")).status, 200)
    self.assertTrue(await self.db.can_view_user(100, ANON))
    await self.db.delete_messages(123, [1], peer_type="channel")
    self.assertFalse(await self.db.can_view_user(100, ANON))

  async def legacy(self, *, vectors):
    await self.sql_file("tests/fixtures/schema_before_per_group.sql")
    ids = []
    async with self.db.get_conn() as conn:
      for kind, pt, pid, topic in (("group", "channel", 123, None), ("topic", "channel", 123, 42), ("private_chat", "user", 123, None)):
        cid = await conn.fetchval(
          """INSERT INTO conversations(kind,telegram_peer_type,telegram_peer_id,topic_id,name,legacy_group_id)
             VALUES($1,$2,$3,$4,'legacy',$5) RETURNING id""",
          kind, pt, pid, topic, None if kind == "private_chat" else pid,
        )
        ids.append(cid)
      await conn.execute("INSERT INTO tg_groups(group_id,name,conversation_id) VALUES(123,'legacy',$1)", ids[0])
      for i, cid in enumerate(ids):
        # Literal fixture SQL, all values bound.
        # pi-lens-ignore: python-sql-injection
        await conn.execute(
          """INSERT INTO messages(conversation_id,group_id,msgid,from_user,from_user_name,text,created_at,updated_at,deleted_at,topic_id,reply_to_id)
             VALUES($1,$2,1,100,'Old sender',$3,$4,$5,$6,$7,99)""",
          cid, 123 if i < 2 else None, "" if i == 1 else "needle legacy",
          DATE.replace(year=2020 + i), DATE, DATE if i == 1 else None, 42 if i == 1 else None,
        )
      # pi-lens-ignore: python-sql-injection
      await conn.execute("INSERT INTO public_conversation_access(conversation_id) VALUES($1)", ids[0])
      await conn.execute("INSERT INTO message_revisions(conversation_id,msgid,revision_type,text,from_user_name,created_at) VALUES($1,1,'delete','old text','Old sender',$2)", ids[1], DATE)
      if vectors:
        await conn.execute("""CREATE TABLE message_embeddings (
          conversation_id uuid, msgid bigint, created_at timestamptz, model text,
          content_hash text, embedding vector(512),
          PRIMARY KEY(conversation_id,msgid,created_at,model),
          FOREIGN KEY(conversation_id,msgid,created_at) REFERENCES messages(conversation_id,msgid,created_at) ON DELETE CASCADE)""")
        # pi-lens-ignore: python-sql-injection
        await conn.execute("INSERT INTO message_embeddings SELECT conversation_id,msgid,created_at,'legacy-model',md5(text),$1::text::vector FROM messages WHERE deleted_at IS NULL", json.dumps([1.0] + [0.0] * 511))
    return ids

  async def test_migrate_legacy_rows_vectors_grants_and_replay(self):
    ids = await self.legacy(vectors=True)
    await self.sql_file("migrations/006_per_group_storage.sql")
    await self.sql_file("migrations/006_per_group_storage.sql")
    async with self.db.get_conn() as conn:
      self.assertIsNone(await conn.fetchval("SELECT to_regclass($1)", f"{self.schema}.messages"))
      self.assertIsNone(await conn.fetchval("SELECT to_regclass($1)", f"{self.schema}.message_embeddings"))
      self.assertEqual(await conn.fetchval("SELECT count(*) FROM message_archives"), 2)
      for i, cid in enumerate(ids):
        scoped = await self.db.archive_conn(conn, cid)
        row = await scoped.fetchrow("SELECT * FROM {messages} WHERE conversation_id=$1", cid)
        self.assertEqual(row["created_at"].year, 2020 + i)
        self.assertEqual(row["reply_to_id"], 99)
        self.assertEqual(row["deleted_at"] is not None, i == 1)
        self.assertEqual(row["from_user_name"], "Old sender")
        self.assertEqual(await scoped.fetchval("SELECT count(*) FROM {embeddings} WHERE conversation_id=$1", cid), int(i != 1))
      self.assertEqual(await conn.fetchval("SELECT count(*) FROM message_revisions"), 1)
    self.assertTrue(await self.db.can_view_user(100, ANON))
    _, rows = await self.db.search(SearchQuery(123, "needle", None, None, None), ANON)
    self.assertEqual([r["conversation_id"] for r in rows], [ids[0]])

  async def test_migrate_without_semantic_then_enable_it(self):
    await self.legacy(vectors=False)
    await self.sql_file("migrations/006_per_group_storage.sql")
    await self.sql_file("migrations/005_semantic_search.sql")
    for archive in await self.db.list_archives():
      async with self.db.get_conn() as conn:
        self.assertEqual(await ArchiveConnection(conn, archive["id"]).fetchval("SELECT count(*) FROM {embeddings}"), 0)

  @unittest.skipUnless(os.environ.get("LUOXU_TEST_CUTWORDS"), "requires freshly built LUOXU_TEST_CUTWORDS binary")
  async def test_wordcloud_helper_reads_migrated_group_only(self):
    await self.legacy(vectors=False)
    await self.sql_file("migrations/006_per_group_storage.sql")
    other = await self.conversation("group", "channel", 456)
    await self.seed(other, "unrelatedsecret")
    url = str(DATABASE_URL)
    url += ("&" if "?" in url else "?") + "options=" + quote(f"-c search_path={self.schema},public")
    for gid, uid, expected in ((123, 0, 1), (123, 100, 1), (123, 999, 0), (456, 0, 1)):
      process = await asyncio.create_subprocess_exec(
        os.environ["LUOXU_TEST_CUTWORDS"], url, str(gid), "0", str(uid),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
      )
      out, err = await asyncio.wait_for(process.communicate(), timeout=30)
      self.assertEqual(process.returncode, 0, err.decode())
      self.assertEqual(int(out.splitlines()[0]), expected)
      if gid == 123:
        self.assertNotIn(b"unrelatedsecret", out)

  async def test_migration_failure_rolls_back_source_and_metadata(self):
    await self.legacy(vectors=False)
    async with self.db.get_conn() as conn:
      # External dependencies must prevent destructive cleanup (no CASCADE).
      await conn.execute("CREATE VIEW external_reader AS SELECT * FROM messages")
    with self.assertRaises(asyncpg.DependentObjectsStillExistError):
      await self.sql_file("migrations/006_per_group_storage.sql")
    async with self.db.get_conn() as conn:
      self.assertEqual(await conn.fetchval("SELECT count(*) FROM messages"), 3)
      self.assertIsNone(await conn.fetchval("SELECT to_regclass($1)", f"{self.schema}.message_archives"))
      self.assertFalse(await conn.fetchval("SELECT EXISTS(SELECT 1 FROM bootstrap_state WHERE name='per-peer-storage-v1')"))


if __name__ == "__main__":
  unittest.main()
