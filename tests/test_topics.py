"""Regression coverage for reply threads incorrectly becoming forum topics.

Unit tests: python -m unittest discover -s tests -v
Database tests additionally need LUOXU_TEST_DATABASE_URL pointing at a disposable
PostgreSQL/PGroonga instance. Each test uses and removes its own isolated schema.
"""

import datetime
import os
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

import asyncpg
from aiohttp.test_utils import TestClient, TestServer
from telethon.tl import types
from telethon.tl.patched import Message

from luoxu.auth import AuthService, Principal
from luoxu.db import PostgreStore
from luoxu.types import SearchQuery
from luoxu.util import UpdateLoaded
from luoxu.web import setup_app

UTC = datetime.timezone.utc
GROUP_ID = 1998301990
DATABASE_URL = os.environ.get("LUOXU_TEST_DATABASE_URL")


def channel(peer_id=GROUP_ID, **kwargs):
  return types.Channel(
    id=peer_id,
    title="Non-forum group",
    photo=types.ChatPhotoEmpty(),
    date=datetime.datetime(2024, 1, 1, tzinfo=UTC),
    megagroup=True,
    **kwargs,
  )


def message(msgid, *, peer=None, top_id=None, forum_topic=None, reply_id: int | None = 1):
  msg = Message(
    id=msgid,
    peer_id=peer or types.PeerChannel(GROUP_ID),
    from_id=types.PeerUser(100),
    date=datetime.datetime(2025, 1, 2, tzinfo=UTC),
    message=f"message {msgid}",
    reply_to=types.MessageReplyHeader(
      reply_to_msg_id=reply_id,
      reply_to_top_id=top_id,
      forum_topic=forum_topic,
    ),
  )
  msg._chat = channel()
  msg._sender = types.User(id=100, first_name="Sender")
  return msg


class TopicClassificationTests(unittest.TestCase):
  def test_ordinary_reply_threads_are_not_topics(self):
    for flag in (None, False):
      with self.subTest(forum_topic=flag):
        msg = message(402919, top_id=402821, forum_topic=flag)
        self.assertEqual(
          PostgreStore._peer_info(msg), ("group", "channel", GROUP_ID, None)
        )

  def test_forum_reply_uses_topic_root(self):
    msg = message(102, top_id=42, reply_id=101, forum_topic=True)
    self.assertEqual(
      PostgreStore._peer_info(msg), ("topic", "channel", GROUP_ID, 42)
    )

  def test_direct_reply_to_forum_root_uses_reply_id(self):
    msg = message(102, reply_id=42, forum_topic=True)
    self.assertEqual(
      PostgreStore._peer_info(msg), ("topic", "channel", GROUP_ID, 42)
    )

  def test_basic_groups_and_private_chats_never_have_topics(self):
    cases = [
      (types.PeerChat(123), ("group", "chat", 123, None)),
      (types.PeerUser(123), ("private_chat", "user", 123, None)),
    ]
    for peer, expected in cases:
      with self.subTest(peer=peer):
        msg = message(102, peer=peer, top_id=42, forum_topic=True)
        self.assertEqual(PostgreStore._peer_info(msg), expected)

  def test_repair_configuration_requires_explicit_integer_ids(self):
    self.assertEqual(PostgreStore({"url": "unused"}).repair_non_forum_groups, frozenset())
    for invalid in (None, "1998301990", [True], [1.5], ["1998301990"], [-1]):
      with (
        self.subTest(invalid=invalid),
        self.assertRaisesRegex(ValueError, "repair_non_forum_groups"),
      ):
        PostgreStore({"url": "unused"}, repair_non_forum_groups=invalid)

  def test_missing_topic_root_does_not_invent_a_topic(self):
    msg = message(102, reply_id=None, forum_topic=True)
    self.assertEqual(
      PostgreStore._peer_info(msg), ("group", "channel", GROUP_ID, None)
    )


@unittest.skipUnless(DATABASE_URL, "set LUOXU_TEST_DATABASE_URL for PostgreSQL tests")
class TopicStorageTests(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    self.schema = "topic_test_" + uuid.uuid4().hex
    self.admin = await asyncpg.connect(DATABASE_URL)
    await self.admin.execute("CREATE EXTENSION IF NOT EXISTS pgroonga")
    await self.admin.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    # The identifier is generated locally, never taken from a request.
    await self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
    self.db = PostgreStore(
      {"url": DATABASE_URL}, repair_non_forum_groups=[GROUP_ID],
    )
    self.db.pool = await asyncpg.create_pool(
      DATABASE_URL, min_size=1, max_size=2,
      server_settings={"search_path": f"{self.schema},public"},
    )
    async with self.db.get_conn() as conn:
      schema_sql = Path(__file__).resolve().parents[1] / "dbsetup.sql"
      await conn.execute(schema_sql.read_text())
      self.parent = await self.db.insert_group(conn, channel())
    self.parent_id = self.parent["conversation_uuid"]

  async def asyncTearDown(self):
    await self.db.close()
    await self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
    await self.admin.close()

  async def seed_topic(self, conn, topic_id, peer_id=GROUP_ID):
    row = await self.db._ensure_conversation(
      conn, "topic", "channel", peer_id, "Non-forum group", topic_id=topic_id,
      legacy_group_id=peer_id,
    )
    return row["id"]

  async def seed_message(
    self, conn, cid, msgid, *, topic_id=None, text="hello", year=2025,
    edited=None, deleted=None, reply_to=None,
  ):
    await conn.execute(
      """
      INSERT INTO messages (
        conversation_id, group_id, msgid, topic_id, text, from_user,
        from_user_name, created_at, updated_at, deleted_at, reply_to_id,
        quote_text, media
      ) VALUES ($1, $2, $3, $4, $5, 100, 'Sender', $6, $7, $8, $9,
                'quoted text', '{"type":"MessageMediaPhoto","id":123}'::jsonb)
      """,
      cid, GROUP_ID, msgid, topic_id, text,
      datetime.datetime(year, 1, 2, tzinfo=UTC), edited, deleted, reply_to,
    )

  async def test_ordinary_reply_ingestion_lists_only_one_group(self):
    msgs = [message(402900 + i, top_id=402800 + i) for i in range(4)]
    await self.db.insert_messages(msgs, UpdateLoaded.update_none)
    rows = await self.db.list_all_conversations()
    self.assertEqual([(r["kind"], r["id"]) for r in rows], [("group", self.parent_id)])
    async with self.db.get_conn() as conn:
      self.assertEqual(await conn.fetchval("SELECT count(*) FROM messages"), 4)
      self.assertEqual(
        await conn.fetchval("SELECT count(*) FROM messages WHERE topic_id IS NOT NULL"),
        0,
      )
    await self.db.grant_public(self.parent_id)
    async with self.api_client() as client:
      response = await client.get("/api/luoxu/conversations")
      self.assertEqual(response.status, 200)
      payload = await response.json()
      self.assertEqual(len(payload["conversations"]), 1)
      self.assertEqual(payload["conversations"][0]["kind"], "group")

  async def test_startup_repairs_messages_history_and_grants(self):
    async with self.db.get_conn() as conn:
      false_topic = await self.seed_topic(conn, 402821)
      second_topic = await self.seed_topic(conn, 402858)
      await self.db.insert_group(conn, channel(GROUP_ID + 1, forum=True))
      real_topic = await self.seed_topic(conn, 42, peer_id=GROUP_ID + 1)
      await self.seed_message(conn, self.parent_id, 1, year=2024, text="root")
      await self.seed_message(conn, false_topic, 2, topic_id=402821, reply_to=1)
      deleted = datetime.datetime(2025, 1, 3, tzinfo=UTC)
      await self.seed_message(conn, second_topic, 3, topic_id=402858, deleted=deleted, text="")
      await conn.execute(
        """
        INSERT INTO message_revisions (
          conversation_id, msgid, revision_type, text, from_user_name,
          topic_id, created_at, reply_to_id, quote_text, media
        ) VALUES ($1, 3, 'delete', 'pre-deletion text', 'Sender', 402858,
                  '2025-01-02', 2, 'old quote', '{"id":123}')
        """, second_topic,
      )
    user = await self.db.create_user("reader", "unused-test-hash")
    topic_user = await self.db.create_user("topic_reader", "unused-test-hash")
    await self.db.grant_conversation(user["id"], self.parent_id)
    await self.db.grant_conversation(topic_user["id"], false_topic)
    await self.db.grant_public(false_topic)

    async with self.db.get_conn() as conn:
      repaired = await self.db.insert_group(conn, channel())
      self.assertEqual(repaired["conversation_uuid"], self.parent_id)
      self.assertEqual(await conn.fetchval("SELECT count(*) FROM messages"), 3)
      rows = await conn.fetch("SELECT * FROM messages ORDER BY msgid")
      self.assertTrue(all(r["conversation_id"] == self.parent_id for r in rows))
      self.assertTrue(all(r["topic_id"] is None for r in rows))
      self.assertEqual(rows[1]["reply_to_id"], 1)
      self.assertEqual(rows[1]["quote_text"], "quoted text")
      self.assertIn("MessageMediaPhoto", rows[1]["media"])
      self.assertEqual(rows[2]["deleted_at"], deleted)
      revision = await conn.fetchrow("SELECT * FROM message_revisions")
      self.assertEqual(revision["conversation_id"], self.parent_id)
      self.assertIsNone(revision["topic_id"])
      self.assertEqual(revision["text"], "pre-deletion text")
      self.assertEqual(revision["reply_to_id"], 2)
      self.assertEqual(revision["quote_text"], "old quote")
      self.assertIsNotNone(await self.db.get_group(conn, GROUP_ID))
      remaining = await conn.fetch("SELECT id FROM conversations WHERE kind = 'topic'")
      self.assertEqual([r["id"] for r in remaining], [real_topic])

    reader = Principal(str(user["id"]), "reader", is_anonymous=False)
    restricted = Principal(str(topic_user["id"]), "topic_reader", is_anonymous=False)
    self.assertTrue(await self.db.can_access(self.parent_id, reader))
    self.assertFalse(await self.db.can_access(self.parent_id, restricted))
    self.assertFalse(await self.db.can_access(self.parent_id, Principal(None, None)))
    self.assertEqual(await self.db.list_user_grants(topic_user["id"]), [])
    context = await self.db.get_context(self.parent_id, 2, reader)
    if context is None:
      self.fail("repaired reply message must be available in the group context")
    self.assertEqual(context["replies"][0]["msgid"], 1)
    _, results = await self.db.search(SearchQuery(GROUP_ID, None, None, None, None), reader)
    self.assertEqual({r["msgid"] for r in results}, {1, 2})
    # Repeated startup must be a no-op, including while history is disabled.
    async with self.db.get_conn() as conn:
      await self.db.insert_group(conn, channel())
      self.assertEqual(await conn.fetchval("SELECT count(*) FROM message_revisions"), 1)
      self.assertEqual(await conn.fetchval("SELECT count(*) FROM messages"), 3)

  async def test_repair_requires_operator_confirmation(self):
    self.db.repair_non_forum_groups = frozenset()
    async with self.db.get_conn() as conn:
      topic_id = await self.seed_topic(conn, 42)
      await self.seed_message(conn, topic_id, 1, topic_id=42)
      await self.db.insert_group(conn, channel())
      self.assertEqual(await conn.fetchval(
        "SELECT conversation_id FROM messages WHERE msgid = 1"
      ), topic_id)
      self.assertTrue(await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM conversations WHERE id = $1)", topic_id,
      ))

  async def test_real_forums_and_incomplete_entities_are_not_repaired(self):
    variants = [
      channel(forum=True), channel(min=True), channel(monoforum=True),
      SimpleNamespace(id=GROUP_ID, title="Unknown entity"),
    ]
    async with self.db.get_conn() as conn:
      topic_id = await self.seed_topic(conn, 42)
      for entity in variants:
        await self.db.insert_group(conn, entity)
        self.assertTrue(await conn.fetchval(
          "SELECT EXISTS(SELECT 1 FROM conversations WHERE id = $1)", topic_id,
        ))

  async def test_repair_merges_duplicates_without_resurrecting_deletions(self):
    older = datetime.datetime(2025, 1, 3, tzinfo=UTC)
    newer = datetime.datetime(2025, 1, 4, tzinfo=UTC)
    async with self.db.get_conn() as conn:
      first = await self.seed_topic(conn, 42)
      second = await self.seed_topic(conn, 43)
      await self.seed_message(conn, self.parent_id, 1, text="old", edited=older)
      await self.seed_message(conn, first, 1, topic_id=42, text="new", edited=newer)
      await self.seed_message(conn, second, 1, topic_id=43, text="oldest")
      await self.seed_message(conn, self.parent_id, 2, text="", deleted=older)
      await self.seed_message(conn, first, 2, topic_id=42, text="stale replay", edited=newer)
      await self.seed_message(conn, self.parent_id, 3, text="stale replay", edited=newer)
      await self.seed_message(conn, first, 3, topic_id=42, text="", deleted=older)
      await self.db.insert_group(conn, channel())
      rows = await conn.fetch("SELECT * FROM messages ORDER BY msgid")
      self.assertEqual(len(rows), 3)
      self.assertEqual(rows[0]["text"], "new")
      self.assertEqual(rows[0]["updated_at"], newer)
      self.assertTrue(all(r["deleted_at"] is not None for r in rows[1:]))
      self.assertTrue(all(r["text"] == "" for r in rows[1:]))
      self.assertTrue(all(r["topic_id"] is None for r in rows))
      self.assertEqual(await conn.fetchval("SELECT count(*) FROM message_revisions"), 0)

  def api_client(self, auth=None):
    app = setup_app(
      self.db, None, str(Path(__file__).parent), "nobody.jpg", "ghost.jpg",
      prefix="/api/luoxu", auth_service=auth,
    )
    return TestClient(TestServer(app))

  async def test_legacy_context_url_matches_uuid_context(self):
    async with self.db.get_conn() as conn:
      await self.seed_message(conn, self.parent_id, 402867, year=2023)
      await self.seed_message(conn, self.parent_id, 402868, year=2024, reply_to=402867)
      await self.seed_message(conn, self.parent_id, 402869, year=2025)
    await self.db.grant_public(self.parent_id)
    async with self.api_client() as client:
      response = await client.get(f"/api/luoxu/context?g={GROUP_ID}&id=402868")
      self.assertEqual(response.status, 200)
      payload = await response.json()
      self.assertEqual(payload["target"]["id"], 402868)
      self.assertEqual(payload["target"]["conversation_id"], str(self.parent_id))
      self.assertEqual([m["id"] for m in payload["before"]], [402867])
      self.assertEqual([m["id"] for m in payload["after"]], [402869])
      self.assertEqual([m["id"] for m in payload["replies"]], [402867])
      canonical = await client.get(
        f"/api/luoxu/conversations/{self.parent_id}/messages/402868/context"
      )
      self.assertEqual(canonical.status, 200)
      self.assertEqual(await canonical.json(), payload)
      self.assertEqual(response.headers["Cache-Control"], "private, no-store")
      limited = await client.get(
        f"/api/luoxu/context?g={GROUP_ID}&id=402868&before=0&after=0&depth=0"
      )
      limited_payload = await limited.json()
      self.assertEqual(limited_payload["before"], [])
      self.assertEqual(limited_payload["after"], [])
      self.assertEqual(limited_payload["replies"], [])

  async def test_legacy_context_enforces_actual_topic_access(self):
    async with self.db.get_conn() as conn:
      allowed_topic = await self.seed_topic(conn, 42)
      hidden_topic = await self.seed_topic(conn, 43)
      await self.seed_message(conn, self.parent_id, 1, year=2023)
      await self.seed_message(conn, allowed_topic, 2, topic_id=42, year=2024, reply_to=1)
      await self.seed_message(conn, hidden_topic, 3, topic_id=43, year=2025)
    auth = AuthService({"jwt_secret": AuthService.random_secret()})
    user = await self.db.create_user("topic_reader", "unused-test-hash")
    await self.db.grant_conversation(user["id"], allowed_topic)
    headers = {"Authorization": f"Bearer {auth.access_token(user)}"}
    async with self.api_client(auth) as client:
      anonymous = await client.get(f"/api/luoxu/context?g={GROUP_ID}&id=2")
      self.assertEqual(anonymous.status, 404)
      allowed = await client.get(
        f"/api/luoxu/context?g={GROUP_ID}&id=2", headers=headers,
      )
      self.assertEqual(allowed.status, 200)
      payload = await allowed.json()
      self.assertEqual(payload["target"]["conversation_id"], str(allowed_topic))
      self.assertEqual(payload["before"], [])
      self.assertEqual(payload["after"], [])
      self.assertEqual(payload["replies"], [{"msgid": 1, "status": "unavailable"}])
      for msgid in (1, 3, 404):
        hidden = await client.get(
          f"/api/luoxu/context?g={GROUP_ID}&id={msgid}", headers=headers,
        )
        self.assertEqual(hidden.status, 404)
      await self.db.revoke_conversation(user["id"], allowed_topic)
      revoked = await client.get(f"/api/luoxu/context?g={GROUP_ID}&id=2", headers=headers)
      self.assertEqual(revoked.status, 404)

  async def test_legacy_context_rejects_bad_parameters(self):
    cases = [
      "", "?g=1", "?id=1", "?g=abc&id=1", "?g=1&id=abc",
      "?g=0&id=1", "?g=1&id=-1", "?g=1&id=0",
      f"?g={2**64}&id=1", f"?g=1&id={2**64}",
    ]
    async with self.api_client() as client:
      for query in cases:
        with self.subTest(query=query):
          response = await client.get("/api/luoxu/context" + query)
          self.assertEqual(response.status, 400)

  async def test_repair_is_transactional(self):
    async with self.db.get_conn() as conn:
      false_topic = await self.seed_topic(conn, 42)
      await self.seed_message(conn, false_topic, 1, topic_id=42)
    with self.assertRaisesRegex(RuntimeError, "rollback probe"):
      async with self.db.get_conn() as conn:
        await self.db.insert_group(conn, channel())
        raise RuntimeError("rollback probe")
    async with self.db.get_conn() as conn:
      row = await conn.fetchrow("SELECT * FROM messages")
      self.assertEqual(row["conversation_id"], false_topic)
      self.assertIsNotNone(await conn.fetchrow(
        "SELECT id FROM conversations WHERE id = $1", false_topic,
      ))


if __name__ == "__main__":
  unittest.main()
