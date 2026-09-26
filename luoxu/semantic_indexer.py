"""Run the optional resumable embedding worker: python -m luoxu.semantic_indexer."""

import argparse
import asyncio
import logging

from .db import PostgreStore
from .semantic import SemanticUnavailable, index_batch  # type: ignore[import-not-found]
from .util import load_config

logger = logging.getLogger(__name__)


async def run(config, *, once=False):
  settings = config["database"].get("semantic", {})
  if not settings.get("enabled", False):
    raise ValueError("enable database.semantic before starting the embedding worker")
  batch_size = settings.get("batch_size", 16)
  interval = settings.get("poll_interval", 10)
  if type(batch_size) is not int or not 1 <= batch_size <= 32:
    raise ValueError("semantic batch_size must be an integer from 1 to 32")
  if type(interval) is not int or not 1 <= interval <= 3600:
    raise ValueError("semantic poll_interval must be an integer from 1 to 3600")
  db = PostgreStore(config["database"])
  await db.setup()
  try:
    async with db.get_conn() as conn:
      # Fail fast for missing migration rather than running a silently broken loop.
      await conn.execute("SELECT 1 FROM message_embedding_template LIMIT 0")
    if db.embedder is None:
      raise ValueError("semantic embedding service is not configured")
    while True:
      try:
        count = 0
        # Round-robin one batch per archive: a large group cannot starve others.
        for archive in await db.list_archives():
          count += await index_batch(db, db.embedder, batch_size, archive_id=archive["id"])
      except SemanticUnavailable:
        if once:
          raise
        # No text/query is logged. Retry the same durable missing rows later.
        logger.warning("Embedding service unavailable; retrying in %ss", interval)
        await asyncio.sleep(interval)
        continue
      if count:
        logger.info("Processed %d messages for semantic indexing", count)
        # Let interactive queries contend with backfill for the CPU service.
        await asyncio.sleep(0.1)
      elif once:
        return
      else:
        await asyncio.sleep(interval)
  finally:
    await db.close()


if __name__ == "__main__":
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--config", default="config.toml")
  parser.add_argument("--once", action="store_true", help="backfill until caught up, then exit")
  args = parser.parse_args()
  logging.basicConfig(level=logging.INFO)
  try:
    asyncio.run(run(load_config(args.config), once=args.once))
  except KeyboardInterrupt:
    pass
