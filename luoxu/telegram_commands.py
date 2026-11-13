"""Private-chat default-group commands; no public group-list replies."""

import asyncio
import logging
import re
import time
import uuid
from contextlib import suppress

from aiohttp import ClientError, ClientSession, ClientTimeout

from .telegram_auth import MembershipUnavailable

logger = logging.getLogger(__name__)
PAGE_SIZE = 8
MAX_COMMAND_AGE = 300


class BotAPIError(Exception):
  """Sanitized error: never carries a token-bearing URL or Telegram message."""

  def __init__(self, code=0, retry_after=5):
    super().__init__(f"Bot API unavailable ({code})")
    self.code = code
    self.retry_after = min(max(retry_after, 1), 60)


class BotAPI:
  def __init__(self, token, session):
    self._token = token
    self.session = session

  async def call(self, method, payload):
    try:
      async with self.session.post(
        f"https://api.telegram.org/bot{self._token}/{method}",
        json=payload, allow_redirects=False,
      ) as response:
        data = await response.json()
        if not isinstance(data, dict):
          raise BotAPIError
        if response.status != 200 or data.get("ok") is not True:
          retry = data.get("parameters", {}).get("retry_after", 5)
          code = data.get("error_code", response.status)
          raise BotAPIError(code if type(code) is int else response.status,
                            retry if type(retry) is int else 5)
        return data.get("result")
    except (ClientError, TimeoutError, ValueError, AttributeError):
      raise BotAPIError from None


def private_sender(message, sender):
  chat = message.get("chat", {}) if isinstance(message, dict) else {}
  if not isinstance(chat, dict) or not isinstance(sender, dict):
    return None
  uid = sender.get("id")
  if (type(uid) is not int or not 0 < uid < 2**52 or sender.get("is_bot") is not False
      or chat.get("type") != "private" or type(chat.get("id")) is not int or chat["id"] != uid):
    return None
  return uid


class TelegramCommands:
  def __init__(self, db, auth, client, api=None):
    self.db, self.auth, self.client = db, auth, client
    self.api = api
    self.bot_id = int(auth._token.split(":", 1)[0])
    self.username = ""
    self.task = None
    self.stopping = asyncio.Event()
    self.status = "stopped"

  async def start(self, _app):
    self.task = asyncio.create_task(self.run(), name="telegram-default-commands")

  async def close(self, _app):
    self.stopping.set()
    if self.task:
      self.task.cancel()
      with suppress(asyncio.CancelledError):
        await self.task
    self.status = "stopped"

  async def pause(self, seconds):
    with suppress(TimeoutError):
      await asyncio.wait_for(self.stopping.wait(), seconds)

  async def register_menu(self):
    assert self.api is not None
    info = await self.api.call("getWebhookInfo", {})
    if not isinstance(info, dict) or info.get("url"):
      raise BotAPIError(409)  # Never delete or replace somebody else's webhook.
    me = await self.api.call("getMe", {})
    if (not isinstance(me, dict) or me.get("id") != self.bot_id
        or not isinstance(me.get("username"), str)):
      raise BotAPIError
    self.username = me["username"]
    scope = {"type": "all_private_chats"}
    commands = await self.api.call("getMyCommands", {"scope": scope})
    if not isinstance(commands, list):
      raise BotAPIError
    commands = [item for item in commands if isinstance(item, dict) and item.get("command") != "default"]
    commands.append({"command": "default", "description": "设置或清除个人默认群组"})
    if len(commands) > 100:
      raise BotAPIError(400)
    if await self.api.call("setMyCommands", {"scope": scope, "commands": commands}) is not True:
      raise BotAPIError

  async def run(self):
    lock_name = f"luoxu:telegram-commands:{self.bot_id}"
    async with ClientSession(timeout=ClientTimeout(total=35)) as session:
      self.api = self.api or BotAPI(self.auth._token, session)
      while not self.stopping.is_set():
        try:
          if self.db.pool is None:
            raise RuntimeError("database pool is unavailable")
          async with self.db.pool.acquire(timeout=10) as conn:
            acquired = await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1,0))", lock_name)
            if not acquired:
              self.status = "standby"
            else:
              try:
                await self.register_menu()
                self.status = "running"
                logger.info("Telegram default commands ready (private chats only)")
                offset = await conn.fetchval(
                  """SELECT CASE WHEN updated_at < now()-interval '6 days' THEN 0 ELSE next_update_id END
                     FROM telegram_bot_offsets WHERE bot_id=$1""", self.bot_id,
                ) or 0
                last_update = time.monotonic()
                while not self.stopping.is_set():
                  if self.client is not None and not self.client.is_connected():
                    await self.pause(1)
                    continue
                  # A dead lease must stop polling before another worker takes over.
                  await conn.execute("SELECT 1")
                  if time.monotonic() - last_update > 6 * 86400:
                    offset = 0  # Telegram may randomize update IDs after a week idle.
                  updates = await self.api.call("getUpdates", {
                    "offset": offset, "timeout": 20, "limit": 50,
                    "allowed_updates": ["message", "callback_query"],
                  })
                  if not isinstance(updates, list):
                    raise BotAPIError
                  await conn.execute("SELECT 1")
                  progressed = False
                  for update in updates:
                    update_id = update.get("update_id") if isinstance(update, dict) else None
                    if type(update_id) is not int or update_id < offset:
                      continue
                    try:
                      await self.handle_update(update)
                    except BotAPIError as exc:
                      # Blocked users / expired callbacks cannot poison the queue.
                      if exc.code not in (400, 403):
                        raise
                    offset = update_id + 1
                    progressed = True
                    # Fixed SQL; both values are PostgreSQL bind parameters.
                    # pi-lens-ignore: python-sql-injection
                    await conn.execute(
                      """INSERT INTO telegram_bot_offsets(bot_id,next_update_id)
                         VALUES ($1,$2) ON CONFLICT(bot_id) DO UPDATE
                         SET next_update_id=EXCLUDED.next_update_id, updated_at=now()""",
                      self.bot_id, offset,
                    )
                    last_update = time.monotonic()
                  if updates and not progressed:
                    raise BotAPIError
              finally:
                if not conn.is_closed():
                  # Fixed SQL; the lock namespace is bound, not interpolated.
                  # pi-lens-ignore: python-sql-injection
                  await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1,0))", lock_name)
        except asyncio.CancelledError:
          raise
        except BotAPIError as exc:
          self.status = "blocked" if exc.code in (401, 409) else "retrying"
          logger.warning("Telegram commands %s (API status %s)", self.status, exc.code)
          if self.status == "blocked":
            return
          await self.pause(exc.retry_after)
          continue
        except Exception as exc:
          self.status = "retrying"
          # Exception messages/tracebacks can contain credentials or message text.
          logger.warning("Telegram commands retrying (%s)", type(exc).__name__)
        await self.pause(5)

  async def eligible_groups(self, telegram_id):
    if not self.auth.enabled or not self.auth.commands_enabled:
      return []
    bound = self.auth.accounts.get(telegram_id)
    user = await self.db.get_user(bound) if bound else await self.db.get_telegram_user(telegram_id)
    if (bound and not user) or (user and not user["is_active"]):
      return []
    try:
      visible = set(await self.auth.visible_groups(self.db, telegram_id, self.client))
    except MembershipUnavailable:
      return []
    if not visible:
      return []
    # Re-read publication/monitoring after Telegram I/O; never use a stale keyboard as authority.
    rows = await self.db.telegram_group_candidates(telegram_id == self.auth.owner_telegram_id)
    return [row for row in rows if row["id"] in visible]

  async def send(self, telegram_id, text, keyboard=None):
    assert self.api is not None
    payload = {"chat_id": telegram_id, "text": text}
    if keyboard is not None:
      payload["reply_markup"] = {"inline_keyboard": keyboard}
    await self.api.call("sendMessage", payload)

  async def show_groups(self, telegram_id, groups, page=0):
    page = min(max(page, 0), (len(groups) - 1) // PAGE_SIZE)
    current = await self.db.get_telegram_default_id(telegram_id)
    keyboard = [[{
      "text": ("✓ " if row["id"] == current else "") + str(row["name"]).replace("\n", " ")[:48],
      "callback_data": f"default:set:{row['id']}",
    }] for row in groups[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
    navigation = []
    if page:
      navigation.append({"text": "上一页", "callback_data": f"default:page:{page - 1}"})
    if (page + 1) * PAGE_SIZE < len(groups):
      navigation.append({"text": "下一页", "callback_data": f"default:page:{page + 1}"})
    if navigation:
      keyboard.append(navigation)
    keyboard.append([{"text": "清除个人默认群", "callback_data": "default:clear"}])
    await self.send(telegram_id, "请选择默认群组（按钮5分钟有效）；设置后关闭并重新打开小程序生效。", keyboard)

  async def handle_update(self, update):
    assert self.api is not None
    if not isinstance(update, dict):
      return
    callback = update.get("callback_query")
    if isinstance(callback, dict):
      message = callback.get("message")
      telegram_id = private_sender(message, callback.get("from"))
      data = callback.get("data")
      if (not isinstance(message, dict) or telegram_id is None
          or not isinstance(data, str) or len(data.encode()) > 64 or not data.startswith("default:")):
        return
      query_id = callback.get("id")
      if not isinstance(query_id, str) or len(query_id) > 256:
        return
      try:
        await self.api.call("answerCallbackQuery", {"callback_query_id": query_id})
      except BotAPIError as exc:
        if exc.code != 400:
          raise
      action = data.removeprefix("default:")
    else:
      message = update.get("message")
      if not isinstance(message, dict) or message.get("forward_origin"):
        return
      telegram_id = private_sender(message, message.get("from"))
      text = message.get("text")
      if telegram_id is None or not isinstance(text, str):
        return
      match = re.fullmatch(r"/default(?:@([A-Za-z0-9_]+))?(?:\s+(.{0,128}))?\s*", text.strip())
      if not match or (match[1] and match[1].casefold() != self.username.casefold()):
        return
      argument = (match[2] or "").strip()
      action = "clear" if argument == "clear" else ("legacy:" + argument if argument else "page:0")
    # Old updates/keyboards never silently change a preference after a long downtime.
    date = message.get("date")
    if type(date) is not int or not -30 <= time.time() - date <= MAX_COMMAND_AGE:
      return
    groups = await self.eligible_groups(telegram_id)
    if not groups:
      return
    if action == "clear":
      await self.db.set_telegram_default_id(telegram_id, None)
      await self.send(telegram_id, "已清除个人默认群组。下次打开小程序将使用站点默认值或唯一可见群。")
      return
    if re.fullmatch(r"page:[0-9]{1,6}", action):
      await self.show_groups(telegram_id, groups, int(action.split(":")[1]))
      return
    target = None
    if action.startswith("set:"):
      try:
        cid = uuid.UUID(action.removeprefix("set:"))
        target = next((row for row in groups if row["id"] == cid), None)
      except ValueError:
        return
    elif action.startswith("legacy:"):
      value = action.removeprefix("legacy:")
      if not re.fullmatch(r"-?[0-9]{1,20}", value):
        return
      target = next((row for row in groups if int(value) in (
        row["legacy_group_id"], -row["telegram_peer_id"] - (
          1000000000000 if row["telegram_peer_type"] == "channel" else 0
        ),
      )), None)
    if target is None:
      # Do not confirm the existence of a guessed/inaccessible group.
      return
    await self.db.set_telegram_default_id(telegram_id, target["id"])
    title = str(target["name"]).replace("\n", " ")[:200]
    await self.send(telegram_id, f"已设置默认群组：{title}。关闭并重新打开小程序后生效。")
