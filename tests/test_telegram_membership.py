"""Membership admission with synthetic Bot API results; no Telegram network."""

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from luoxu.telegram_auth import MembershipUnavailable, TelegramAuth


class MembershipTests(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    with patch.dict(os.environ, {"LUOXU_TELEGRAM_BOT_TOKEN": "123:synthetic"}):
      self.auth = TelegramAuth({"enabled": True})
    self.db = AsyncMock()
    self.group = {"kind": "group", "telegram_peer_type": "channel",
                  "telegram_peer_id": 123, "reference_count": 1}
    self.db.list_group_monitoring.return_value = [self.group]
    self.lookup = AsyncMock(return_value=True)

  async def test_live_monitoring_scope_cache_and_stopping(self):
    with patch.object(self.auth, "_lookup_member", self.lookup):
      self.assertTrue(await self.auth.is_indexed_group_member(self.db, 42))
      self.assertTrue(await self.auth.is_indexed_group_member(self.db, 42))
      self.lookup.assert_awaited_once()
      self.assertEqual(self.lookup.call_args.args[1:], (-1000000000123, 42))
      self.db.list_group_monitoring.return_value = [{**self.group, "reference_count": 0}]
      self.assertFalse(await self.auth.is_indexed_group_member(self.db, 42))
      self.db.list_group_monitoring.return_value = [{**self.group, "kind": "private_chat"}]
      self.assertFalse(await self.auth.is_indexed_group_member(self.db, 42))
      self.db.list_group_monitoring.return_value = [{**self.group, "telegram_peer_type": "chat"}]
      self.assertTrue(await self.auth.is_indexed_group_member(self.db, 42))
      self.assertEqual(self.lookup.call_args.args[1:], (-123, 42))

  async def test_expired_membership_and_other_user_not_shared(self):
    with patch.object(self.auth, "_lookup_member", self.lookup):
      self.auth.membership_cache_ttl = 0
      self.assertTrue(await self.auth.is_indexed_group_member(self.db, 42))
      self.lookup.return_value = False
      self.assertFalse(await self.auth.is_indexed_group_member(self.db, 42))
      self.assertFalse(await self.auth.is_indexed_group_member(self.db, 43))
      self.assertEqual(self.lookup.await_count, 3)

  async def test_failure_closed_but_another_indexed_group_can_admit(self):
    with patch.object(self.auth, "_lookup_member", self.lookup):
      self.lookup.side_effect = MembershipUnavailable
      with self.assertRaises(MembershipUnavailable):
        await self.auth.is_indexed_group_member(self.db, 42)
      self.db.list_group_monitoring.return_value.append({**self.group, "telegram_peer_id": 456})
      self.lookup.side_effect = [MembershipUnavailable, True]
      self.assertTrue(await self.auth.is_indexed_group_member(self.db, 42))

  async def test_bot_api_statuses_and_request_contract(self):
    for member, allowed in [({"status": "creator"}, True), ({"status": "administrator"}, True),
                            ({"status": "member"}, True), ({"status": "left"}, False),
                            ({"status": "kicked"}, False),
                            ({"status": "restricted", "is_member": True}, True),
                            ({"status": "restricted", "is_member": False}, False)]:
      session = MagicMock()
      response = AsyncMock()
      response.status = 200
      response.json.return_value = {"ok": True, "result": member}
      session.post.return_value.__aenter__.return_value = response
      with self.subTest(member=member):
        self.assertEqual(await self.auth._lookup_member(session, -1000000000123, 42), allowed)
        self.assertEqual(session.post.call_args.kwargs["json"], {"chat_id": -1000000000123, "user_id": 42})
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])
    for status, body in [(403, {}), (429, {}), (200, {"ok": False}),
                         (200, {"ok": True, "result": {"status": "unknown"}}), (200, [])]:
      response.status, response.json.return_value = status, body
      with self.assertRaises(MembershipUnavailable):
        await self.auth._lookup_member(session, -1000000000123, 42)
