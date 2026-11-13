"""Synthetic Bot API command/menu tests; never contact Telegram."""

import asyncio
import os
import tempfile
import time
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientError
from aiohttp.test_utils import TestClient, TestServer

from luoxu.auth import AuthService
from luoxu.telegram_auth import MembershipUnavailable, TelegramAuth
from luoxu.telegram_commands import BotAPI, BotAPIError, TelegramCommands
from luoxu.web import setup_app


def message(uid=43, text="/default", **changes):
  return {"message": {"from": {"id": uid, "is_bot": False},
                      "chat": {"id": uid, "type": "private"},
                      "date": int(time.time()), "text": text, **changes}}


def callback(uid, data, **changes):
  return {"callback_query": {"id": "synthetic-query", "from": {"id": uid, "is_bot": False},
                             "message": {"chat": {"id": uid, "type": "private"},
                                         "date": int(time.time())}, "data": data, **changes}}


class CommandTests(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    with patch.dict(os.environ, {"LUOXU_TELEGRAM_BOT_TOKEN": "123:synthetic"}):
      self.auth = TelegramAuth({"enabled": True, "commands": {"enabled": True}})
    self.rows = [{"id": uuid.uuid4(), "name": f"visible-{i}", "legacy_group_id": 100 + i,
                  "telegram_peer_type": "channel", "telegram_peer_id": 100 + i} for i in range(10)]
    self.db = AsyncMock()
    self.db.get_telegram_user.return_value = None
    self.db.telegram_group_candidates.return_value = self.rows
    self.db.get_telegram_default_id.return_value = self.rows[1]["id"]
    self.visible = AsyncMock(return_value=tuple(row["id"] for row in self.rows))
    self.auth.visible_groups = self.visible
    self.api = MagicMock()
    self.api.call = AsyncMock(return_value=True)
    self.service = TelegramCommands(self.db, self.auth, None, self.api)
    self.service.username = "fixture_bot"

  def sent(self):
    return [call.args[1] for call in self.api.call.await_args_list if call.args[0] == "sendMessage"]

  async def test_list_pagination_selection_and_clear(self):
    await self.service.handle_update(message())
    first = self.sent()[-1]
    self.assertEqual(first["chat_id"], 43)
    keyboard = first["reply_markup"]["inline_keyboard"]
    self.assertEqual(len(keyboard), 10)  # 8 groups + next + clear
    self.assertTrue(keyboard[1][0]["text"].startswith("✓ "))
    self.assertTrue(all(len(button["callback_data"].encode()) <= 64 for row in keyboard for button in row))
    await self.service.handle_update(callback(43, "default:page:1"))
    self.assertEqual(len(self.sent()[-1]["reply_markup"]["inline_keyboard"]), 4)
    await self.service.handle_update(callback(43, f"default:set:{self.rows[1]['id']}"))
    self.db.set_telegram_default_id.assert_awaited_with(43, self.rows[1]["id"])
    self.assertIn("visible-1", self.sent()[-1]["text"])
    await self.service.handle_update(message(text="/default clear"))
    self.db.set_telegram_default_id.assert_awaited_with(43, None)
    for text in ("/default@fixture_bot 100", "/default -1000000000100"):
      await self.service.handle_update(message(text=text))
      self.db.set_telegram_default_id.assert_awaited_with(43, self.rows[0]["id"])

  async def test_group_chats_forwarded_stale_and_other_commands_are_ignored(self):
    bot_message = message()
    bot_message["message"]["from"] = {"id": 43, "is_bot": True}
    invalid = [message(chat={"id": -100, "type": "supergroup"}),
               message(chat={"id": 42, "type": "private"}),
               bot_message,
               message(date=int(time.time()) - 301), message(forward_origin={"type": "user"}),
               message(text="/default@another_bot"), message(text="/start"),
               {"edited_message": message()["message"]}, {}, []]
    for update in invalid:
      await self.service.handle_update(update)
    self.visible.assert_not_awaited()
    self.db.set_telegram_default_id.assert_not_awaited()
    self.assertEqual(self.sent(), [])

  async def test_unknown_members_disabled_accounts_and_forged_selection_stay_silent(self):
    for result in ((), MembershipUnavailable()):
      self.visible.return_value = result
      self.visible.side_effect = result if isinstance(result, Exception) else None
      await self.service.handle_update(message())
    self.visible.side_effect = None
    self.visible.return_value = tuple(row["id"] for row in self.rows)
    self.db.get_telegram_user.return_value = {"is_active": False}
    await self.service.handle_update(message())
    self.db.get_telegram_user.return_value = None
    for data in (f"default:set:{uuid.uuid4()}", "default:set:not-a-uuid", "default:unknown"):
      await self.service.handle_update(callback(43, data))
    await self.service.handle_update(message(text="/default 999999"))
    await self.service.handle_update(callback(43, f"default:set:{self.rows[0]['id']}",
                                              message={"chat": {"id": 42, "type": "private"}, "date": int(time.time())}))
    self.assertEqual(self.sent(), [])
    self.db.set_telegram_default_id.assert_not_awaited()
    # Callback acknowledgements are empty and reveal no group or rejection text.
    for call in self.api.call.await_args_list:
      if call.args[0] == "answerCallbackQuery":
        self.assertEqual(call.args[1], {"callback_query_id": "synthetic-query"})

  async def test_publication_is_rechecked_after_membership_and_old_buttons_expire(self):
    self.db.telegram_group_candidates.return_value = self.rows[1:]
    await self.service.handle_update(callback(43, f"default:set:{self.rows[0]['id']}"))
    await self.service.handle_update(callback(43, f"default:set:{self.rows[1]['id']}",
                                              message={"chat": {"id": 43, "type": "private"}, "date": int(time.time())-301}))
    self.db.set_telegram_default_id.assert_not_awaited()
    self.assertEqual(self.sent(), [])

  async def test_menu_registration_preserves_other_commands_and_never_replaces_webhook(self):
    async def call(method, payload):
      return {"getWebhookInfo": {"url": ""}, "getMe": {"id": 123, "username": "fixture_bot"},
              "getMyCommands": [{"command": "help", "description": "Existing help"}],
              "setMyCommands": True}[method]
    self.api.call.side_effect = call
    await self.service.register_menu()
    method, payload = self.api.call.await_args.args
    self.assertEqual(method, "setMyCommands")
    self.assertEqual(payload["scope"], {"type": "all_private_chats"})
    self.assertEqual([c["command"] for c in payload["commands"]], ["help", "default"])
    self.api.call.reset_mock()
    self.api.call.side_effect = None
    self.api.call.return_value = {"url": "https://other.invalid/webhook"}
    with self.assertRaises(BotAPIError) as error:
      await self.service.register_menu()
    self.assertEqual(error.exception.code, 409)
    self.api.call.assert_awaited_once_with("getWebhookInfo", {})

  async def test_transport_errors_are_redacted_and_redirects_not_followed(self):
    session = MagicMock()
    session.post.side_effect = ClientError("https://api.telegram.org/bot123:synthetic/private")
    api = BotAPI("123:synthetic", session)
    with self.assertRaises(BotAPIError) as error:
      await api.call("getUpdates", {})
    self.assertNotIn("synthetic", str(error.exception))
    session.post.side_effect = None
    response = AsyncMock()
    response.status = 429
    response.json.return_value = {"ok": False, "parameters": {"retry_after": 999}}
    session.post.return_value.__aenter__.return_value = response
    with self.assertRaises(BotAPIError) as error:
      await api.call("getUpdates", {})
    self.assertEqual(error.exception.retry_after, 60)
    self.assertFalse(session.post.call_args.kwargs["allow_redirects"])

  async def test_app_lifecycle_starts_only_when_enabled_and_cleans_worker(self):
    auth = AuthService({"jwt_secret": "synthetic-secret-" * 3})
    auth.telegram = self.auth
    with tempfile.TemporaryDirectory() as directory, patch.object(TelegramCommands, "run", new=AsyncMock()) as run:
      app = setup_app(self.db, None, directory, "avatar", "ghost", auth_service=auth)
      async with TestClient(TestServer(app)):
        await asyncio.sleep(0)
        run.assert_awaited_once()
      self.assertEqual(app["telegram_commands"].status, "stopped")
      auth.telegram.commands_enabled = False
      app = setup_app(self.db, None, directory, "avatar", "ghost", auth_service=auth)
      async with TestClient(TestServer(app)):
        self.assertNotIn("telegram_commands", app)
      run.assert_awaited_once()

  async def test_configuration_requires_authentication_and_boolean_flag(self):
    for config in ({"commands": {"enabled": True}}, {"commands": {"enabled": "true"}},
                   {"commands": []}):
      with self.assertRaises(ValueError):
        TelegramAuth(config)
