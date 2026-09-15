"""Admin group HTTP tests with real storage/indexer integration, no Telegram I/O.

Set LUOXU_TEST_DATABASE_URL to a disposable PostgreSQL/PGroonga instance.
Each test creates and drops its own isolated schema.
"""

import asyncio
import datetime
import os
import secrets
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import create_autospec, patch

import asyncpg
from aiohttp.test_utils import TestClient, TestServer
from telethon import TelegramClient
from telethon.tl import types

from luoxu.__main__ import Indexer
from luoxu.auth import AuthService, Principal
from luoxu.db import PostgreStore
from luoxu.web import setup_app

DATABASE_URL = os.environ.get("LUOXU_TEST_DATABASE_URL")
GROUP_ID = 1968895590
MARKED_ID = "-1001968895590"
PREFIX = "/api/luoxu"


@unittest.skipUnless(DATABASE_URL, "set LUOXU_TEST_DATABASE_URL for PostgreSQL tests")
class AdminGroupTests(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    self.schema = "admin_group_test_" + uuid.uuid4().hex
    self.admin = await asyncpg.connect(DATABASE_URL)
    await self.admin.execute("CREATE EXTENSION IF NOT EXISTS pgroonga")
    await self.admin.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    await self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
    self.db = PostgreStore({"url": DATABASE_URL})
    self.db.pool = await asyncpg.create_pool(
      DATABASE_URL,
      min_size=1,
      max_size=1,
      server_settings={"search_path": f"{self.schema},public"},
    )
    async with self.db.get_conn() as conn:
      # Trusted fixture DDL from a fixed repository path, never request input.
      # pi-lens-ignore: python-sql-injection
      await conn.execute(
        (Path(__file__).resolve().parents[1] / "dbsetup.sql").read_text()
      )
    self.auth = AuthService({"jwt_secret": "admin-group-test-secret-" * 3})
    # Requests use real signed JWTs and database-backed authorization, not login.
    self.admin_user = await self.db.create_user("admin", "unused-test-hash", True)
    self.headers = {
      "Authorization": "Bearer " + self.auth.access_token(self.admin_user)
    }
    self.entity = types.Channel(
      id=GROUP_ID,
      title="Monitored private group",
      photo=types.ChatPhotoEmpty(),
      date=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc),
      megagroup=True,
    )
    self.telegram = create_autospec(TelegramClient, instance=True)
    self.telegram.get_entity.return_value = self.entity
    self.indexer = Indexer({"telegram": {}})
    self.indexer.client = self.telegram
    self.indexer.dbstore = self.db
    self.history_calls = []
    self.history_started = asyncio.Event()

    async def history(instance, client, store, callback):
      self.history_calls.append(instance)
      self.history_started.set()
      callback()

    history_patch = patch("luoxu.__main__.GroupHistoryIndexer.run", new=history)
    history_patch.start()
    self.addCleanup(history_patch.stop)
    cache = tempfile.TemporaryDirectory()
    self.addCleanup(cache.cleanup)
    root = Path(__file__).resolve().parents[1]
    app = setup_app(
      self.db,
      self.telegram,
      cache.name,
      str(root / "nobody.jpg"),
      str(root / "ghost.jpg"),
      prefix=PREFIX,
      auth_service=self.auth,
      add_group=self.indexer.add_group,
    )
    self.client = TestClient(TestServer(app))
    await self.client.start_server()

  async def asyncTearDown(self):
    await self.client.close()
    await self.db.close()
    await self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
    await self.admin.close()

  async def post_group(self, target=MARKED_ID):
    return await self.client.post(
      PREFIX + "/admin/groups",
      json={"group": target},
      headers=self.headers,
    )

  async def login_user(self, username):
    password = secrets.token_urlsafe(24)
    response = await self.client.post(
      PREFIX + "/admin/users",
      headers=self.headers,
      json={"username": username, "password": password, "is_admin": False},
    )
    self.assertEqual(response.status, 201, await response.text())
    user = (await response.json())["user"]
    response = await self.client.post(
      PREFIX + "/auth/login",
      json={"username": username, "password": password},
    )
    self.assertEqual(response.status, 200, await response.text())
    token = (await response.json())["access_token"]
    return user["id"], {"Authorization": "Bearer " + token}

  async def seed_group_message(self, group_id, sender_id):
    entity = types.Channel(
      id=group_id,
      title=f"Group {group_id}",
      photo=types.ChatPhotoEmpty(),
      date=self.entity.date,
      megagroup=True,
    )
    self.telegram.get_entity.return_value = entity
    response = await self.post_group(-1000000000000 - group_id)
    self.assertEqual(response.status, 201, await response.text())
    conversation = (await response.json())["conversation"]
    self.assertFalse(conversation["is_public"])
    cid = conversation["id"]
    async with self.db.get_conn() as conn:
      # Every value is bound through $1-$5; the SQL and identifiers are literal.
      # pi-lens-ignore: python-sql-injection
      await conn.execute(
        """INSERT INTO messages
          (conversation_id, group_id, msgid, text, from_user, from_user_name, created_at)
          VALUES ($1, $2, 1, 'visibility marker', $3, $4, $5)""",
        cid,
        group_id,
        sender_id,
        f"sender {sender_id}",
        datetime.datetime(2025, 1, 2, tzinfo=datetime.timezone.utc),
      )
    return {"cid": cid, "group_id": group_id, "sender_id": sender_id}

  async def assert_content_access(self, headers, groups, allowed):
    response = await self.client.get(PREFIX + "/conversations", headers=headers)
    self.assertEqual(response.status, 200)
    self.assertEqual(
      {r["id"] for r in (await response.json())["conversations"]}, allowed
    )
    response = await self.client.get(PREFIX + "/groups", headers=headers)
    self.assertEqual(response.status, 200)
    self.assertEqual(
      {r["conversation_id"] for r in (await response.json())["groups"]},
      allowed,
    )
    search = {"q": "visibility", "start": "1735689600", "end": "1735862400"}
    response = await self.client.get(PREFIX + "/search", params=search, headers=headers)
    self.assertEqual(response.status, 200, await response.text())
    self.assertEqual(
      {r["conversation_id"] for r in (await response.json())["messages"]},
      allowed,
    )
    response = await self.client.get(
      PREFIX + "/names", params={"q": "sender"}, headers=headers
    )
    self.assertEqual(response.status, 200)
    self.assertEqual(
      {r[0] for r in (await response.json())["names"]},
      {group["sender_id"] for group in groups if group["cid"] in allowed},
    )
    for group in groups:
      cid, gid, sender = group["cid"], group["group_id"], group["sender_id"]
      expected = 200 if cid in allowed else 404
      # Supplying a guessed UUID, legacy group ID, or sender ID never bypasses grants.
      for path in (
        f"/conversations/{cid}/messages/1",
        f"/conversations/{cid}/messages/1/context",
        f"/context?g={gid}&id=1",
        f"/avatar/{sender}.jpg",
      ):
        response = await self.client.get(PREFIX + path, headers=headers)
        self.assertEqual(
          response.status,
          expected,
          (path, await response.text() if response.status != 200 else ""),
        )
        await response.read()
      response = await self.client.get(
        PREFIX + "/search", params={**search, "g": gid}, headers=headers
      )
      self.assertEqual(response.status, expected)
      response = await self.client.get(
        PREFIX + "/search", params={**search, "conversation_id": cid}, headers=headers
      )
      self.assertEqual(response.status, 200)
      self.assertEqual(len((await response.json())["messages"]), int(cid in allowed))
      response = await self.client.get(
        PREFIX + "/names", params={"q": "sender", "g": gid}, headers=headers
      )
      self.assertEqual(response.status, 200)
      self.assertEqual(len((await response.json())["names"]), int(cid in allowed))

  async def test_two_logins_have_independent_grants_and_revocation_is_immediate(self):
    alice_id, alice = await self.login_user("alice")
    bob_id, bob = await self.login_user("bob")
    self.assertNotEqual(alice["Authorization"], bob["Authorization"])
    groups = [await self.seed_group_message(GROUP_ID + i, 800000 + i) for i in range(3)]
    private_a, private_b, public = [group["cid"] for group in groups]
    for uid, cid in ((alice_id, private_a), (bob_id, private_b)):
      response = await self.client.put(
        PREFIX + f"/admin/users/{uid}/grants/{cid}", headers=self.headers
      )
      self.assertEqual(response.status, 204)
    response = await self.client.post(
      PREFIX + f"/admin/public/{public}", headers=self.headers
    )
    self.assertEqual(response.status, 204)
    await self.assert_content_access(alice, groups, {private_a, public})
    await self.assert_content_access(bob, groups, {private_b, public})
    await self.assert_content_access({}, groups, {public})
    # Management authority is not an implicit bypass of content permissions.
    await self.assert_content_access(self.headers, groups, {public})
    admin_grant = PREFIX + f"/admin/users/{self.admin_user['id']}/grants/{private_a}"
    response = await self.client.put(admin_grant, headers=self.headers)
    self.assertEqual(response.status, 204)
    await self.assert_content_access(self.headers, groups, {private_a, public})
    response = await self.client.delete(admin_grant, headers=self.headers)
    self.assertEqual(response.status, 204)
    await self.assert_content_access(self.headers, groups, {public})
    response = await self.client.get(
      PREFIX + "/admin/conversations", headers=self.headers
    )
    self.assertEqual(
      {r["id"] for r in (await response.json())["conversations"]},
      {private_a, private_b, public},
    )
    response = await self.client.get(
      PREFIX + f"/admin/users/{alice_id}/grants", headers=self.headers
    )
    self.assertEqual(
      {r["id"] for r in (await response.json())["conversations"]}, {private_a}
    )
    response = await self.client.get(PREFIX + "/admin/public", headers=self.headers)
    self.assertEqual(
      {r["id"] for r in (await response.json())["conversations"]}, {public}
    )
    response = await self.client.delete(
      PREFIX + f"/admin/users/{alice_id}/grants/{private_a}", headers=self.headers
    )
    self.assertEqual(response.status, 204)
    # Existing access tokens immediately reflect permission changes; no re-login.
    await self.assert_content_access(alice, groups, {public})
    response = await self.client.delete(
      PREFIX + f"/admin/public/{public}", headers=self.headers
    )
    self.assertEqual(response.status, 204)
    await self.assert_content_access(alice, groups, set())
    await self.assert_content_access(bob, groups, {private_b})
    await self.assert_content_access({}, groups, set())
    await self.assert_content_access(self.headers, groups, set())
    self.assertEqual(len(await self.db.list_all_conversations()), 3)

  async def test_only_admin_can_monitor_or_change_account_grants(self):
    user_id, ordinary = await self.login_user("ordinary")
    info = await self.indexer.init_group(self.entity)
    cid = str(info["conversation_uuid"])
    actions = [
      ("POST", "/admin/groups", {"group": MARKED_ID}),
      ("GET", "/admin/conversations", None),
      ("GET", "/admin/public", None),
      ("GET", f"/admin/users/{user_id}/grants", None),
      ("PUT", f"/admin/users/{user_id}/grants/{cid}", None),
      ("DELETE", f"/admin/users/{user_id}/grants/{cid}", None),
      ("POST", f"/admin/public/{cid}", None),
      ("DELETE", f"/admin/public/{cid}", None),
      ("PATCH", f"/admin/users/{user_id}", {"is_admin": True}),
    ]
    for headers, status in (({}, 401), (ordinary, 403)):
      for method, path, body in actions:
        response = await self.client.request(
          method, PREFIX + path, json=body, headers=headers
        )
        self.assertEqual(response.status, status, (method, path))
    self.telegram.get_entity.assert_not_awaited()
    self.assertEqual(self.history_calls, [])
    self.assertEqual(await self.db.list_user_grants(user_id), [])
    self.assertEqual(await self.db.list_public_grants(), [])
    self.assertFalse((await self.db.get_user(user_id))["is_admin"])

  async def test_public_group_topics_inherit_but_private_chats_cannot_be_public(self):
    user_id, alice = await self.login_user("topic-user")
    info = await self.indexer.init_group(self.entity)
    cid = str(info["conversation_uuid"])
    async with self.db.get_conn() as conn:
      topic = await self.db._ensure_conversation(
        conn,
        "topic",
        "channel",
        GROUP_ID,
        "Forum topic",
        topic_id=123,
        legacy_group_id=GROUP_ID,
      )
      private = await self.db._ensure_conversation(
        conn, "private_chat", "user", 654321, "Explicit private archive"
      )
    topic_id, private_id = str(topic["id"]), str(private["id"])
    response = await self.client.post(
      PREFIX + f"/admin/public/{cid}", headers=self.headers
    )
    self.assertEqual(response.status, 204)
    response = await self.client.get(
      PREFIX + "/admin/conversations", headers=self.headers
    )
    rows = {r["id"]: r for r in (await response.json())["conversations"]}
    self.assertTrue(rows[cid]["is_public"])
    self.assertTrue(rows[topic_id]["is_public"])
    self.assertFalse(rows[private_id]["is_public"])
    response = await self.client.post(
      PREFIX + f"/admin/public/{private_id}", headers=self.headers
    )
    self.assertEqual(response.status, 400)
    response = await self.client.put(
      PREFIX + f"/admin/users/{user_id}/grants/{private_id}", headers=self.headers
    )
    self.assertEqual(response.status, 204)
    for headers, expected in (
      ({}, {cid, topic_id}),
      (alice, {cid, topic_id, private_id}),
    ):
      response = await self.client.get(PREFIX + "/conversations", headers=headers)
      self.assertEqual(
        {r["id"] for r in (await response.json())["conversations"]}, expected
      )

  async def test_invalid_group_bodies_do_not_start_monitoring(self):
    for body in (
      {},
      [],
      {"group": True},
      {"group": False},
      {"group": None},
      {"group": " "},
      {"group": []},
    ):
      response = await self.client.post(
        PREFIX + "/admin/groups", json=body, headers=self.headers
      )
      self.assertEqual(response.status, 400)
    self.telegram.get_entity.assert_not_awaited()
    self.assertEqual(self.history_calls, [])

  async def test_web_only_mode_reports_monitoring_unavailable(self):
    app = setup_app(
      self.db,
      None,
      ".",
      "nobody.jpg",
      "ghost.jpg",
      prefix=PREFIX,
      auth_service=self.auth,
    )
    async with TestClient(TestServer(app)) as client:
      response = await client.post(
        PREFIX + "/admin/groups",
        json={"group": MARKED_ID},
        headers=self.headers,
      )
      self.assertEqual(response.status, 503)
    self.telegram.get_entity.assert_not_awaited()
    self.assertEqual(await self.db.list_all_conversations(), [])

  async def test_missing_conversation_metadata_returns_503_not_serialization_error(
    self,
  ):
    with patch.object(self.db, "get_conversation", return_value=None):
      response = await self.post_group()
      self.assertEqual(response.status, 503)
      self.assertIn("group conversation is unavailable", await response.text())

  async def test_new_group_starts_history_and_returns_canonical_conversation(self):
    response = await self.post_group()
    await asyncio.wait_for(self.history_started.wait(), 0.5)
    self.telegram.get_entity.assert_awaited_once_with(int(MARKED_ID))
    self.assertEqual(response.status, 201, await response.text())
    body = await response.json()
    rows = await self.db.list_all_conversations()
    self.assertEqual(len(rows), 1)
    self.assertEqual(
      body,
      {
        "conversation": {
          "id": str(rows[0]["id"]),
          "kind": "group",
          "name": self.entity.title,
          "telegram_peer_type": "channel",
          "telegram_peer_id": GROUP_ID,
          "topic_id": None,
          "pub_id": None,
          "legacy_group_id": GROUP_ID,
          "is_public": False,
        }
      },
    )
    self.assertEqual(len(self.history_calls), 1)
    # History still receives its loading cursors, not the public API DTO.
    self.assertIn("loaded_first_id", self.history_calls[0].group_info)
    self.assertIn("loaded_last_id", self.history_calls[0].group_info)
    self.assertEqual(self.telegram.add_event_handler.call_count, 2)
    self.assertEqual(await self.db.list_conversations(Principal(None, None)), [])

  async def test_admin_list_distinguishes_public_from_monitored_groups(self):
    # A Telegram username is not a grant to anonymous Luoxu visitors.
    self.entity.username = "telegram_public_link"
    first = await self.indexer.init_group(self.entity)
    second_entity = types.Chat(
      id=12345,
      title="Explicitly public in Luoxu",
      photo=types.ChatPhotoEmpty(),
      participants_count=2,
      date=self.entity.date,
      version=1,
    )
    second = await self.indexer.init_group(second_entity)
    response = await self.client.post(
      PREFIX + f"/admin/public/{second['conversation_uuid']}",
      headers=self.headers,
    )
    self.assertEqual(response.status, 204)
    response = await self.client.get(
      PREFIX + "/admin/conversations", headers=self.headers
    )
    self.assertEqual(response.status, 200)
    rows = {row["id"]: row for row in (await response.json())["conversations"]}
    self.assertFalse(rows[str(first["conversation_uuid"])]["is_public"])
    self.assertEqual(
      rows[str(first["conversation_uuid"])]["pub_id"], "telegram_public_link"
    )
    self.assertTrue(rows[str(second["conversation_uuid"])]["is_public"])
    self.assertIsNone(rows[str(second["conversation_uuid"])]["pub_id"])

  async def test_already_monitored_group_returns_conversation_without_restarting(self):
    info = await self.indexer.init_group(self.entity)
    self.indexer.group_forward_history_done[GROUP_ID] = True
    await self.db.grant_public(info["conversation_uuid"])
    response = await self.post_group()
    self.assertEqual(response.status, 201, await response.text())
    conversation = (await response.json())["conversation"]
    self.assertEqual(conversation["id"], str(info["conversation_uuid"]))
    self.assertTrue(conversation["is_public"])
    self.assertEqual(len(await self.db.list_public_grants()), 1)
    self.assertEqual(self.history_calls, [])
    self.telegram.add_event_handler.assert_not_called()
    self.assertEqual(len(await self.db.list_all_conversations()), 1)


if __name__ == "__main__":
  unittest.main()
