import asyncio
import contextlib
import importlib
import inspect
import logging
import operator
import os
import re
from functools import partial
from typing import Any, cast

from aiohttp import web  # type: ignore[import-not-found]
from telethon import events, utils  # type: ignore[import-not-found]

from . import web as myweb
from .auth import AuthService  # type: ignore[import-not-found]
from .ctxvars import msg_source
from .db import PostgreStore
from .group import GroupHistoryIndexer, PrivateHistoryIndexer
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
      config["database"], client, history_enabled=history_enabled,
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
    ocr_ignore_group_ids = []
    group_entities = []
    private_entities = []
    dialogs = None
    for g in tg_config["index_groups"]:
      g = str(g)
      if g.startswith("@"):
        group = cast(Any, await client.get_entity(g))
      else:
        g2 = 0
        try:
          g2 = int(g)
          group = cast(Any, await client.get_entity(g2))
        except ValueError:
          if dialogs is None:
            dialogs = await client.get_dialogs()
          group = next(d.entity for d in dialogs if d.entity.id == g2)

      if g in tg_config.get("ocr_ignore_groups", ()):
        ocr_ignore_group_ids.append(group.id)

      group_entities.append(group)

    for private in tg_config.get("index_private_chats", ()):
      try:
        target = private if str(private).startswith("@") else int(private)
        entity = cast(Any, await client.get_entity(target))
      except (TypeError, ValueError):
        logger.exception("invalid private chat configuration: %r", private)
        raise
      private_entities.append(entity)
      self.indexed_private_ids.add(entity.id)

    self.ocr_ignore_group_ids = ocr_ignore_group_ids
    self.client = client
    indexed_entities = group_entities + private_entities
    client.add_event_handler(self.on_message, events.NewMessage(chats=indexed_entities))
    client.add_event_handler(
      self.on_message, events.MessageEdited(chats=indexed_entities)
    )
    client.add_event_handler(
      self.on_deleted, events.MessageDeleted(chats=indexed_entities)
    )

    await self.load_plugins(client)

    try:
      while True:
        try:
          await self.run_on_connected(client, db, group_entities)
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

  async def run_on_connected(self, client, db, group_entities):
    self.group_forward_history_done = {}
    runnables = []
    for group in group_entities:
      ginfo = await self.init_group(group)
      use_ocr = group.id not in self.ocr_ignore_group_ids
      gi = GroupHistoryIndexer(group, ginfo, use_ocr)
      runnables.append(
        gi.run(
          client,
          db,
          partial(operator.setitem, self.group_forward_history_done, group.id, True),
        )
      )
    web_config = self.config["web"]
    await db.bootstrap(
      web_config["auth"],
      web_config.get("public_groups", ()),
      AuthService(web_config["auth"]),
    )
    for private_id in self.indexed_private_ids:
      entity = await client.get_entity(private_id)
      runnables.append(PrivateHistoryIndexer(entity, True).run(client, db))

    if not client.is_connected():
      await _start_client(client, self.config["telegram"]["account"])
      # reset last ping to avoid reconnecting every 60s
      logger.info("resetting client._sender._ping")
      client._sender._ping = None

    # we do need to fetch history on startup because telethon doesn't
    # record group's pts in database.
    #
    # we also need to fetch history on reconnect because sometimes we still
    # don't see some missed updates (I don't know why).
    #
    # we may still miss edits that happen while we're offline and missed
    # the updates.
    gis = asyncio.gather(*runnables)
    # await client.catch_up()
    try:
      await client.run_until_disconnected()
    finally:
      gis.cancel()
      with contextlib.suppress(asyncio.CancelledError):
        await gis

  async def add_group(self, target):
    if self.client is None or self.dbstore is None:
      raise RuntimeError("Telegram client is not ready")
    try:
      entity = cast(Any, await self.client.get_entity(target if not target.lstrip("-").isdigit() else int(target)))
    except Exception as exc:
      raise ValueError("group not found") from exc
    if entity.id in self.group_forward_history_done:
      async with self.dbstore.get_conn() as conn:
        return await self.dbstore.get_group(conn, entity.id)
    info = await self.init_group(entity)
    self.group_forward_history_done[entity.id] = False
    self.client.add_event_handler(self.on_message, events.NewMessage(chats=[entity]))
    self.client.add_event_handler(self.on_message, events.MessageEdited(chats=[entity]))
    asyncio.create_task(GroupHistoryIndexer(entity, info, entity.id not in self.ocr_ignore_group_ids).run(self.client, self.dbstore, partial(operator.setitem, self.group_forward_history_done, entity.id, True)))
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
