import asyncio
import logging

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
