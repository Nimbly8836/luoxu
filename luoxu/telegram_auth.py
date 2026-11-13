"""Verify Mini App initData; never trust Telegram's client-side user object."""

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import parse_qsl

from aiohttp import ClientError, ClientSession, ClientTimeout


class MembershipUnavailable(Exception):
  """A membership decision could not be established; never includes a Bot URL."""


@dataclass(frozen=True)
class TelegramIdentity:
  telegram_id: int
  user_id: str
  fingerprint: str
  expires_at: int


class TelegramAuth:
  def __init__(self, config):
    self.enabled = config.get("enabled", False)
    if not isinstance(self.enabled, bool):
      raise ValueError("telegram.enabled must be a boolean")
    self.accounts = {}
    self.max_age = 300
    self.secret = b""
    self._token = ""
    self._members = OrderedDict()
    self.membership_cache_ttl = config.get("membership_cache_ttl", 60)
    if type(self.membership_cache_ttl) is not int or not 0 <= self.membership_cache_ttl <= 300:
      raise ValueError("telegram.membership_cache_ttl must be 0 to 300 seconds")
    if not self.enabled:
      return
    self.max_age = config.get("max_age", 300)
    if type(self.max_age) is not int or not 30 <= self.max_age <= 3600:
      raise ValueError("telegram.max_age must be 30 to 3600 seconds")
    token = os.environ.get(config.get("bot_token_env", "LUOXU_TELEGRAM_BOT_TOKEN"), "")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
      raise ValueError("Telegram bot token environment variable is missing or invalid")
    self._token = token
    self.secret = hmac.digest(b"WebAppData", token.encode(), "sha256")
    for account in config.get("accounts", []):
      telegram_id = account["telegram_id"]
      if type(telegram_id) is not int or not 0 < telegram_id < 2**52:
        raise ValueError("telegram_id must be a positive Telegram numeric user ID")
      if telegram_id in self.accounts:
        raise ValueError("duplicate Telegram account binding")
      self.accounts[telegram_id] = str(uuid.UUID(account["user_id"]))

  def verify(self, raw, *, now=None):
    if not self.enabled:
      raise ValueError("Telegram login is disabled")
    if not isinstance(raw, str) or not raw or len(raw) > 16384:
      raise ValueError("invalid initData")
    if re.search(r"%(?![0-9a-fA-F]{2})", raw):
      raise ValueError("invalid encoding")
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True,
                      errors="strict", max_num_fields=64)
    fields = dict(pairs)
    if len(fields) != len(pairs) or any("\n" in key or "\r" in key for key in fields):
      raise ValueError("duplicate or invalid fields")
    signature = fields.pop("hash", "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", signature):
      raise ValueError("invalid hash")
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    expected = hmac.digest(self.secret, check.encode(), "sha256")
    if not hmac.compare_digest(expected, bytes.fromhex(signature)):
      raise ValueError("invalid signature")
    auth_date = fields.get("auth_date", "")
    if not re.fullmatch(r"[0-9]{1,12}", auth_date):
      raise ValueError("invalid auth_date")
    issued = int(auth_date)
    now = time.time() if now is None else now
    if issued > now + 30 or now >= issued + self.max_age:
      raise ValueError("expired or future initData")
    try:
      user = json.loads(fields.get("user", ""))
    except (ValueError, RecursionError) as exc:
      raise ValueError("invalid user") from exc
    if not isinstance(user, dict) or type(user.get("id")) is not int:
      raise ValueError("invalid user id")
    user_id = self.accounts.get(user["id"])
    if user_id is None:
      raise ValueError("unbound Telegram user")
    # Canonical verified MAC, not the raw query: reordering/encoding cannot bypass replay checks.
    return TelegramIdentity(user["id"], user_id, hashlib.sha256(expected).hexdigest(),
                            issued + self.max_age)

  async def _lookup_member(self, session, chat_id, telegram_id):
    # Never log/propagate transport exceptions: Bot API URLs contain the secret.
    try:
      async with session.post(
        f"https://api.telegram.org/bot{self._token}/getChatMember",
        json={"chat_id": chat_id, "user_id": telegram_id},
        allow_redirects=False,
      ) as response:
        if response.status != 200:
          raise MembershipUnavailable
        payload = await response.json()
      member = payload.get("result")
      if payload.get("ok") is not True or not isinstance(member, dict):
        raise MembershipUnavailable
      if member.get("status") in ("creator", "administrator", "member"):
        return True
      if member.get("status") == "restricted":
        return member.get("is_member") is True
      if member.get("status") in ("left", "kicked"):
        return False
      raise MembershipUnavailable
    except (ClientError, TimeoutError, ValueError, AttributeError):
      raise MembershipUnavailable from None

  async def is_indexed_group_member(self, db, telegram_id):
    # Re-read monitoring intent even when Telegram results are cached: stopped
    # archives, private chats and static index_groups are not admission sources.
    groups = await db.list_group_monitoring()
    candidates = {
      -(1000000000000 + row["telegram_peer_id"]) if row["telegram_peer_type"] == "channel"
      else -row["telegram_peer_id"]
      for row in groups if row["reference_count"] > 0 and row["kind"] == "group"
      and row["telegram_peer_type"] in ("channel", "chat")
    }
    failed = False
    try:
      async with asyncio.timeout(10), ClientSession(timeout=ClientTimeout(total=4)) as session:
        for chat_id in sorted(candidates):
          key = (chat_id, telegram_id)
          cached = self._members.get(key)
          if cached and cached[0] > time.monotonic():
            if cached[1]:
              return True
            continue
          try:
            member = await self._lookup_member(session, chat_id, telegram_id)
          except MembershipUnavailable:
            failed = True
            continue
          self._members[key] = (time.monotonic() + self.membership_cache_ttl, member)
          self._members.move_to_end(key)
          while len(self._members) > 1024:
            self._members.popitem(last=False)
          if member:
            return True
    except TimeoutError:
      raise MembershipUnavailable from None
    if failed:
      raise MembershipUnavailable
    return False
