import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from telethon import events
from telethon.tl import types

from .ctxvars import msg_source
from .util import UpdateLoaded

logger = logging.getLogger(__name__)


async def timed_get_messages(client, *args, **kwargs):
  while True:
    try:
      return await asyncio.wait_for(client.get_messages(*args, **kwargs), 60)
    except asyncio.TimeoutError:
      logger.error("timed out getting a message, retrying: %r, %r", args, kwargs)
      await asyncio.sleep(1)
    except Exception:
      logger.exception("error in get_messages")
      await asyncio.sleep(1)


class GroupHistoryIndexer:
  entity = None

  def __init__(self, entity, group_info, use_ocr):
    self.group_id = entity.id
    self.entity = entity
    self.group_info = group_info
    self.use_ocr = use_ocr

  async def run(self, client, dbstore, callback):
    msg_source.set("history")  # type: ignore[arg-type]
    group_info = self.group_info
    first_id = group_info["loaded_first_id"]
    last_id = group_info["loaded_last_id"]

    # Seed the cursor with the newest message on the first run.  The old
    # implementation fetched two messages and could lose the entire history
    # when no newer page was returned.
    if last_id is None:
      latest = await timed_get_messages(client, self.entity, limit=1)
      if not latest:
        callback()
        return
      latest = list(latest)
      first_id = last_id = latest[-1].id
      await dbstore.insert_messages(
        latest, UpdateLoaded.update_both, use_ocr=self.use_ocr
      )

    while True:
      msgs = await timed_get_messages(
        client, self.entity, limit=50, reverse=True, min_id=last_id
      )
      if not msgs:
        break
      msgs = list(msgs)
      await dbstore.insert_messages(
        msgs, UpdateLoaded.update_last, use_ocr=self.use_ocr
      )
      last_id = msgs[-1].id

    logger.info("forward history index done for group %s", group_info["name"])
    callback()
    if first_id == 1:
      return

    while True:
      msgs = await timed_get_messages(client, self.entity, limit=50, max_id=first_id)
      if not msgs:
        break
      msgs = list(reversed(msgs))
      first_id = msgs[0].id
      await dbstore.insert_messages(
        msgs, UpdateLoaded.update_first, use_ocr=self.use_ocr
      )
      if first_id == 1:
        break


class PrivateHistoryIndexer:
  def __init__(self, entity, use_ocr):
    self.entity = entity
    self.use_ocr = use_ocr

  async def run(self, client, dbstore):
    msg_source.set("private-history")  # type: ignore[arg-type]
    batch = []
    async for msg in client.iter_messages(self.entity, reverse=True):
      batch.append(msg)
      if len(batch) == 50:
        await dbstore.insert_messages(
          batch, UpdateLoaded.update_none, use_ocr=self.use_ocr
        )
        batch = []
    if batch:
      await dbstore.insert_messages(
        batch, UpdateLoaded.update_none, use_ocr=self.use_ocr
      )
    logger.info("private history index done for %s", getattr(self.entity, "id", None))


class MonitoringUnavailable(RuntimeError):
  """The local indexer cannot currently accept Telegram requests."""


@dataclass
class _Group:
  conversation_id: Any
  phase: str = "starting"
  error_type: str | None = None
  retry_at: float = 0
  worker: asyncio.Task | None = None
  callbacks: list = field(default_factory=list)
  event_tasks: set = field(default_factory=set)


class GroupMonitor:
  """Own one collection worker per group selected by durable references."""

  POLL_INTERVAL = 2.0
  RESOLVE_TIMEOUT = 30.0
  RETRY_SECONDS = 30.0

  def __init__(self, indexer):
    self.indexer = indexer
    self.groups = {}
    self._primed = {}
    self._lock = asyncio.Lock()
    self._resolvers = asyncio.Semaphore(4)
    self._closed = asyncio.Event()

  def prime(self, info, entity):
    cid = info["conversation_uuid"]
    if cid not in self.groups:
      self._primed[cid] = (entity, info)

  def statuses(self):
    return {
      str(cid): {"state": g.phase, "error_type": g.error_type}
      for cid, g in self.groups.items()
    }

  async def reconcile(self):
    async with self._lock:
      if self._closed.is_set():
        return
      rows = await self.indexer.dbstore.list_group_monitoring()
      wanted = {r["id"]: r for r in rows if r["reference_count"] > 0}
      for cid in list(self.groups):
        if cid not in wanted:
          group = self.groups.pop(cid)
          await self._stop(group)
      for cid in list(self._primed):
        if cid not in wanted:
          del self._primed[cid]
      now = asyncio.get_running_loop().time()
      for cid, row in wanted.items():
        group = self.groups.get(cid)
        if group is not None:
          if group.phase in ("starting", "running"):
            continue
          if group.phase == "retrying" and now < group.retry_at:
            continue
          await self._stop(group)
        group = _Group(cid)
        self.groups[cid] = group
        group.worker = asyncio.create_task(
          self._serve(group, row, self._primed.pop(cid, None)),
          name=f"monitor-group-{cid}",
        )

  async def _entity(self, row):
    client = self.indexer.client
    peer_cls = (
      types.PeerChat if row["telegram_peer_type"] == "chat" else types.PeerChannel
    )
    peer = peer_cls(row["telegram_peer_id"])
    try:
      return await client.get_entity(peer)
    except ValueError:
      # A restored session may lack an access-hash cache entry.
      dialogs = await client.get_dialogs()
    # Keep chat/channel identity separate; numeric IDs alone are ambiguous.
    expected = types.Chat if isinstance(peer, types.PeerChat) else types.Channel
    for dialog in dialogs:
      entity = dialog.entity
      if isinstance(entity, expected) and entity.id == row["telegram_peer_id"]:
        return entity
    raise ValueError("monitored group is unavailable")

  def _callback(self, group, handler):
    async def receive(event):
      if group.phase != "running":
        return
      task = asyncio.current_task()
      group.event_tasks.add(task)
      try:
        await handler(event)
      finally:
        group.event_tasks.discard(task)

    return receive

  async def _serve(self, group, row, primed):
    indexer = self.indexer
    client = indexer.client
    peer_id = row["telegram_peer_id"]
    try:
      async with self._resolvers:
        async with asyncio.timeout(self.RESOLVE_TIMEOUT):
          if primed is None:
            entity = await self._entity(row)
            expected = (
              types.Chat if row["telegram_peer_type"] == "chat" else types.Channel
            )
            if not isinstance(entity, expected) or entity.id != peer_id:
              raise ValueError("Telegram peer does not match the archived group")
            info = await indexer.init_group(entity)
          else:
            entity, info = primed
          # Initialization can repair fake topics and cascade their grants.
          # A removed last reference must not start even a transient history job.
          desired = await indexer.dbstore.list_group_monitoring(group.conversation_id)
          if not desired or not desired[0]["reference_count"]:
            return
      indexer.group_forward_history_done[peer_id] = False
      for handler, event_type in (
        (indexer.on_message, events.NewMessage),
        (indexer.on_message, events.MessageEdited),
        (indexer.on_deleted, events.MessageDeleted),
      ):
        callback = self._callback(group, handler)
        builder = event_type(chats=[entity])
        client.add_event_handler(callback, builder)
        group.callbacks.append((callback, builder))
      group.phase = "running"
      logger.info("monitoring group %s (%s)", peer_id, group.conversation_id)

      def forward_done():
        if group.phase == "running":
          indexer.group_forward_history_done[peer_id] = True

      await GroupHistoryIndexer(
        entity,
        info,
        peer_id not in indexer.ocr_ignore_group_ids,
      ).run(client, indexer.dbstore, forward_done)
      # Finishing history must not remove the live-update subscriptions.
      await self._closed.wait()
    except asyncio.CancelledError:
      raise
    except Exception as exc:
      group.phase = "retrying"
      group.error_type = type(exc).__name__
      group.retry_at = asyncio.get_running_loop().time() + self.RETRY_SECONDS
      logger.exception("monitoring group %s failed; will retry", peer_id)
    finally:
      if group.phase != "retrying":
        group.phase = "stopped"
      for callback, builder in group.callbacks:
        client.remove_event_handler(callback, builder)
      group.callbacks.clear()
      tasks = [t for t in group.event_tasks if t is not asyncio.current_task()]
      for task in tasks:
        task.cancel()
      if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
      indexer.group_forward_history_done.pop(peer_id, None)

  async def _stop(self, group):
    group.phase = "stopping"
    if group.worker is not None:
      group.worker.cancel()
      await asyncio.gather(group.worker, return_exceptions=True)
    logger.info("stopped monitoring %s; archive retained", group.conversation_id)

  async def run(self):
    while not self._closed.is_set():
      try:
        await self.reconcile()
      except Exception:
        # Keep current workers on a transient DB failure. Do not mistake a
        # failed lookup for an empty desired set and stop everyone's indexing.
        logger.exception("cannot refresh monitoring references; retrying")
      try:
        await asyncio.wait_for(self._closed.wait(), self.POLL_INTERVAL)
      except TimeoutError:
        continue

  async def close(self):
    async with self._lock:
      self._closed.set()
      groups, self.groups = list(self.groups.values()), {}
      for group in groups:
        await self._stop(group)
      self._primed.clear()
