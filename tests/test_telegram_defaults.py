"""Default preferences and polling checkpoints in isolated PostgreSQL schemas."""

import asyncio
import json
import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
from test_admin_groups import DATABASE_URL, GROUP_ID, PREFIX, AdminGroupFixture
from test_telegram_commands import callback, message
from test_telegram_login import TOKEN, signed

from luoxu.db import PostgreStore
from luoxu.telegram_auth import TelegramAuth
from luoxu.telegram_commands import BotAPIError, TelegramCommands


class TelegramDefaultTests(AdminGroupFixture):
  async def asyncSetUp(self):
    await super().asyncSetUp()
    with patch.dict(os.environ, {"LUOXU_TELEGRAM_BOT_TOKEN": TOKEN}):
      self.auth.telegram = TelegramAuth({
        "enabled": True, "owner_telegram_id": 42, "membership_cache_ttl": 0,
        "commands": {"enabled": True},
        "accounts": [{"telegram_id": 42, "user_id": str(self.admin_user["id"])}],
      })
    groups = [await self.seed_group_message(GROUP_ID + i, 800000 + i) for i in range(3)]
    self.a, self.b, self.secret = [uuid.UUID(g["cid"]) for g in groups]
    await self.db.grant_public(self.a)
    await self.db.grant_public(self.b)
    self.members = {42: {GROUP_ID, GROUP_ID + 2}, 43: {GROUP_ID}, 44: {GROUP_ID + 1}}

    async def check(session, client, chat_id, telegram_id):
      return -chat_id - 1000000000000 in self.members.get(telegram_id, set())

    self.auth.telegram._check_member = AsyncMock(side_effect=check)
    self.api = MagicMock()
    self.api.call = AsyncMock(return_value=True)
    self.commands = TelegramCommands(self.db, self.auth.telegram, self.telegram, self.api)
    self.commands.username = "fixture_bot"

  async def login(self, telegram_id):
    raw = signed(user=json.dumps({"id": telegram_id}), query_id=uuid.uuid4().hex)
    response = await self.client.post(PREFIX + "/auth/telegram", json={"init_data": raw})
    self.assertEqual(response.status, 200, await response.text())
    return {"Authorization": "Bearer " + (await response.json())["access_token"]}

  async def profile(self, headers):
    response = await self.client.get(PREFIX + "/auth/me", headers=headers)
    self.assertEqual(response.status, 200, await response.text())
    return (await response.json())["user"]

  def sends(self):
    return [call.args[1] for call in self.api.call.await_args_list if call.args[0] == "sendMessage"]

  async def test_private_command_lists_match_scope_and_preferences_are_per_telegram_user(self):
    for uid, expected in ((42, {self.a, self.secret}), (43, {self.a}), (44, {self.b})):
      await self.commands.handle_update(message(uid))
      buttons = self.sends()[-1]["reply_markup"]["inline_keyboard"]
      ids = {uuid.UUID(button["callback_data"].split(":", 2)[2]) for row in buttons for button in row
             if button["callback_data"].startswith("default:set:")}
      self.assertEqual(ids, expected)
    await self.commands.handle_update(callback(42, f"default:set:{self.secret}"))
    await self.commands.handle_update(callback(43, f"default:set:{self.a}"))
    # Commands save preferences only, never create accounts, grants or public flags.
    async with self.db.get_conn() as conn:
      self.assertEqual(await conn.fetchval("SELECT count(*) FROM auth_telegram_accounts"), 0)
      self.assertEqual(await conn.fetchval("SELECT count(*) FROM conversation_access"), 0)
      self.assertEqual(await conn.fetchval("SELECT count(*) FROM public_conversation_access"), 2)
    owner, alice, bob = await self.login(42), await self.login(43), await self.login(44)
    self.assertEqual((await self.profile(owner))["default_group_id"], str(GROUP_ID + 2))
    self.assertEqual((await self.profile(alice))["default_group_id"], str(GROUP_ID))
    self.assertIsNone((await self.profile(bob))["default_group_id"])
    self.assertNotIn("default_group_id", await self.profile(self.headers))
    await self.commands.handle_update(message(43, "/default clear"))
    self.assertIsNone((await self.profile(alice))["default_group_id"])
    self.assertEqual((await self.profile(owner))["default_group_id"], str(GROUP_ID + 2))

  async def test_stale_or_forged_default_cannot_widen_permissions(self):
    self.members[43].add(GROUP_ID + 1)
    alice = await self.login(43)
    await self.commands.handle_update(callback(43, f"default:set:{self.a}"))
    await self.commands.handle_update(callback(43, f"default:set:{self.secret}"))
    self.assertEqual(await self.db.get_telegram_default_id(43), self.a)
    await self.db.revoke_public(self.a)
    self.assertIsNone((await self.profile(alice))["default_group_id"])
    self.api.call.reset_mock()
    await self.commands.handle_update(callback(43, f"default:set:{self.a}"))
    self.assertEqual(self.sends(), [])
    owner = await self.login(42)
    await self.commands.handle_update(callback(42, f"default:set:{self.secret}"))
    await self.db.set_manual_monitoring(self.secret, False)
    self.assertIsNone((await self.profile(owner))["default_group_id"])
    async with self.db.get_conn() as conn:
      private = await self.db._ensure_conversation(conn, "private_chat", "user", 654321, "private")
    with self.assertRaises(ValueError):
      await self.db.set_telegram_default_id(42, private["id"])
    # Persisted UUID exists but is never returned once its group is outside scope.
    self.assertEqual(await self.db.get_telegram_default_id(42), self.secret)

  async def test_nonmembers_disabled_users_and_group_messages_are_silent(self):
    await self.commands.handle_update(message(45))
    await self.commands.handle_update(message(42, chat={"id": -100123, "type": "supergroup"}))
    user = await self.db.ensure_telegram_user(43)
    await self.db.update_user(user["id"], is_active=False)
    await self.commands.handle_update(message(43))
    self.assertEqual(self.sends(), [])
    async with self.db.get_conn() as conn:
      self.assertEqual(await conn.fetchval("SELECT count(*) FROM telegram_default_groups"), 0)

  async def test_migration_repeatability_and_startup_gate(self):
    migration = (Path(__file__).resolve().parents[1] / "migrations/009_telegram_default_groups.sql").read_text()
    async with self.db.get_conn() as conn:
      # Fixed migration file, never request-controlled SQL.
      # pi-lens-ignore: python-sql-injection
      await conn.execute(migration)
      # pi-lens-ignore: python-sql-injection
      await conn.execute(migration)
      await conn.execute("DROP TABLE telegram_bot_offsets")
    with self.assertRaisesRegex(RuntimeError, "009"):
      await self.db.bootstrap({}, None, self.auth)
    self.auth.telegram.commands_enabled = False
    await self.db.bootstrap({}, None, self.auth)

  async def polling_store(self):
    store = PostgreStore({"url": DATABASE_URL})
    store.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4,
                                          server_settings={"search_path": f"{self.schema},public"})
    self.addAsyncCleanup(store.close)
    return store

  def polling_api(self, reached, *, failure=None, webhook=False):
    api = MagicMock()
    api.sent = []
    api.offsets = []

    async def call(method, payload):
      if method == "getWebhookInfo":
        return {"url": "https://other.invalid" if webhook else ""}
      if method == "getMe":
        return {"id": int(TOKEN.split(":")[0]), "username": "fixture_bot"}
      if method == "getMyCommands":
        return []
      if method == "setMyCommands":
        return True
      if method == "getUpdates":
        api.offsets.append(payload["offset"])
        if payload["offset"] <= 7:
          return [{"update_id": 7, **message(43, f"/default {GROUP_ID}")}]
        reached.set()
        await asyncio.Event().wait()
      if method == "sendMessage":
        api.sent.append(payload)
        if failure:
          raise failure
        return True
      raise AssertionError(method)

    api.call = AsyncMock(side_effect=call)
    return api

  async def test_polling_lease_checkpoint_restart_and_cleanup(self):
    store = await self.polling_store()
    reached = asyncio.Event()
    api = self.polling_api(reached)
    worker = TelegramCommands(store, self.auth.telegram, None, api)
    await worker.start(None)
    self.addAsyncCleanup(worker.close, None)
    await asyncio.wait_for(reached.wait(), 3)
    self.assertEqual(await self.db.get_telegram_default_id(43), self.a)
    other_api = self.polling_api(asyncio.Event())
    other = TelegramCommands(store, self.auth.telegram, None, other_api)
    await other.start(None)
    self.addAsyncCleanup(other.close, None)
    for _ in range(50):
      if other.status == "standby":
        break
      await asyncio.sleep(0.02)
    self.assertEqual(other.status, "standby")
    other_api.call.assert_not_awaited()
    await other.close(None)
    await worker.close(None)
    resumed = asyncio.Event()
    resumed_api = self.polling_api(resumed)
    restarted = TelegramCommands(store, self.auth.telegram, None, resumed_api)
    await restarted.start(None)
    self.addAsyncCleanup(restarted.close, None)
    await asyncio.wait_for(resumed.wait(), 3)
    self.assertEqual(resumed_api.offsets, [8])
    self.assertEqual(resumed_api.sent, [])
    await restarted.close(None)

  async def test_blocked_recipient_does_not_poison_polling_queue(self):
    store = await self.polling_store()
    reached = asyncio.Event()
    api = self.polling_api(reached, failure=BotAPIError(403))
    worker = TelegramCommands(store, self.auth.telegram, None, api)
    await worker.start(None)
    self.addAsyncCleanup(worker.close, None)
    await asyncio.wait_for(reached.wait(), 3)
    self.assertEqual(api.offsets, [0, 8])
    await worker.close(None)

  async def test_transient_send_failure_does_not_ack_update_and_webhook_is_not_deleted(self):
    store = await self.polling_store()
    worker = TelegramCommands(store, self.auth.telegram, None,
                              self.polling_api(asyncio.Event(), failure=BotAPIError(500)))

    async def stop_retry(seconds):
      worker.stopping.set()

    worker.pause = stop_retry
    await asyncio.wait_for(worker.run(), 3)
    async with self.db.get_conn() as conn:
      self.assertEqual(await conn.fetchval("SELECT count(*) FROM telegram_bot_offsets"), 0)
    self.assertEqual(await self.db.get_telegram_default_id(43), self.a)
    api = self.polling_api(asyncio.Event(), webhook=True)
    blocked = TelegramCommands(store, self.auth.telegram, None, api)
    await asyncio.wait_for(blocked.run(), 3)
    self.assertEqual(blocked.status, "blocked")
    api.call.assert_awaited_once_with("getWebhookInfo", {})
