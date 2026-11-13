"""Real HTTP/DB Mini App privacy boundaries; Telegram membership is synthetic."""

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestServer
from test_admin_groups import GROUP_ID, PREFIX, AdminGroupFixture
from test_telegram_login import TOKEN, signed

from luoxu.auth import Principal
from luoxu.telegram_auth import MembershipUnavailable, TelegramAuth


class MiniAppScopeTests(AdminGroupFixture):
  async def asyncSetUp(self):
    await super().asyncSetUp()
    with patch.dict(os.environ, {"LUOXU_TELEGRAM_BOT_TOKEN": TOKEN}):
      self.auth.telegram = TelegramAuth({
        "enabled": True, "owner_telegram_id": 42, "membership_cache_ttl": 0,
        "accounts": [{"telegram_id": 42, "user_id": str(self.admin_user["id"])}],
      })
    self.groups = [await self.seed_group_message(GROUP_ID + i, 800000 + i) for i in range(3)]
    self.a, self.b, self.secret = [uuid.UUID(g["cid"]) for g in self.groups]
    await self.db.grant_public(self.a)
    await self.db.grant_public(self.b)
    self.members = {42: {GROUP_ID, GROUP_ID + 2}, 43: {GROUP_ID},
                    44: {GROUP_ID + 1}, 45: {GROUP_ID + 2}}

    async def member(session, client, chat_id, telegram_id):
      return -chat_id - 1000000000000 in self.members.get(telegram_id, set())

    self.check = AsyncMock(side_effect=member)
    self.auth.telegram._check_member = self.check

  async def exchange(self, telegram_id, **extra):
    raw = signed(user=json.dumps({"id": telegram_id, "username": "admin", "is_admin": True}),
                 query_id=uuid.uuid4().hex)
    response = await self.client.post(PREFIX + "/auth/telegram", json={"init_data": raw, **extra})
    return response, raw

  async def session(self, telegram_id):
    response, raw = await self.exchange(telegram_id)
    self.assertEqual(response.status, 200, await response.text())
    tokens = await response.json()
    return {"Authorization": "Bearer " + tokens["access_token"]}, tokens, raw

  async def assert_groups(self, headers, expected):
    response = await self.client.get(PREFIX + "/groups", headers=headers)
    self.assertEqual(response.status, 200, await response.text())
    self.assertEqual({uuid.UUID(g["conversation_id"]) for g in (await response.json())["groups"]}, expected)

  async def test_owner_guests_web_and_content_are_separate(self):
    owner, _, _ = await self.session(42)
    alice, tokens, _ = await self.session(43)
    bob, _, _ = await self.session(44)
    await self.assert_groups(owner, {self.a, self.secret})
    await self.assert_groups(alice, {self.a})
    await self.assert_groups(bob, {self.b})
    await self.assert_groups({}, {self.a, self.b})
    await self.assert_groups(self.headers, {self.a, self.b, self.secret})
    await self.assert_content_access(owner, self.groups, {str(self.a), str(self.secret)})
    await self.assert_content_access(alice, self.groups, {str(self.a)})
    guest = await self.db.get_telegram_user(43)
    self.assertFalse(guest["is_admin"])
    self.assertEqual(await self.db.list_user_grants(guest["id"]), [])
    self.assertFalse(self.auth.verify_password(guest["password_hash"], "!telegram-only"))
    self.assertEqual(self.auth.decode(tokens["access_token"])["telegram_scope_version"], 1)
    for headers in (owner, alice):
      response = await self.client.get(PREFIX + "/admin/conversations", headers=headers)
      self.assertEqual(response.status, 403)
      response = await self.client.get(PREFIX + "/auth/me", headers=headers)
      profile = (await response.json())["user"]
      self.assertFalse(profile["is_admin"])
      self.assertEqual(profile["auth_source"], "telegram")
    # Neither ordinary grants nor even a local admin promotion widen TG sessions.
    await self.db.grant_conversation(guest["id"], self.secret)
    await self.db.update_user(guest["id"], is_admin=True)
    await self.assert_groups(alice, {self.a})
    current = await self.db.get_user(guest["id"])
    web_headers = {"Authorization": "Bearer " + self.auth.access_token(current)}
    await self.assert_groups(web_headers, {self.a, self.b, self.secret})

  async def test_nonmembers_private_only_members_and_failures_create_nothing(self):
    for telegram_id in (45, 46):
      response, _ = await self.exchange(telegram_id)
      self.assertEqual(response.status, 401)
      self.assertIsNone(await self.db.get_telegram_user(telegram_id))
    self.check.side_effect = MembershipUnavailable
    response, _ = await self.exchange(47)
    self.assertEqual(response.status, 503)
    self.assertIsNone(await self.db.get_telegram_user(47))
    async with self.db.get_conn() as conn:
      self.assertEqual(await conn.fetchval("SELECT count(*) FROM auth_telegram_accounts"), 0)
      self.assertEqual(await conn.fetchval("SELECT count(*) FROM auth_refresh_tokens"), 0)

  async def test_partial_verification_and_unset_owner_never_widen_scope(self):
    async def partial(session, client, chat_id, telegram_id):
      if chat_id == -1000000000000 - GROUP_ID:
        return True
      raise MembershipUnavailable
    self.check.side_effect = partial
    headers, _, _ = await self.session(42)
    await self.assert_groups(headers, {self.a})
    self.check.side_effect = None
    self.check.return_value = True
    self.auth.telegram.owner_telegram_id = None
    await self.assert_groups(headers, {self.a, self.b})
    response = await self.client.get(PREFIX + f"/conversations/{self.secret}/messages/1", headers=headers)
    self.assertEqual(response.status, 404)

  async def test_startup_requires_identity_migration_when_enabled(self):
    async with self.db.get_conn() as conn:
      await conn.execute("DROP TABLE auth_telegram_accounts")
    with self.assertRaisesRegex(RuntimeError, "007 and 008"):
      await self.db.bootstrap({}, None, self.auth)
    self.auth.telegram.enabled = False
    await self.db.bootstrap({}, None, self.auth)

  async def test_scope_rechecks_publication_membership_monitoring_and_refresh(self):
    self.members[43] = {GROUP_ID, GROUP_ID + 1}
    headers, tokens, _ = await self.session(43)
    await self.assert_groups(headers, {self.a, self.b})
    await self.db.revoke_public(self.b)
    await self.assert_groups(headers, {self.a})
    response = await self.client.get(PREFIX + f"/conversations/{self.b}/messages/1", headers=headers)
    self.assertEqual(response.status, 404)
    self.members[43] = set()
    response = await self.client.get(PREFIX + "/groups", headers=headers)
    self.assertEqual(response.status, 401)
    response = await self.client.post(PREFIX + "/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    self.assertEqual(response.status, 401)
    owner, _, _ = await self.session(42)
    await self.db.set_manual_monitoring(self.secret, False)
    await self.assert_groups(owner, {self.a})
    # Previously computed scope is revalidated against publication in SQL too.
    guest = await self.db.get_telegram_user(43)
    stale = Principal(str(guest["id"]), guest["username"], True, False,
                      telegram_id=43, telegram_group_ids=(self.b,), telegram_owner=False)
    self.assertFalse(await self.db.can_access(self.b, stale))

  async def test_history_and_deleted_snapshots_use_same_member_scope(self):
    self.db.history_enabled = True
    cast(TestServer, self.client.server).app["history_enabled"] = True
    for peer in (GROUP_ID, GROUP_ID + 1):
      await self.db.delete_messages(peer, [1], peer_type="channel")
    headers, _, _ = await self.session(43)
    for cid, peer, expected in ((self.a, GROUP_ID, 200), (self.b, GROUP_ID + 1, 404)):
      response = await self.client.get(
        PREFIX + f"/conversations/{cid}/messages/1/history?include_history=true", headers=headers,
      )
      self.assertEqual(response.status, expected)
      if expected == 200:
        self.assertEqual(len((await response.json())["history"]), 1)
      response = await self.client.get(
        PREFIX + f"/search?g={peer}&q=visibility&include_deleted=true", headers=headers,
      )
      self.assertEqual(response.status, expected)
      if expected == 200:
        self.assertEqual((await response.json())["messages"][0]["content_source"], "delete_snapshot")

  async def test_topics_inherit_only_scoped_parent_and_private_chats_never_enter(self):
    async with self.db.get_conn() as conn:
      topic = await self.db._ensure_conversation(conn, "topic", "channel", GROUP_ID,
                                                "public topic", topic_id=88, legacy_group_id=GROUP_ID)
      hidden = await self.db._ensure_conversation(conn, "topic", "channel", GROUP_ID + 2,
                                                 "private topic", topic_id=99, legacy_group_id=GROUP_ID + 2)
      private = await self.db._ensure_conversation(conn, "private_chat", "user", 654321, "private chat")
    for telegram_id, expected in ((42, {self.a, self.secret, topic["id"], hidden["id"]}),
                                  (43, {self.a, topic["id"]})):
      headers, _, _ = await self.session(telegram_id)
      response = await self.client.get(PREFIX + "/conversations", headers=headers)
      ids = {uuid.UUID(row["id"]) for row in (await response.json())["conversations"]}
      self.assertEqual(ids, expected)
      self.assertNotIn(private["id"], ids)
    await self.db.grant_public(hidden["id"])
    response, _ = await self.exchange(45)
    self.assertEqual(response.status, 401)  # public topic != public parent group

  async def test_resume_binds_current_signed_identity_not_cached_web_or_other_user(self):
    owner_headers, owner_tokens, owner_raw = await self.session(42)
    alice_headers, alice_tokens, alice_raw = await self.session(43)
    # A valid old launch can resume only with its matching live refresh token.
    expired_alice = signed(user='{"id":43}', auth_date=str(int(time.time()) - 1000))
    response = await self.client.post(PREFIX + "/auth/telegram", json={
      "init_data": expired_alice, "refresh_token": alice_tokens["refresh_token"],
    })
    self.assertEqual(response.status, 200)
    resumed = await response.json()
    self.assertEqual(self.auth.decode(resumed["access_token"])["telegram_id"], 43)
    await self.assert_groups({"Authorization": "Bearer " + resumed["access_token"]}, {self.a})
    for wrong_refresh in (owner_tokens["refresh_token"], self.auth.refresh_token(self.admin_user)[0],
                          alice_tokens["refresh_token"]):
      response = await self.client.post(PREFIX + "/auth/telegram", json={
        "init_data": expired_alice, "refresh_token": wrong_refresh,
      })
      self.assertEqual(response.status, 401)
    # A new launch under another TG account cannot inherit the previous owner's session.
    response, _ = await self.exchange(44, refresh_token=owner_tokens["refresh_token"])
    self.assertEqual(response.status, 200)
    other = await response.json()
    self.assertEqual(self.auth.decode(other["access_token"])["telegram_id"], 44)
    await self.assert_groups(owner_headers, {self.a, self.secret})
    await self.assert_groups(alice_headers, {self.a})

  async def test_legacy_tokens_disabled_accounts_and_single_use_identity_creation(self):
    headers, tokens, raw = await self.session(43)
    guest = await self.db.get_telegram_user(43)
    import jwt
    claims = self.auth.decode(tokens["access_token"])
    claims.pop("telegram_scope_version")
    old = jwt.encode(claims, self.auth.secret, algorithm="HS256")
    response = await self.client.get(PREFIX + "/groups", headers={"Authorization": "Bearer " + old})
    self.assertEqual(response.status, 401)
    await self.db.update_user(guest["id"], is_active=False)
    response, _ = await self.exchange(43)
    self.assertEqual(response.status, 401)
    self.assertEqual((await self.client.get(PREFIX + "/groups", headers=headers)).status, 401)
    raw = signed(user='{"id":44}', query_id=uuid.uuid4().hex)
    responses = await asyncio.gather(*[
      self.client.post(PREFIX + "/auth/telegram", json={"init_data": raw}) for _ in range(5)
    ])
    self.assertEqual(sorted(r.status for r in responses), [200, 401, 401, 401, 401])
    users = await asyncio.gather(*[self.db.ensure_telegram_user(44) for _ in range(5)])
    self.assertEqual(len({row["id"] for row in users}), 1)
    async with self.db.get_conn() as conn:
      migration = (Path(__file__).resolve().parents[1] / "migrations/008_telegram_accounts.sql").read_text()
      # Fixed repository migration, no user-controlled SQL.
      # pi-lens-ignore: python-sql-injection
      await conn.execute(migration)
      self.assertEqual(await conn.fetchval("SELECT count(*) FROM auth_telegram_accounts"), 2)
