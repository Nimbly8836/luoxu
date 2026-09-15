import asyncio
import importlib
import inspect
import logging
import os
import re
from typing import Any, cast

from aiohttp import web  # type: ignore[import-not-found]
from telethon import events, utils  # type: ignore[import-not-found]
from telethon.errors import RPCError
from telethon.tl import types

from . import web as myweb
from .auth import AuthService  # type: ignore[import-not-found]
from .ctxvars import msg_source
from .db import PostgreStore
from .group import GroupMonitor, MonitoringUnavailable, PrivateHistoryIndexer
from .util import UpdateLoaded, create_client, load_config, run_until_sigint

logger = logging.getLogger(__name__)


async def _start_client(client, account):
  result = client.start(account)
  if inspect.isawaitable(result):
    await result


class Indexer:
  def __init__(self, config):
    self.config = config
    self.mark_as_read = config["telegram"].get("mark_as_read", True)
    self.dbstore: PostgreStore | None = None
    self.client = None
    self.msg_handlers = []
    self.indexed_private_ids = set()
    self.ocr_ignore_group_ids = set()
    self.group_forward_history_done = {}
    self._monitor: GroupMonitor | None = None
    self._monitor_lock = asyncio.Lock()

  async def load_plugins(self, client):
    for plugin, conf in self.config.get("plugin", {}).items():
      if not conf.get("enabled", True):
        continue

      logger.info("loading plugin %s", plugin)
      mod = importlib.import_module(f"luoxu_plugins.{plugin}")
      ret = mod.register(self, client)
      if inspect.isawaitable(ret):
        await ret

  def add_msg_handler(self, handler, pattern=".*"):
    self.msg_handlers.append((handler, re.compile(pattern)))

  async def on_message(self, event):
    if isinstance(event, events.MessageEdited.Event):
      msg_source.set("editmsg")
    else:
      msg_source.set("newmsg")
    msg = event.message
    peer_id = getattr(msg.peer_id, "channel_id", None)
    if peer_id is None:
      peer_id = getattr(msg.peer_id, "chat_id", None)
    if peer_id is None:
      peer_id = getattr(msg.peer_id, "user_id", None)
    use_ocr = peer_id not in self.ocr_ignore_group_ids
    dbstore = self.dbstore

    if self.group_forward_history_done.get(peer_id, False):
      update_loaded = UpdateLoaded.update_last
    else:
      update_loaded = UpdateLoaded.update_none
    if dbstore is None:
      return
    await dbstore.insert_messages([msg], update_loaded, use_ocr)

    if self.mark_as_read:
      try:
        await msg.mark_read()
      except ConnectionError as e:
        logger.warning("cannot mark as read: %r", e)

    for handler, pattern in self.msg_handlers:
      logger.debug("message: %s, pattern: %s", msg.text, pattern)
      if msg.text and pattern.fullmatch(msg.text):
        asyncio.create_task(handler(event))

  async def run(self):
    config = self.config
    tg_config = config["telegram"]
    client = create_client(tg_config)

    web_config = config["web"]
    history_enabled = web_config.get("message_history", {}).get("enabled", False)
    db = PostgreStore(
      config["database"],
      client,
      history_enabled=history_enabled,
      repair_non_forum_groups=tg_config.get("repair_non_forum_groups", ()),
    )
    await db.setup()
    auth = AuthService(web_config["auth"])
    await db.bootstrap(web_config["auth"], None, auth)
    self.dbstore = db

    cache_dir = web_config["cache_dir"]
    try:
      os.makedirs(cache_dir, exist_ok=True)
    except OSError:
      logger.exception("cannot create avatar cache directory")
      raise
    app = myweb.setup_app(
      db,
      client,
      os.path.abspath(cache_dir),
      os.path.abspath(web_config["default_avatar"]),
      os.path.abspath(web_config["ghost_avatar"]),
      prefix=web_config["prefix"],
      origins=web_config["origins"],
      auth_service=auth,
      history_enabled=history_enabled,
      context_config=web_config.get("context"),
      add_group=self.add_group,
      monitoring_changed=self.sync_monitoring,
      monitoring_status=self.monitoring_status,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
      runner,
      web_config["listen_host"],
      web_config["listen_port"],
    )
    await site.start()

    await _start_client(client, tg_config["account"])
    self.client = client
    await self.import_group_config()
    ignored = {str(g) for g in tg_config.get("ocr_ignore_groups", ())}
    for row in await db.list_group_monitoring():
      peer_cls = (
        types.PeerChat if row["telegram_peer_type"] == "chat" else types.PeerChannel
      )
      references = {
        str(row["telegram_peer_id"]),
        str(utils.get_peer_id(peer_cls(row["telegram_peer_id"]))),
        "@" + (row["pub_id"] or ""),
        row["pub_id"],
      }
      if references & ignored:
        self.ocr_ignore_group_ids.add(row["telegram_peer_id"])

    private_entities = []
    for private in tg_config.get("index_private_chats", ()):
      try:
        target = private if str(private).startswith("@") else int(private)
        entity = cast(Any, await client.get_entity(target))
      except (TypeError, ValueError):
        logger.exception("invalid private chat configuration: %r", private)
        raise
      private_entities.append(entity)
      self.indexed_private_ids.add(entity.id)

    # Group callbacks belong to GroupMonitor so they can be removed on the
    # last-reference transition. Private chats still require explicit config.
    if private_entities:
      client.add_event_handler(
        self.on_message, events.NewMessage(chats=private_entities)
      )
      client.add_event_handler(
        self.on_message, events.MessageEdited(chats=private_entities)
      )
      client.add_event_handler(
        self.on_deleted, events.MessageDeleted(chats=private_entities)
      )

    await self.load_plugins(client)

    try:
      while True:
        try:
          await self.run_on_connected(client, db)
          logger.warning("disconnected, reconnecting in 1s")
          await asyncio.sleep(1)
        except (
          ConnectionError,
          asyncio.CancelledError,
          asyncio.exceptions.IncompleteReadError,
        ) as e:
          if isinstance(e.__context__, KeyboardInterrupt):
            break
          else:
            logger.exception("connection error, retry in 5s")
            await asyncio.sleep(5)
    finally:
      await runner.cleanup()
      await self.close_group_monitoring()
      await db.close()

  async def on_deleted(self, event):
    chat_id = getattr(event, "chat_id", None)
    if chat_id is None or self.dbstore is None:
      return
    peer_id, peer_cls = utils.resolve_id(chat_id)
    peer_type = {
      "PeerChannel": "channel",
      "PeerChat": "chat",
      "PeerUser": "user",
    }.get(peer_cls.__name__)
    if peer_type is None:
      logger.warning("ignoring deletion from unsupported peer %r", peer_cls)
      return
    kind = "private_chat" if peer_type == "user" else "group"
    await self.dbstore.delete_messages(
      peer_id,
      list(getattr(event, "deleted_ids", ())),
      kind=kind,
      peer_type=peer_type,
    )

  async def run_on_connected(self, client, db, group_entities=None):
    # The old group_entities argument is not authoritative after initial import.
    if not client.is_connected():
      await _start_client(client, self.config["telegram"]["account"])
      logger.info("resetting client._sender._ping")
      client._sender._ping = None
    web_config = self.config["web"]
    await db.bootstrap(
      web_config["auth"],
      web_config.get("public_groups", ()),
      AuthService(web_config["auth"]),
    )
    await self.sync_monitoring()
    monitor = self._monitor
    if monitor is None:
      raise MonitoringUnavailable("Telegram client is not connected")
    watcher = asyncio.create_task(monitor.run(), name="group-monitoring-refresh")
    runnables = []
    try:
      for private_id in self.indexed_private_ids:
        entity = await client.get_entity(private_id)
        runnables.append(
          asyncio.create_task(PrivateHistoryIndexer(entity, True).run(client, db))
        )
      # Reconnect starts fresh workers, which resume from archived cursors.
      # As before, Telegram cannot reconstruct edits missed while offline.
      await client.run_until_disconnected()
    finally:
      watcher.cancel()
      for task in runnables:
        task.cancel()
      await asyncio.gather(watcher, *runnables, return_exceptions=True)
      await self.close_group_monitoring()

  async def resolve_group(self, target):
    if self.client is None:
      raise MonitoringUnavailable("Telegram client is not ready")
    target = str(target).strip()
    number = None
    if target.lstrip("-").isdigit():
      try:
        number = int(target)
      except ValueError as exc:
        raise ValueError("invalid group ID") from exc
      if number == 0 or not -(2**63) <= number < 2**63:
        raise ValueError("group ID must be a nonzero signed 64-bit integer")
    try:
      entity = cast(
        Any,
        await self.client.get_entity(number if number is not None else target),
      )
    except ValueError:
      if number is None:
        raise
      entity = None
    if isinstance(entity, (types.Chat, types.Channel)):
      return entity
    if number is not None:
      peer_id, peer_cls = utils.resolve_id(number)
      expected = {
        types.PeerChat: types.Chat,
        types.PeerChannel: types.Channel,
      }.get(peer_cls)
      for dialog in await self.client.get_dialogs():
        entity = dialog.entity
        if (
          isinstance(entity, (types.Chat, types.Channel))
          and entity.id == peer_id
          and (expected is None or isinstance(entity, expected))
        ):
          return entity
    raise ValueError("target is not an accessible group")

  async def import_group_config(self):
    if self.dbstore is None:
      raise MonitoringUnavailable("database is not ready")
    if await self.dbstore.monitoring_config_imported():
      return
    groups = [
      await self.resolve_group(g)
      for g in self.config["telegram"].get("index_groups", ())
    ]
    await self.dbstore.import_monitoring_config(groups)

  def _group_monitor(self) -> GroupMonitor:
    monitor = self._monitor
    if monitor is None:
      monitor = GroupMonitor(self)
      self._monitor = monitor
    return monitor

  async def sync_monitoring(self, *, prime=None):
    async with self._monitor_lock:
      if (
        self.dbstore is not None
        and self.client is not None
        and self.client.is_connected()
      ):
        monitor = self._group_monitor()
        if prime is not None:
          monitor.prime(*prime)
        await monitor.reconcile()

  def monitoring_status(self):
    return self._monitor.statuses() if self._monitor else {}

  async def close_group_monitoring(self):
    async with self._monitor_lock:
      monitor, self._monitor = self._monitor, None
      if monitor is not None:
        await monitor.close()

  async def add_group(self, target):
    if self.client is None or self.dbstore is None:
      raise MonitoringUnavailable("Telegram client is not ready")
    try:
      entity = await self.resolve_group(target)
    except (ValueError, TypeError, RPCError) as exc:
      raise ValueError("group not found") from exc
    except ConnectionError as exc:
      raise MonitoringUnavailable("Telegram client is not connected") from exc
    info = await self.dbstore.add_monitored_group(entity)
    if str(target) in {
      str(g) for g in self.config["telegram"].get("ocr_ignore_groups", ())
    }:
      self.ocr_ignore_group_ids.add(entity.id)
    await self.sync_monitoring(prime=(info, entity))
    return info

  async def init_group(self, group):
    logger.info("init_group: %r", group.title)
    if self.dbstore is None:
      raise RuntimeError("database store is not initialized")
    async with self.dbstore.get_conn() as conn:
      return await self.dbstore.insert_group(conn, group)


if __name__ == "__main__":
  from .lib.nicelogger import enable_pretty_logging

  # enable_pretty_logging('DEBUG')
  enable_pretty_logging(logging.INFO)

  import argparse

  parser = argparse.ArgumentParser()
  parser.add_argument("--config", default="config.toml", help="config file path")
  args = parser.parse_args()

  config = load_config(args.config)
  indexer = Indexer(config)
  run_until_sigint(indexer.run(), name="indexer")
