"""MTProto membership and provider selection; synthetic clients only."""

import asyncio
import datetime
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer
from telethon.errors import (
  ChatAdminRequiredError,
  FloodWaitError,
  UserNotParticipantError,
)
from telethon.tl import functions, types

from luoxu.auth import AuthService
from luoxu.telegram_auth import MembershipUnavailable, TelegramAuth
from luoxu.web import setup_app


class UserbotMembershipTests(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    with patch.dict(os.environ, {"LUOXU_TELEGRAM_BOT_TOKEN": "123:synthetic"}):
      self.auth = TelegramAuth({"enabled": True})
    self.client = AsyncMock()
    self.client.is_connected = Mock(return_value=True)
    self.client.get_input_entity.side_effect = lambda peer: (
      types.InputPeerChannel(peer.channel_id, 1) if isinstance(peer, types.PeerChannel)
      else types.InputPeerUser(peer.user_id, 2)
    )

  async def test_channel_members_and_restrictions(self):
    rights = types.ChatAdminRights()
    for member, allowed in [
      (types.ChannelParticipant(42, None), True),
      (types.ChannelParticipantSelf(42, 1, None), True),
      (types.ChannelParticipantCreator(42, rights), True),
      (types.ChannelParticipantAdmin(42, 1, None, rights), True),
      (types.ChannelParticipantLeft(types.PeerUser(42)), False),
      (types.ChannelParticipantBanned(types.PeerUser(42), 1, None,
                                     types.ChatBannedRights(None, send_messages=True)), True),
      (types.ChannelParticipantBanned(types.PeerUser(42), 1, None,
                                     types.ChatBannedRights(None), left=True), False),
      (types.ChannelParticipantBanned(types.PeerUser(42), 1, None,
                                     types.ChatBannedRights(None, view_messages=True)), False),
    ]:
      with self.subTest(member=type(member).__name__, allowed=allowed):
        self.client.return_value = types.channels.ChannelParticipant(member, [], [])
        result = await self.auth._lookup_userbot_member(self.client, -1000000000123, 42)
        self.assertEqual(result, allowed)
        request = self.client.call_args.args[0]
        self.assertIsInstance(request, functions.channels.GetParticipantRequest)
        self.assertEqual(request.channel.channel_id, 123)
        self.assertEqual(request.participant.user_id, 42)
        self.assertEqual(self.client.call_args.kwargs, {"flood_sleep_threshold": 0})

  async def test_self_resolution_still_checks_membership(self):
    self.client.get_input_entity.side_effect = [types.InputPeerChannel(123, 1), types.InputPeerSelf()]
    self.client.return_value = types.channels.ChannelParticipant(types.ChannelParticipantSelf(42, 1, None), [], [])
    self.assertTrue(await self.auth._lookup_userbot_member(self.client, -1000000000123, 42))
    self.assertIsInstance(self.client.call_args.args[0].participant, types.InputPeerSelf)
    self.client.assert_awaited_once()

  async def test_explicit_nonmember_vs_unavailable(self):
    self.client.side_effect = UserNotParticipantError(None)
    self.assertFalse(await self.auth._lookup_userbot_member(self.client, -1000000000123, 42))
    for error in (ChatAdminRequiredError(None), FloodWaitError(None, capture=60), OSError()):
      self.client.side_effect = error
      with self.subTest(error=type(error).__name__), self.assertRaises(MembershipUnavailable):
        await self.auth._lookup_userbot_member(self.client, -1000000000123, 42)
    self.client.get_input_entity.side_effect = ValueError("unknown access hash")
    with self.assertRaises(MembershipUnavailable):
      await self.auth._lookup_userbot_member(self.client, -1000000000123, 42)

  async def test_missing_disconnected_and_wrong_identity_fail_closed(self):
    with self.assertRaises(MembershipUnavailable):
      await self.auth._lookup_userbot_member(None, -1000000000123, 42)
    self.client.is_connected.return_value = False
    with self.assertRaises(MembershipUnavailable):
      await self.auth._lookup_userbot_member(self.client, -1000000000123, 42)
    self.client.is_connected.return_value = True
    for member in (types.ChannelParticipant(99, None),
                   types.ChannelParticipantLeft(types.PeerUser(99)), object()):
      self.client.return_value = SimpleNamespace(participant=member)
      with self.assertRaises(MembershipUnavailable):
        await self.auth._lookup_userbot_member(self.client, -1000000000123, 42)

  async def test_basic_group_complete_and_forbidden_lists(self):
    for participants, allowed in [
      (types.ChatParticipants(123, [types.ChatParticipant(42, 1, None)], 1), True),
      (types.ChatParticipants(123, [types.ChatParticipant(99, 1, None)], 1), False),
      (types.ChatParticipantsForbidden(123, types.ChatParticipant(42, 1, None)), True),
      (types.ChatParticipantsForbidden(123), None),
      (types.ChatParticipants(999, [types.ChatParticipant(42, 1, None)], 1), None),
    ]:
      self.client.return_value = types.messages.ChatFull(
        types.ChatFull(123, "", participants, types.PeerNotifySettings()), [], [],
      )
      if allowed is None:
        with self.assertRaises(MembershipUnavailable):
          await self.auth._lookup_userbot_member(self.client, -123, 42)
      else:
        self.assertEqual(await self.auth._lookup_userbot_member(self.client, -123, 42), allowed)
      self.assertIsInstance(self.client.call_args.args[0], functions.messages.GetFullChatRequest)
      self.assertEqual(self.client.call_args.args[0].chat_id, 123)

  async def test_auto_only_falls_back_when_indeterminate(self):
    userbot = AsyncMock()
    bot = AsyncMock(return_value=True)
    with patch.object(self.auth, "_lookup_userbot_member", userbot), patch.object(self.auth, "_lookup_member", bot):
      for allowed in (True, False):
        userbot.return_value = allowed
        self.assertEqual(await self.auth._check_member(None, self.client, -123, 42), allowed)
      bot.assert_not_awaited()
      for error in (MembershipUnavailable(), TimeoutError()):
        userbot.side_effect = error
        self.assertTrue(await self.auth._check_member(None, self.client, -123, 42))
      self.assertEqual(bot.await_count, 2)
      bot.side_effect = MembershipUnavailable
      with self.assertRaises(MembershipUnavailable):
        await self.auth._check_member(None, self.client, -123, 42)

  async def test_fixed_modes_and_cancellation(self):
    userbot = AsyncMock(side_effect=MembershipUnavailable)
    bot = AsyncMock(return_value=True)
    with patch.object(self.auth, "_lookup_userbot_member", userbot), patch.object(self.auth, "_lookup_member", bot):
      self.auth.membership_source = "userbot"
      with self.assertRaises(MembershipUnavailable):
        await self.auth._check_member(None, self.client, -123, 42)
      bot.assert_not_awaited()
      userbot.reset_mock()
      self.auth.membership_source = "bot"
      self.assertTrue(await self.auth._check_member(None, self.client, -123, 42))
      userbot.assert_not_awaited()
      self.auth.membership_source = "auto"
      bot.reset_mock()
      userbot.side_effect = asyncio.CancelledError
      with self.assertRaises(asyncio.CancelledError):
        await self.auth._check_member(None, self.client, -123, 42)
      bot.assert_not_awaited()

  async def test_web_only_fallback_and_cache_source_isolation(self):
    db = AsyncMock()
    db.list_group_monitoring.return_value = [{"kind": "group", "telegram_peer_type": "channel",
                                             "telegram_peer_id": 123, "reference_count": 1}]
    bot = AsyncMock(return_value=True)
    with patch.object(self.auth, "_lookup_member", bot):
      self.assertTrue(await self.auth.is_indexed_group_member(db, 42))
      bot.assert_awaited_once()
      self.auth.membership_source = "userbot"
      with self.assertRaises(MembershipUnavailable):
        await self.auth.is_indexed_group_member(db, 42)
      bot.assert_awaited_once()

  async def test_app_wires_existing_client_without_new_session(self):
    directory = tempfile.TemporaryDirectory()
    self.addCleanup(directory.cleanup)
    app = setup_app(AsyncMock(), self.client, directory.name,
                    directory.name + "/avatar", directory.name + "/ghost")
    self.assertIs(app["telegram_client"], self.client)
    self.client.connect.assert_not_awaited()
    self.client.start.assert_not_awaited()
    app.freeze()
    await app.cleanup()

  async def test_http_access_and_refresh_use_indexer_client(self):
    user_id = "11111111-1111-4111-8111-111111111111"
    with patch.dict(os.environ, {"LUOXU_TELEGRAM_BOT_TOKEN": "123:synthetic"}):
      auth = AuthService({"jwt_secret": "synthetic-secret-" * 3, "telegram": {
        "enabled": True, "membership_source": "userbot", "membership_cache_ttl": 0,
        "accounts": [{"telegram_id": 42, "user_id": user_id}],
      }})
    now = datetime.datetime.now(datetime.UTC)
    user = {"id": user_id, "username": "test", "is_active": True, "is_admin": False,
            "created_at": now, "updated_at": now}
    db = AsyncMock()
    db.get_user.return_value = user
    db.consume_refresh_token.return_value = user
    db.telegram_group_candidates.return_value = [{"id": user_id, "kind": "group", "telegram_peer_type": "channel",
                                                  "telegram_peer_id": 123}]
    self.client.return_value = types.channels.ChannelParticipant(types.ChannelParticipant(42, None), [], [])
    directory = tempfile.TemporaryDirectory()
    self.addCleanup(directory.cleanup)
    app = setup_app(db, self.client, directory.name, directory.name + "/avatar",
                    directory.name + "/ghost", auth_service=auth)
    async with TestClient(TestServer(app)) as http:
      headers = {"Authorization": "Bearer " + auth.access_token(user, 42)}
      self.assertEqual((await http.get("/auth/me", headers=headers)).status, 200)
      refresh, _, _ = auth.refresh_token(user, 42)
      self.assertEqual((await http.post("/auth/refresh", json={"refresh_token": refresh})).status, 200)
      self.client.return_value = types.channels.ChannelParticipant(types.ChannelParticipantLeft(types.PeerUser(42)), [], [])
      self.assertEqual((await http.get("/auth/me", headers=headers)).status, 401)
      self.assertEqual((await http.post("/auth/refresh", json={"refresh_token": refresh})).status, 401)
      self.assertEqual(db.consume_refresh_token.await_count, 1)

  async def test_invalid_provider_configuration(self):
    for value in ("", "other", None, True, []):
      with self.subTest(value=value), self.assertRaises(ValueError):
        TelegramAuth({"membership_source": value})
