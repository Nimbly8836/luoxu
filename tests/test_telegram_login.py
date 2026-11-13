"""Synthetic Mini App signatures; never contact Telegram or use production data."""

import asyncio
import datetime
import hashlib
import hmac
import json
import os
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import asyncpg
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from luoxu.auth import AuthService
from luoxu.db import PostgreStore
from luoxu.telegram_auth import MembershipUnavailable, TelegramAuth
from luoxu.web import AuthHandler, auth_middleware

TOKEN = "123456:synthetic-test-token"  # noqa: S105 — public synthetic fixture, not a credential
USER_ID = "11111111-1111-4111-8111-111111111111"
CONFIG = {"enabled": True, "accounts": [{"telegram_id": 42, "user_id": USER_ID}]}


def signed(**overrides):
  fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": 42}),
            "query_id": "synthetic-launch", **overrides}
  check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
  key = hmac.digest(b"WebAppData", TOKEN.encode(), "sha256")
  fields["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
  return urlencode(fields)


class VerificationTests(unittest.TestCase):
  def setUp(self):
    env = patch.dict(os.environ, {"LUOXU_TELEGRAM_BOT_TOKEN": TOKEN})
    env.start()
    self.addCleanup(env.stop)
    self.auth = TelegramAuth(CONFIG)

  def test_valid_and_reordered_launch_have_same_identity(self):
    raw = signed(user=json.dumps({"id": 42, "username": "not-a-local-login", "is_admin": True}))
    result = self.auth.verify(raw)
    self.assertEqual(result.user_id, USER_ID)
    self.assertEqual(result, self.auth.verify("&".join(reversed(raw.split("&")))))

  def test_invalid_inputs(self):
    now = int(time.time())
    raw = signed()
    invalid = [None, "", "x" * 16385, raw.replace("synthetic-launch", "tampered"),
               raw + "&hash=" + "0" * 64, raw + "&user=%7B%7D", raw + "&x=%GG",
               "hash=" + "0" * 64, signed(auth_date=str(now - 300)),
               signed(auth_date=str(now + 31)), signed(auth_date="NaN"),
               signed(user="null"), signed(user="[]"), signed(user="{"),
               signed(user=json.dumps({"id": True})), signed(user=json.dumps({"id": "42"})),
               signed(user=json.dumps({"id": 999, "username": "admin", "is_admin": True}))]
    for value in invalid:
      with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
        self.auth.verify(value, now=now)

  def test_other_bot_rejected(self):
    raw = signed()
    with patch.dict(os.environ, {"LUOXU_TELEGRAM_BOT_TOKEN": "999:other-token"}):
      other = TelegramAuth(CONFIG)
    with self.assertRaises(ValueError):
      other.verify(raw)

  def test_time_boundaries(self):
    raw = signed(auth_date="1000")
    self.auth.verify(raw, now=970)
    self.auth.verify(raw, now=1299)
    for now in (969, 1300):
      with self.assertRaises(ValueError):
        self.auth.verify(raw, now=now)

  def test_config_fail_closed(self):
    for changes in ({"enabled": "true"}, {"max_age": 0}, {"max_age": True},
                    {"max_age": 3601}, {"accounts": CONFIG["accounts"] * 2},
                    {"accounts": [{"telegram_id": True, "user_id": USER_ID}]},
                    {"accounts": [{"telegram_id": 42, "user_id": "admin"}]}):
      with self.subTest(changes=changes), self.assertRaises(ValueError):
        TelegramAuth({**CONFIG, **changes})
    with patch.dict(os.environ, {"LUOXU_TELEGRAM_BOT_TOKEN": ""}):
      with self.assertRaises(ValueError):
        TelegramAuth(CONFIG)
      TelegramAuth({})
    with self.assertRaises(ValueError):
      TelegramAuth({}).verify(signed())


class TelegramHTTPTests(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    with patch.dict(os.environ, {"LUOXU_TELEGRAM_BOT_TOKEN": TOKEN}):
      self.auth = AuthService({"jwt_secret": "test-secret-" * 4, "telegram": CONFIG})
    self.member_check = AsyncMock(return_value=True)
    self.auth.telegram.is_indexed_group_member = self.member_check
    now = datetime.datetime.now(datetime.UTC)
    self.user = {"id": USER_ID, "username": "bound-account", "is_admin": False,
                 "is_active": True, "created_at": now, "updated_at": now}
    self.db = AsyncMock()
    self.db.get_user.return_value = self.user
    seen = set()

    async def consume(fingerprint, expires):
      if fingerprint in seen:
        return False
      seen.add(fingerprint)
      return True

    self.db.consume_telegram_init_data.side_effect = consume
    self.db.consume_refresh_token.return_value = self.user
    app = web.Application(middlewares=[auth_middleware])
    app["auth"], app["db"] = self.auth, self.db
    handler = AuthHandler(self.db, self.auth)
    app.router.add_post("/auth/telegram", handler.telegram)
    app.router.add_post("/auth/refresh", handler.refresh)
    app.router.add_get("/auth/me", handler.me)
    self.client = TestClient(TestServer(app))
    await self.client.start_server()
    self.addAsyncCleanup(self.client.close)

  async def test_exchange_me_refresh_and_replay(self):
    raw = signed()
    response = await self.client.post("/auth/telegram", json={"init_data": raw})
    self.assertEqual(response.status, 200)
    self.assertEqual(response.headers["Cache-Control"], "private, no-store")
    tokens = await response.json()
    claims = self.auth.decode(tokens["access_token"])
    self.assertEqual(claims["sub"], USER_ID)
    self.assertFalse(claims["admin"])
    self.assertEqual(claims["telegram_id"], 42)
    me = await self.client.get("/auth/me", headers={"Authorization": "Bearer " + tokens["access_token"]})
    self.assertEqual((await me.json())["user"]["username"], "bound-account")
    refresh = await self.client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    self.assertEqual(refresh.status, 200)
    self.assertEqual(self.auth.decode((await refresh.json())["access_token"])["telegram_id"], 42)
    replay = await self.client.post("/auth/telegram", json={"init_data": "&".join(reversed(raw.split("&")))})
    self.assertEqual(replay.status, 401)
    self.assertEqual(self.db.save_refresh_token.await_count, 2)

  async def test_invalid_inactive_missing_and_disabled(self):
    for body in ({}, {"init_data": "forged", "user": {"id": 42}},
                 {"init_data": signed(user='{"id": 999}')}, {"init_data": []}):
      response = await self.client.post("/auth/telegram", json=body)
      self.assertEqual(response.status, 401)
    self.db.consume_telegram_init_data.assert_not_awaited()
    for user in (None, {**self.user, "is_active": False}):
      self.db.get_user.return_value = user
      response = await self.client.post("/auth/telegram", json={"init_data": signed()})
      self.assertEqual(response.status, 401)
    self.db.save_refresh_token.assert_not_awaited()
    self.auth.telegram.enabled = False
    response = await self.client.post("/auth/telegram", json={"init_data": signed()})
    self.assertEqual(response.status, 503)

  async def test_membership_rechecked_for_access_and_refresh(self):
    response = await self.client.post("/auth/telegram", json={"init_data": signed()})
    tokens = await response.json()
    for failure, expected in ((False, 401), (MembershipUnavailable(), 503)):
      check = self.member_check
      check.return_value = failure
      check.side_effect = failure if isinstance(failure, Exception) else None
      me = await self.client.get("/auth/me", headers={"Authorization": "Bearer " + tokens["access_token"]})
      self.assertEqual(me.status, expected)
      refresh = await self.client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
      self.assertEqual(refresh.status, expected)
      login = await self.client.post("/auth/telegram", json={"init_data": signed(query_id="new")})
      self.assertEqual(login.status, expected)
    self.db.consume_refresh_token.assert_not_awaited()
    # Password sessions are outside the Mini App admission rule.
    me = await self.client.get("/auth/me", headers={"Authorization": "Bearer " + self.auth.access_token(self.user)})
    self.assertEqual(me.status, 200)

  async def test_binding_removal_revokes_telegram_access(self):
    response = await self.client.post("/auth/telegram", json={"init_data": signed()})
    tokens = await response.json()
    self.auth.telegram.accounts.clear()
    me = await self.client.get("/auth/me", headers={"Authorization": "Bearer " + tokens["access_token"]})
    self.assertEqual(me.status, 401)

  async def test_concurrent_exchange_only_one_session(self):
    raw = signed()
    responses = await asyncio.gather(*[
      self.client.post("/auth/telegram", json={"init_data": raw}) for _ in range(5)
    ])
    self.assertEqual(sorted(r.status for r in responses), [200, 401, 401, 401, 401])
    self.db.save_refresh_token.assert_awaited_once()


@unittest.skipUnless(os.environ.get("LUOXU_TEST_DATABASE_URL"), "requires disposable PostgreSQL")
class ReplayStorageTests(unittest.IsolatedAsyncioTestCase):
  async def test_migration_concurrency_and_expiry(self):
    url = os.environ["LUOXU_TEST_DATABASE_URL"]
    conn = await asyncpg.connect(url)
    schema = "telegram_test_" + uuid.uuid4().hex
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    db = PostgreStore({"url": url})
    try:
      db.pool = await asyncpg.create_pool(url, min_size=1, max_size=5,
                                         server_settings={"search_path": schema})
      async with db.get_conn() as c:
        migration = (Path(__file__).resolve().parents[1] / "migrations/007_telegram_login.sql").read_text()
        # Fixed repository migration file, no request-controlled SQL.
        # pi-lens-ignore: python-sql-injection
        await c.execute(migration)
        # pi-lens-ignore: python-sql-injection
        await c.execute(migration)
      expires = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=300)
      results = await asyncio.gather(*[db.consume_telegram_init_data("a" * 64, expires) for _ in range(10)])
      self.assertEqual(sum(results), 1)
      # A separate connection pool/process sees the same durable replay record.
      other = PostgreStore({"url": url})
      other.pool = await asyncpg.create_pool(url, min_size=1, max_size=1,
                                           server_settings={"search_path": schema})
      try:
        self.assertFalse(await other.consume_telegram_init_data("a" * 64, expires))
      finally:
        await other.close()
      self.assertFalse(await db.consume_telegram_init_data("expired", expires - datetime.timedelta(seconds=600)))
    finally:
      await db.close()
      await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
      await conn.close()
