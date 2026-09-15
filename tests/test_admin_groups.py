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
from unittest.mock import AsyncMock, create_autospec, patch

import asyncpg
from aiohttp.test_utils import TestClient, TestServer
from telethon import TelegramClient
from telethon.tl import types

from luoxu.__main__ import Indexer
from luoxu.auth import AuthService, Principal
from luoxu.db import PostgreStore
from luoxu.group import GroupHistoryIndexer
from luoxu.web import setup_app

DATABASE_URL = os.environ.get("LUOXU_TEST_DATABASE_URL")
GROUP_ID = 1968895590
MARKED_ID = "-1001968895590"
PREFIX = "/api/luoxu"


@unittest.skipUnless(DATABASE_URL, "set LUOXU_TEST_DATABASE_URL for PostgreSQL tests")
class AdminGroupFixture(unittest.IsolatedAsyncioTestCase):
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

    self.real_history_run = GroupHistoryIndexer.run
    history_patch = patch("luoxu.group.GroupHistoryIndexer.run", new=history)
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
      monitoring_changed=self.indexer.sync_monitoring,
      monitoring_status=self.indexer.monitoring_status,
    )
    self.client = TestClient(TestServer(app))
    await self.client.start_server()

  async def asyncTearDown(self):
    await self.client.close()
    await self.indexer.close_group_monitoring()
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
        PREFIX + "/search",
        params={**search, "conversation_id": cid},
        headers=headers,
      )
      self.assertEqual(response.status, 200)
      self.assertEqual(len((await response.json())["messages"]), int(cid in allowed))
      response = await self.client.get(
        PREFIX + "/names", params={"q": "sender", "g": gid}, headers=headers
      )
      self.assertEqual(response.status, 200)
      self.assertEqual(len((await response.json())["names"]), int(cid in allowed))


class AdminGroupTests(AdminGroupFixture):
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
    self.assertEqual(self.telegram.add_event_handler.call_count, 3)
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

  async def test_already_monitored_group_returns_conversation_without_restarting(
    self,
  ):
    first = await self.post_group()
    self.assertEqual(first.status, 201)
    cid = (await first.json())["conversation"]["id"]
    await asyncio.wait_for(self.history_started.wait(), 1)
    await self.db.grant_public(cid)
    response = await self.post_group()
    self.assertEqual(response.status, 201, await response.text())
    conversation = (await response.json())["conversation"]
    self.assertEqual(conversation["id"], cid)
    self.assertTrue(conversation["is_public"])
    self.assertEqual(len(await self.db.list_public_grants()), 1)
    self.assertEqual(len(self.history_calls), 1)
    self.assertEqual(self.telegram.add_event_handler.call_count, 3)
    self.assertEqual(len(await self.db.list_all_conversations()), 1)


@unittest.skipUnless(DATABASE_URL, "set LUOXU_TEST_DATABASE_URL for PostgreSQL tests")
class MonitoringTests(AdminGroupFixture):
  async def monitoring_state(self, cid):
    response = await self.client.get(
      PREFIX + "/admin/conversations", headers=self.headers
    )
    self.assertEqual(response.status, 200)
    rows = {r["id"]: r for r in (await response.json())["conversations"]}
    self.assertIn(cid, rows)
    return rows[cid]["monitoring"]

  async def test_restart_restores_a_manually_added_unshared_group(self):
    response = await self.post_group()
    self.assertEqual(response.status, 201)
    cid = (await response.json())["conversation"]["id"]
    self.assertEqual(await self.db.list_public_grants(), [])
    await self.indexer.close_group_monitoring()
    self.history_started.clear()
    restarted = Indexer(
      {
        "telegram": {},
        "web": {"auth": {"jwt_secret": "admin-group-test-secret-" * 3}},
      }
    )
    restarted.client = self.telegram
    restarted.dbstore = self.db
    ready, disconnected = asyncio.Event(), asyncio.Event()

    async def connected():
      ready.set()
      await disconnected.wait()

    self.telegram.run_until_disconnected = AsyncMock(side_effect=connected)
    task = asyncio.create_task(restarted.run_on_connected(self.telegram, self.db, []))
    try:
      await asyncio.wait_for(ready.wait(), 2)
      await asyncio.wait_for(self.history_started.wait(), 2)
      self.assertIn(
        GROUP_ID,
        restarted.group_forward_history_done,
        "persisted group was not restored after restart",
      )
      self.assertEqual(str((await self.db.get_conversation(cid))["id"]), cid)
    finally:
      disconnected.set()
      await asyncio.wait_for(task, 2)

  async def test_removing_manual_reference_keeps_account_reference_alive(self):
    response = await self.post_group()
    self.assertEqual(response.status, 201)
    cid = (await response.json())["conversation"]["id"]
    user_id, _ = await self.login_user("reference-owner")
    response = await self.client.put(
      PREFIX + f"/admin/users/{user_id}/grants/{cid}",
      headers=self.headers,
    )
    self.assertEqual(response.status, 204)
    response = await self.client.delete(
      PREFIX + f"/admin/groups/{cid}",
      headers=self.headers,
    )
    self.assertEqual(response.status, 204)
    self.assertIsNotNone(await self.db.get_conversation(cid))

  # ── (1) All-sources-removed stops monitoring, archive survives, re-enable resumes ──

  async def test_all_references_removed_stops_monitoring_keeps_archive_reenabling_resumes(
    self,
  ):
    """Contract: last-reference removal stops callbacks/history but never deletes archives."""
    response = await self.post_group()
    self.assertEqual(response.status, 201)
    cid = (await response.json())["conversation"]["id"]
    await asyncio.wait_for(self.history_started.wait(), 1)

    await self.seed_group_message(GROUP_ID, 100)
    # Confirm monitoring row is active (reference_count >= 1).
    monitoring = await self.db.list_group_monitoring()
    row = next(r for r in monitoring if str(r["id"]) == cid)
    self.assertIsNotNone(row, "group should appear in monitoring list after add")
    self.assertTrue(row["manual_reference"])
    self.assertGreater(row["reference_count"], 0)

    # Remove the only reference (manual). Archive must survive.
    response = await self.client.delete(
      PREFIX + f"/admin/groups/{cid}",
      headers=self.headers,
    )
    self.assertEqual(response.status, 204)

    conversation = await self.db.get_conversation(cid)
    self.assertIsNotNone(
      conversation, "archive must not be deleted when monitoring stops"
    )

    # reference_count must drop to 0.
    monitoring = await self.db.list_group_monitoring()
    row = next(r for r in monitoring if str(r["id"]) == cid)
    if row is not None:
      self.assertFalse(row["manual_reference"])
      self.assertEqual(row["reference_count"], 0)

    state = await self.monitoring_state(cid)
    self.assertEqual(state["runtime"]["state"], "stopped")
    self.assertEqual(state["reference_count"], 0)
    self.assertEqual(self.telegram.remove_event_handler.call_count, 3)
    self.history_started.clear()
    # Re-enable via PUT /admin/groups/{cid} — must resume with the same UUID.
    response = await self.client.put(
      PREFIX + f"/admin/groups/{cid}",
      headers=self.headers,
    )
    self.assertEqual(response.status, 204)

    monitoring = await self.db.list_group_monitoring()
    row = next(r for r in monitoring if str(r["id"]) == cid)
    self.assertIsNotNone(row, "re-enabled group must reappear in monitoring")
    self.assertTrue(row["manual_reference"])
    self.assertGreater(row["reference_count"], 0)

    await asyncio.wait_for(self.history_started.wait(), 1)
    self.assertEqual((await self.monitoring_state(cid))["runtime"]["state"], "running")
    self.assertEqual(self.telegram.add_event_handler.call_count, 6)
    response = await self.client.put(
      PREFIX + f"/admin/users/{self.admin_user['id']}/grants/{cid}",
      headers=self.headers,
    )
    self.assertEqual(response.status, 204)
    response = await self.client.get(
      PREFIX + f"/conversations/{cid}/messages/1",
      headers=self.headers,
    )
    self.assertEqual(response.status, 200)
    self.assertEqual((await response.json())["message"]["text"], "visibility marker")
    self.assertEqual(str((await self.db.get_conversation(cid))["id"]), cid)

  # ── (2) Two accounts + public references removed in different orders ──

  async def test_reference_removal_ordering_with_two_accounts_public_and_last_account_delete(
    self,
  ):
    """Contract: each reference type is independent; removing all stops monitoring.
    Deleting the last account cascades its grants; archive is still retained."""
    response = await self.post_group()
    self.assertEqual(response.status, 201)
    cid = (await response.json())["conversation"]["id"]

    alice_id, alice_headers = await self.login_user("order-alice")
    bob_id, _ = await self.login_user("order-bob")

    # Grant both accounts and make the group public.
    for uid in (alice_id, bob_id):
      r = await self.client.put(
        PREFIX + f"/admin/users/{uid}/grants/{cid}",
        headers=self.headers,
      )
      self.assertEqual(r.status, 204)
    r = await self.client.post(PREFIX + f"/admin/public/{cid}", headers=self.headers)
    self.assertEqual(r.status, 204)

    # reference_count = manual(1) + alice(1) + bob(1) + public(1) = 4
    monitoring = await self.db.list_group_monitoring()
    row = next(r for r in monitoring if str(r["id"]) == cid)
    self.assertEqual(row["reference_count"], 4)

    r = await self.client.delete(PREFIX + f"/admin/groups/{cid}", headers=self.headers)
    self.assertEqual(r.status, 204)
    self.assertEqual((await self.monitoring_state(cid))["reference_count"], 3)

    # Remove alice's grant — public and Bob remain.
    r = await self.client.delete(
      PREFIX + f"/admin/users/{alice_id}/grants/{cid}",
      headers=self.headers,
    )
    self.assertEqual(r.status, 204)
    monitoring = await self.db.list_group_monitoring()
    row = next(r for r in monitoring if str(r["id"]) == cid)
    self.assertEqual(row["reference_count"], 2)

    # Remove public grant — only Bob remains.
    r = await self.client.delete(PREFIX + f"/admin/public/{cid}", headers=self.headers)
    self.assertEqual(r.status, 204)
    monitoring = await self.db.list_group_monitoring()
    row = next(r for r in monitoring if str(r["id"]) == cid)
    self.assertEqual(row["reference_count"], 1)
    r = await self.client.get(PREFIX + "/conversations", headers=alice_headers)
    self.assertEqual((await r.json())["conversations"], [])
    r = await self.client.patch(
      PREFIX + f"/admin/users/{bob_id}",
      headers=self.headers,
      json={"is_active": False},
    )
    self.assertEqual(r.status, 200)
    self.assertEqual((await self.monitoring_state(cid))["reference_count"], 1)

    # Deleting Bob cascades the LAST reference and stops collection.
    r = await self.client.delete(
      PREFIX + f"/admin/users/{bob_id}",
      headers=self.headers,
    )
    self.assertEqual(r.status, 204)
    monitoring = await self.db.list_group_monitoring()
    row = next(r for r in monitoring if str(r["id"]) == cid)
    self.assertEqual(row["reference_count"], 0)
    self.assertEqual((await self.monitoring_state(cid))["runtime"]["state"], "stopped")
    self.assertEqual(self.telegram.remove_event_handler.call_count, 3)

    # Repeating removal remains idempotent; the archive survives.
    r = await self.client.delete(
      PREFIX + f"/admin/groups/{cid}",
      headers=self.headers,
    )
    self.assertEqual(r.status, 204)
    self.assertIsNotNone(await self.db.get_conversation(cid))

    monitoring = await self.db.list_group_monitoring()
    row = next(r for r in monitoring if str(r["id"]) == cid)
    if row is not None:
      self.assertEqual(row["reference_count"], 0)

  # ── (3) Topic-only grants keep parent collecting; removing stops it ──

  async def test_topic_grant_keeps_parent_collecting_without_broader_read_permission(
    self,
  ):
    """Contract: personal grants on topics keep parent peer indexed.
    A topic grant gives read access only to that topic, not the parent group."""
    info = await self.indexer.init_group(self.entity)
    parent_cid = str(info["conversation_uuid"])

    async with self.db.get_conn() as conn:
      topic = await self.db._ensure_conversation(
        conn,
        "topic",
        "channel",
        GROUP_ID,
        "Topic A",
        topic_id=999,
        legacy_group_id=GROUP_ID,
      )
    topic_cid = str(topic["id"])

    user_id, alice = await self.login_user("topic-only-user")

    # Grant alice only the topic, not the parent group.
    r = await self.client.put(
      PREFIX + f"/admin/users/{user_id}/grants/{topic_cid}",
      headers=self.headers,
    )
    self.assertEqual(r.status, 204)

    # The topic grant on GROUP_ID's peer should produce user_references >= 1
    # on the monitoring row for the parent group.
    monitoring = await self.db.list_group_monitoring()
    row = next(r for r in monitoring if str(r["id"]) == parent_cid)
    self.assertIsNotNone(
      row, "topic grant must appear as a reference on the parent group"
    )
    self.assertGreater(
      row["user_references"],
      0,
      "topic-only grant must increment parent's user_references",
    )
    self.assertGreater(row["reference_count"], 0)

    # Alice can see the topic conversation (via personal grant + inheritance).
    r = await self.client.get(PREFIX + "/conversations", headers=alice)
    self.assertEqual(r.status, 200)
    accessible = {c["id"] for c in (await r.json())["conversations"]}
    self.assertEqual(accessible, {topic_cid})
    self.assertNotIn(parent_cid, accessible)
    await asyncio.wait_for(self.history_started.wait(), 1)
    self.assertEqual(
      (await self.monitoring_state(parent_cid))["runtime"]["state"], "running"
    )

    # Remove alice's topic grant — parent peer's user_references drops to 0.
    r = await self.client.delete(
      PREFIX + f"/admin/users/{user_id}/grants/{topic_cid}",
      headers=self.headers,
    )
    self.assertEqual(r.status, 204)

    monitoring = await self.db.list_group_monitoring()
    row = next(r for r in monitoring if str(r["id"]) == parent_cid)
    self.assertIsNotNone(row)
    self.assertEqual(row["user_references"], 0)
    self.assertEqual(row["reference_count"], 0)
    self.assertEqual(
      (await self.monitoring_state(parent_cid))["runtime"]["state"], "stopped"
    )
    self.assertEqual(self.telegram.remove_event_handler.call_count, 3)

  # ── (4) One-time config import is idempotent; replay does not resurrect removed monitoring ──

  async def test_migration_adopts_old_archives_once_but_never_resurrects_stopped_groups(
    self,
  ):
    migration = (
      Path(__file__).resolve().parents[1] / "migrations" / "004_group_monitoring.sql"
    ).read_text()

    async def migrate():
      pool = self.db.pool
      assert pool is not None
      async with pool.acquire() as conn:
        # Fixed, trusted repository migration; its own BEGIN/COMMIT is intentional.
        # pi-lens-ignore: python-sql-injection
        await conn.execute(migration)

    info = await self.indexer.init_group(self.entity)
    cid = str(info["conversation_uuid"])
    await migrate()
    self.assertFalse(
      (await self.monitoring_state(cid))["manual"],
      "fresh schemas must skip legacy adoption",
    )
    async with self.db.get_conn() as conn:
      # Simulate upgrading a pre-004 installation: no legacy adoption marker.
      # Literal SQL; the marker name is separately bound as $1.
      # pi-lens-ignore: python-sql-injection
      await conn.execute(
        "DELETE FROM bootstrap_state WHERE name = $1",
        "group-monitoring-legacy-v1",
      )
      # Literal SQL; both cursor values and group ID are bound as $1/$2/$3.
      # pi-lens-ignore: python-sql-injection
      await conn.execute(
        "UPDATE tg_groups SET loaded_first_id = $1, loaded_last_id = $2 WHERE group_id = $3",
        10,
        20,
        GROUP_ID,
      )
    await migrate()
    self.assertTrue((await self.monitoring_state(cid))["manual"])
    self.assertEqual(await self.db.list_public_grants(), [])
    async with self.db.get_conn() as conn:
      stored = await self.db.get_group(conn, GROUP_ID)
      self.assertEqual((stored["loaded_first_id"], stored["loaded_last_id"]), (10, 20))
    response = await self.client.delete(
      PREFIX + f"/admin/groups/{cid}", headers=self.headers
    )
    self.assertEqual(response.status, 204)
    later = types.Channel(
      id=GROUP_ID + 1,
      title="Later archive",
      photo=types.ChatPhotoEmpty(),
      date=self.entity.date,
      megagroup=True,
    )
    later_info = await self.indexer.init_group(later)
    later_cid = str(later_info["conversation_uuid"])
    await migrate()
    for group_cid in (cid, later_cid):
      state = await self.monitoring_state(group_cid)
      self.assertFalse(state["manual"])
      self.assertEqual(state["reference_count"], 0)
    # A tombstone predating initial config import is authoritative too.
    self.assertTrue(await self.db.import_monitoring_config([self.entity]))
    self.assertFalse((await self.monitoring_state(cid))["manual"])
    self.indexer.config["telegram"]["index_groups"] = [MARKED_ID]
    self.telegram.get_entity.side_effect = AssertionError(
      "stale config must not trigger Telegram lookup"
    )
    await self.indexer.import_group_config()
    self.assertFalse((await self.monitoring_state(cid))["requested"])

  async def test_config_import_is_idempotent_and_does_not_resurrect_removed_monitoring(
    self,
  ):
    """Contract: import_monitoring_config fires once; replay is a no-op even after removal."""
    groups = [self.entity]
    imported = await self.db.import_monitoring_config(groups)
    self.assertTrue(imported, "first import must succeed")

    imported_again = await self.db.import_monitoring_config(groups)
    self.assertFalse(imported_again, "second import must be a no-op")
    self.assertTrue(await self.db.monitoring_config_imported())

    monitoring = await self.db.list_group_monitoring()
    self.assertEqual(len(monitoring), 1)
    cid = str(monitoring[0]["id"])

    # Enable then remove manual reference (operator removes the group).
    r = await self.client.put(PREFIX + f"/admin/groups/{cid}", headers=self.headers)
    self.assertEqual(r.status, 204)
    r = await self.client.delete(PREFIX + f"/admin/groups/{cid}", headers=self.headers)
    self.assertEqual(r.status, 204)

    monitoring = await self.db.list_group_monitoring()
    row = next(r for r in monitoring if str(r["id"]) == cid)
    if row is not None:
      self.assertFalse(row["manual_reference"])
      self.assertEqual(row["reference_count"], 0)

    # Replay must not resurrect.
    replayed = await self.db.import_monitoring_config(groups)
    self.assertFalse(replayed, "replay must not resurrect removed monitoring")

    monitoring = await self.db.list_group_monitoring()
    row = next(r for r in monitoring if str(r["id"]) == cid)
    if row is not None:
      self.assertEqual(
        row["reference_count"], 0, "replay must not re-add manual reference"
      )

    # Archive is preserved.
    self.assertIsNotNone(await self.db.get_conversation(cid))

  # ── (5) Unauthorized monitoring calls do not mutate intent ──

  async def test_unauthorized_monitoring_calls_do_not_mutate(self):
    """Contract: unauthenticated and non-admin calls must not change monitoring state."""
    response = await self.post_group()
    self.assertEqual(response.status, 201)
    cid = (await response.json())["conversation"]["id"]

    _, ordinary = await self.login_user("non-admin-monitor")

    for headers, expected_status in (({}, 401), (ordinary, 403)):
      r = await self.client.get(PREFIX + "/admin/groups", headers=headers)
      self.assertEqual(
        r.status, expected_status, f"GET /admin/groups anon={not headers}"
      )

      r = await self.client.put(
        PREFIX + f"/admin/groups/{cid}",
        headers=headers,
      )
      self.assertEqual(
        r.status, expected_status, f"PUT /admin/groups/{cid} anon={not headers}"
      )

      r = await self.client.delete(
        PREFIX + f"/admin/groups/{cid}",
        headers=headers,
      )
      self.assertEqual(
        r.status,
        expected_status,
        f"DELETE /admin/groups/{cid} anon={not headers}",
      )

    # Monitoring state must be unchanged: manual_reference still True from the add.
    monitoring = await self.db.list_group_monitoring()
    row = next(r for r in monitoring if str(r["id"]) == cid)
    self.assertIsNotNone(row)
    self.assertTrue(
      row["manual_reference"],
      "unauthorized calls must not mutate manual_reference",
    )
    self.assertGreater(row["reference_count"], 0)

  # ── (6) Web-only mode: read-only admin list works; Telegram-dependent add is 503 ──

  async def test_web_only_mode_monitoring_endpoints_behave_correctly(self):
    """Contract: web-only mode (no Telegram client) cannot accept add_group.
    GET admin/conversations is still readable. PUT/DELETE /admin/groups/{cid}
    are pure-DB mutations and must not require Telegram."""
    info = await self.indexer.init_group(self.entity)
    cid = str(info["conversation_uuid"])
    await self.db.set_manual_monitoring(cid, True)

    app = setup_app(
      self.db,
      None,
      ".",
      "nobody.jpg",
      "ghost.jpg",
      prefix=PREFIX,
      auth_service=self.auth,
      # No add_group or monitoring_changed — web-only.
    )
    async with TestClient(TestServer(app)) as web_client:
      # Read-only admin list must still work.
      r = await web_client.get(PREFIX + "/admin/conversations", headers=self.headers)
      self.assertEqual(r.status, 200)
      rows = {row["id"] for row in (await r.json())["conversations"]}
      self.assertIn(cid, rows)

      # POST /admin/groups requires Telegram — must 503.
      r = await web_client.post(
        PREFIX + "/admin/groups",
        json={"group": MARKED_ID},
        headers=self.headers,
      )
      self.assertEqual(r.status, 503)

      # PUT/DELETE /admin/groups/{cid} are pure-DB; must not 503.
      r = await web_client.put(PREFIX + f"/admin/groups/{cid}", headers=self.headers)
      self.assertEqual(r.status, 204)
      r = await web_client.delete(PREFIX + f"/admin/groups/{cid}", headers=self.headers)
      self.assertEqual(r.status, 204)

    self.assertIsNotNone(await self.db.get_conversation(cid))

  # ── (7) Cancellation of slow Telegram entity resolution does not corrupt state ──

  async def test_slow_entity_resolution_cancellation_does_not_corrupt_state(self):
    """Contract: cancelling an in-flight Telegram lookup must not corrupt the DB;
    the group must be retryable and the conversation record preserved."""
    resolve_started = asyncio.Event()
    resolve_cancelled = asyncio.Event()
    original_get_entity = self.telegram.get_entity.side_effect

    async def slow_get_entity(peer):
      resolve_started.set()
      try:
        await asyncio.Future()
      finally:
        resolve_cancelled.set()

    self.telegram.get_entity.side_effect = slow_get_entity

    # Insert the group row directly so the monitor has something to reconcile.
    info = await self.db.add_monitored_group(self.entity)
    cid = str(info["conversation_uuid"])
    await self.indexer.sync_monitoring()
    await asyncio.wait_for(resolve_started.wait(), 2)
    response = await self.client.delete(
      PREFIX + f"/admin/groups/{cid}", headers=self.headers
    )
    self.assertEqual(response.status, 204)
    self.assertTrue(
      resolve_cancelled.is_set(),
      "last-reference removal must cancel queued resolution",
    )
    self.assertEqual((await self.monitoring_state(cid))["runtime"]["state"], "stopped")
    self.telegram.get_entity.side_effect = original_get_entity
    response = await self.client.put(
      PREFIX + f"/admin/groups/{cid}", headers=self.headers
    )
    self.assertEqual(response.status, 204)
    await asyncio.wait_for(self.history_started.wait(), 2)
    self.assertEqual((await self.monitoring_state(cid))["runtime"]["state"], "running")

    # DB must be consistent after cancellation.
    all_convs = await self.db.list_all_conversations()
    self.assertGreater(len(all_convs), 0, "conversation must exist after cancellation")
    for row in await self.db.list_group_monitoring():
      self.assertGreaterEqual(row["reference_count"], 0)

  # ── (8) GET /admin/groups shows only active references; stopped group absent ──

  async def test_topic_repair_removing_last_reference_does_not_start_collection(self):
    info = await self.indexer.init_group(self.entity)
    cid = str(info["conversation_uuid"])
    async with self.db.get_conn() as conn:
      topic = await self.db._ensure_conversation(
        conn,
        "topic",
        "channel",
        GROUP_ID,
        "Legacy fake topic",
        topic_id=999,
        legacy_group_id=GROUP_ID,
      )
    self.db.repair_non_forum_groups = frozenset([GROUP_ID])
    uid, user_headers = await self.login_user("repair-last-ref")
    response = await self.client.put(
      PREFIX + f"/admin/users/{uid}/grants/{topic['id']}",
      headers=self.headers,
    )
    self.assertEqual(response.status, 204)
    async with asyncio.timeout(2):
      while (await self.monitoring_state(cid))["runtime"]["state"] != "stopped":
        # Each HTTP request yields until the worker finishes initialization.
        continue
    self.assertFalse((await self.monitoring_state(cid))["requested"])
    self.assertFalse(self.history_started.is_set())
    self.telegram.add_event_handler.assert_not_called()
    response = await self.client.get(PREFIX + "/conversations", headers=user_headers)
    self.assertEqual((await response.json())["conversations"], [])
    response = await self.client.get(
      PREFIX + f"/admin/users/{uid}/grants", headers=self.headers
    )
    self.assertEqual((await response.json())["conversations"], [])
    self.assertIsNotNone(await self.db.get_conversation(cid))

  async def test_last_reference_cancels_real_history_telegram_io_before_returning(
    self,
  ):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def download(*args, **kwargs):
      started.set()
      try:
        await asyncio.Future()
      finally:
        cancelled.set()

    self.telegram.get_messages.side_effect = download
    with patch("luoxu.group.GroupHistoryIndexer.run", new=self.real_history_run):
      response = await self.post_group()
      self.assertEqual(response.status, 201)
      cid = (await response.json())["conversation"]["id"]
      await asyncio.wait_for(started.wait(), 2)
      async with asyncio.timeout(2):
        response = await self.client.delete(
          PREFIX + f"/admin/groups/{cid}", headers=self.headers
        )
      self.assertEqual(response.status, 204)
      self.assertTrue(cancelled.is_set())
      self.assertEqual(self.telegram.remove_event_handler.call_count, 3)
      self.assertEqual(
        (await self.monitoring_state(cid))["runtime"]["state"], "stopped"
      )

  async def test_web_only_mutations_are_reconciled_by_the_running_indexer(self):
    response = await self.post_group()
    self.assertEqual(response.status, 201)
    cid = (await response.json())["conversation"]["id"]
    await asyncio.wait_for(self.history_started.wait(), 2)
    disconnected = asyncio.Event()
    removed = asyncio.Event()
    self.telegram.run_until_disconnected = AsyncMock(side_effect=disconnected.wait)
    self.telegram.remove_event_handler.side_effect = lambda *args: removed.set()
    self.indexer.config["web"] = {
      "auth": {"jwt_secret": AuthService.random_secret()},
      "public_groups": [],
    }
    core = asyncio.create_task(self.indexer.run_on_connected(self.telegram, self.db))
    app = setup_app(
      self.db,
      None,
      ".",
      "nobody.jpg",
      "ghost.jpg",
      prefix=PREFIX,
      auth_service=self.auth,
    )
    try:
      async with TestClient(TestServer(app)) as web_client:
        response = await web_client.get(
          PREFIX + "/admin/conversations", headers=self.headers
        )
        rows = (await response.json())["conversations"]
        self.assertEqual(rows[0]["monitoring"]["runtime"]["state"], "unknown")
        response = await web_client.delete(
          PREFIX + f"/admin/groups/{cid}", headers=self.headers
        )
        self.assertEqual(response.status, 204)
        await asyncio.wait_for(removed.wait(), 5)
        self.assertEqual(
          (await self.monitoring_state(cid))["runtime"]["state"], "stopped"
        )
        self.history_started.clear()
        response = await web_client.put(
          PREFIX + f"/admin/groups/{cid}", headers=self.headers
        )
        self.assertEqual(response.status, 204)
        await asyncio.wait_for(self.history_started.wait(), 5)
        self.assertEqual(
          (await self.monitoring_state(cid))["runtime"]["state"], "running"
        )
    finally:
      disconnected.set()
      await asyncio.wait_for(core, 3)

  async def test_admin_groups_lists_only_active_monitored_groups(self):
    """Contract: GET /admin/groups returns only groups with reference_count > 0."""
    response = await self.post_group()
    self.assertEqual(response.status, 201)
    cid = (await response.json())["conversation"]["id"]

    r = await self.client.get(PREFIX + "/admin/groups", headers=self.headers)
    self.assertEqual(r.status, 200)
    active_ids = {row["id"] for row in (await r.json())["groups"]}
    self.assertIn(cid, active_ids, "manually added group must appear in /admin/groups")

    # Remove the manual reference.
    r = await self.client.delete(PREFIX + f"/admin/groups/{cid}", headers=self.headers)
    self.assertEqual(r.status, 204)

    r = await self.client.get(PREFIX + "/admin/groups", headers=self.headers)
    self.assertEqual(r.status, 200)
    active_ids = {row["id"] for row in (await r.json())["groups"]}
    self.assertNotIn(
      cid,
      active_ids,
      "group with zero references must not appear in /admin/groups",
    )

    # /admin/conversations must still include it (archive preserved).
    r = await self.client.get(PREFIX + "/admin/conversations", headers=self.headers)
    self.assertEqual(r.status, 200)
    all_ids = {row["id"] for row in (await r.json())["conversations"]}
    self.assertIn(cid, all_ids, "archive must still appear in /admin/conversations")


if __name__ == "__main__":
  unittest.main()
