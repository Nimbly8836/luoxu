"""Explicit, resumable maintenance of existing archived reply headers only.

This is not an indexer. The operator must stop any core sharing this Telegram
session before connecting. See docs/reply-backfill.md before applying changes.
"""

import argparse
import asyncio
import inspect
import json
import logging
import math
import os
import stat
import tempfile
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path

from telethon.errors import FloodWaitError
from telethon.tl import types

from . import util
from .db import PostgreStore

logger = logging.getLogger(__name__)
RPC_TIMEOUT = 60
FLOOD_RETRIES = 3
MAX_MESSAGE_ID = 2**31 - 1
STATE_KEYS = {
  "version",
  "conversation_id",
  "telegram_peer_type",
  "telegram_peer_id",
  "account_id",
  "after_id",
  "through_id",
  "last_id",
  "complete",
}


class BackfillError(ValueError):
  """A safe-to-display maintenance scope or protocol error."""


def _integer(value, name, minimum=0, maximum=None):
  if type(value) is not int or value < minimum:
    raise BackfillError(f"{name} must be an integer >= {minimum}")
  if maximum is not None and value > maximum:
    raise BackfillError(f"{name} must be <= {maximum}")
  return value


def _validate_options(
  apply, batch_size, delay, after_id, through_id, max_batches, max_flood_wait
):
  if type(apply) is not bool:
    raise BackfillError("apply must be a boolean")
  _integer(batch_size, "batch_size", 1, 100)
  if (
    type(delay) not in (int, float) or not 0 < delay <= 3600 or not math.isfinite(delay)
  ):
    raise BackfillError("delay must be finite and in (0, 3600] seconds")
  _integer(after_id, "after_id", 0, MAX_MESSAGE_ID)
  if through_id is not None:
    _integer(through_id, "through_id", after_id, MAX_MESSAGE_ID)
  if max_batches is not None:
    _integer(max_batches, "max_batches", 1)
  _integer(max_flood_wait, "max_flood_wait", 0, 86400)


@contextmanager
def _state_lock(path):
  try:
    import fcntl
  except ImportError:
    raise BackfillError("checkpoint locking requires Linux/macOS fcntl") from None
  # Lock a stable sibling inode, never the checkpoint inode replaced by save.
  # Keep this file after exit: unlinking it can let two processes lock different
  # inodes under the same name. The parent directory must be operator-controlled.
  fd = os.open(
    str(path) + ".lock",
    os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
    0o600,
  )
  try:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
      raise BackfillError("checkpoint lock must be a regular file")
    os.fchmod(fd, 0o600)
    try:
      fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      raise BackfillError("checkpoint is already locked by another job") from None
    try:
      yield
    finally:
      fcntl.flock(fd, fcntl.LOCK_UN)
  finally:
    os.close(fd)


def _unique_object(pairs):
  result = {}
  for key, value in pairs:
    if key in result:
      raise BackfillError("checkpoint contains duplicate JSON keys")
    result[key] = value
  return result


def _read_state(path):
  try:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
  except FileNotFoundError:
    return None
  with os.fdopen(fd, "r", encoding="utf-8") as stream:
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
      raise BackfillError("checkpoint must be a regular file")
    try:
      # Refuse oversized/untrusted checkpoints rather than allocating without a bound.
      text = stream.read(16385)
      if len(text) > 16384:
        raise BackfillError("checkpoint is too large")
      state = json.loads(text, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError):
      raise BackfillError("malformed JSON checkpoint; it was not overwritten") from None
  if not isinstance(state, dict) or set(state) != STATE_KEYS:
    raise BackfillError("checkpoint fields do not match version 1")
  _integer(state["version"], "checkpoint version", 1, 1)
  for key in ("telegram_peer_id", "account_id"):
    _integer(state[key], "checkpoint " + key, 1)
  for key in ("after_id", "through_id", "last_id"):
    _integer(state[key], "checkpoint " + key, 0, MAX_MESSAGE_ID)
  if not state["after_id"] <= state["last_id"] <= state["through_id"]:
    raise BackfillError("inconsistent checkpoint range/cursor")
  if type(state["complete"]) is not bool:
    raise BackfillError("checkpoint complete must be a boolean")
  if state["telegram_peer_type"] not in ("chat", "channel"):
    raise BackfillError("checkpoint has an unsupported peer type")
  try:
    canonical = str(uuid.UUID(state["conversation_id"]))
  except (TypeError, ValueError, AttributeError):
    raise BackfillError("checkpoint conversation_id must be a UUID") from None
  if canonical != state["conversation_id"]:
    raise BackfillError("checkpoint conversation UUID must be canonical")
  return state


def _save_state(path, state):
  fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
      os.fchmod(stream.fileno(), 0o600)
      json.dump(state, stream, sort_keys=True)
      stream.write("\n")
      stream.flush()
      os.fsync(stream.fileno())
    os.replace(name, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
      os.fsync(directory)
    finally:
      os.close(directory)
  finally:
    with suppress(FileNotFoundError):
      os.unlink(name)


async def _rpc(call, max_flood_wait):
  for attempt in range(FLOOD_RETRIES + 1):
    try:
      return await asyncio.wait_for(call(), timeout=RPC_TIMEOUT)
    except FloodWaitError as exc:
      seconds = exc.seconds
      if (
        type(seconds) is not int
        or seconds < 0
        or seconds > max_flood_wait
        or attempt == FLOOD_RETRIES
      ):
        raise BackfillError(
          "FloodWait exceeded the wait/retry limit; cursor retained"
        ) from None
      # Even a zero-second flood must not spin. Never sleep beyond the cap.
      wait = max(1, seconds)
      if wait > max_flood_wait:
        raise BackfillError(
          "FloodWait exceeds configured wait cap; cursor retained"
        ) from None
      logger.warning(
        "Telegram FloodWait: waiting %d seconds (retry %d/%d)",
        wait,
        attempt + 1,
        FLOOD_RETRIES,
      )
      await asyncio.sleep(wait)


def _peer_matches(peer, peer_type, peer_id):
  cls, attr = (
    (types.PeerChat, "chat_id")
    if peer_type == "chat"
    else (types.PeerChannel, "channel_id")
  )
  return (
    isinstance(peer, cls)
    and type(getattr(peer, attr, None)) is int
    and getattr(peer, attr) == peer_id
  )


def _checked_messages(response, requested, peer_type, peer_id):
  if not isinstance(response, (list, tuple)) or len(response) > len(requested):
    raise BackfillError("unexpected Telegram batch response")
  seen = set()
  messages = []
  for message in response:
    if message is None:
      continue
    if not isinstance(message, (types.Message, types.MessageEmpty)):
      raise BackfillError("unexpected Telegram message type; batch was not changed")
    msgid = _integer(message.id, "Telegram message ID", 1, MAX_MESSAGE_ID)
    if msgid not in requested or msgid in seen:
      raise BackfillError(
        "unexpected or duplicate Telegram message ID; batch was not changed"
      )
    seen.add(msgid)
    peer = getattr(message, "peer_id", None)
    if isinstance(message, types.MessageEmpty):
      if peer is not None and not _peer_matches(peer, peer_type, peer_id):
        raise BackfillError("foreign-peer Telegram response; batch was not changed")
      continue
    if not _peer_matches(peer, peer_type, peer_id):
      raise BackfillError("foreign-peer Telegram response; batch was not changed")
    messages.append(message)
  return messages, len(requested) - len(messages)


async def run_backfill(
  db,
  client,
  conversation_id,
  state_path,
  *,
  apply=False,
  batch_size=100,
  delay=1.0,
  after_id=0,
  through_id=None,
  max_batches=None,
  max_flood_wait=300,
):
  """Scan an exact archived group range; preview one batch unless apply=True.

  `filled` counts physical database rows (eligible rows for a preview), whereas
  `scanned`/`unavailable` count requested message IDs in this invocation only.
  `complete` means exhausted local candidates, not Telegram thread completeness.
  DB methods own all row validation and transactional null-only updates.
  """
  _validate_options(
    apply, batch_size, delay, after_id, through_id, max_batches, max_flood_wait
  )
  try:
    cid = uuid.UUID(str(conversation_id))
  except (TypeError, ValueError, AttributeError):
    raise BackfillError("conversation_id must be a UUID") from None
  raw_path = Path(state_path).expanduser()
  path = raw_path.parent.resolve(strict=True) / raw_path.name
  with _state_lock(path):
    state = _read_state(path)
    row = await db.get_conversation(cid)
    if row is None or row["kind"] != "group":
      raise BackfillError(
        "target must be one existing group conversation, not a topic/private chat"
      )
    peer_type, peer_id = row["telegram_peer_type"], row["telegram_peer_id"]
    if peer_type not in ("chat", "channel"):
      raise BackfillError("group must have a chat/channel peer")
    _integer(peer_id, "group peer ID", 1)
    # Fail stale/mis-scoped checkpoints before making Telegram requests.
    scope = {
      "conversation_id": str(cid),
      "telegram_peer_type": peer_type,
      "telegram_peer_id": peer_id,
      "after_id": after_id,
    }
    if state is not None:
      if any(state[key] != value for key, value in scope.items()):
        raise BackfillError(
          "checkpoint scope/initial after_id mismatch; use the original scope"
        )
      if through_id is not None and through_id != state["through_id"]:
        raise BackfillError(
          "checkpoint through_id mismatch; use a new state file for a new range"
        )
      upper = state["through_id"]
    else:
      upper = (
        await db.reply_backfill_upper_bound(cid) if through_id is None else through_id
      )
      _integer(upper, "through_id", after_id, MAX_MESSAGE_ID)
    # Applies to injected connected clients as well as the CLI-created client:
    # Telethon must not sleep internally past the operator's FloodWait cap.
    client.flood_sleep_threshold = 0
    account = await _rpc(client.get_me, max_flood_wait)
    if not isinstance(account, types.User):
      raise BackfillError("Telegram account is not authorized")
    account_id = _integer(account.id, "Telegram account ID", 1)
    if state is not None and state["account_id"] != account_id:
      raise BackfillError("checkpoint Telegram account mismatch")
    peer = (
      types.PeerChat(peer_id) if peer_type == "chat" else types.PeerChannel(peer_id)
    )
    entity = await _rpc(lambda: client.get_entity(peer), max_flood_wait)
    expected_type = types.Chat if peer_type == "chat" else types.Channel
    if (
      not isinstance(entity, expected_type)
      or type(entity.id) is not int
      or entity.id != peer_id
    ):
      raise BackfillError("resolved Telegram entity does not match the archived peer")
    if state is None:
      state = {
        "version": 1,
        **scope,
        "account_id": account_id,
        "through_id": upper,
        "last_id": after_id,
        "complete": after_id == upper,
      }
      if apply:
        # Save the fixed scope before the first request, without advancing cursor.
        _save_state(path, state)
    summary = {
      "scanned": 0,
      "filled": 0,
      "unavailable": 0,
      "last_id": state["last_id"],
      "through_id": upper,
      "complete": state["complete"],
      "dry_run": not apply,
    }
    batches = 0
    while not summary["complete"]:
      ids = await db.reply_backfill_candidates(
        cid, summary["last_id"], upper, batch_size
      )
      if (
        not isinstance(ids, list)
        or len(ids) > batch_size
        or any(type(i) is not int or not summary["last_id"] < i <= upper for i in ids)
        or ids != sorted(set(ids))
      ):
        raise BackfillError("invalid candidate page; cursor retained")
      if not ids:
        summary["complete"] = True
        if apply:
          state["complete"] = True
          _save_state(path, state)
        break
      response = await _rpc(
        lambda ids=ids: client.get_messages(entity, ids=ids), max_flood_wait
      )
      messages, unavailable = _checked_messages(response, set(ids), peer_type, peer_id)
      filled = await db.backfill_reply_ids(cid, messages, dry_run=not apply)
      _integer(filled, "database filled count", 0)
      # The database's transactional method has returned (commit completed).
      # Cancellation/crash before this synchronous save leaves an idempotent replay.
      summary.update(
        scanned=summary["scanned"] + len(ids),
        filled=summary["filled"] + filled,
        unavailable=summary["unavailable"] + unavailable,
        last_id=ids[-1],
        complete=ids[-1] == upper or len(ids) < batch_size,
      )
      if apply:
        state.update(last_id=summary["last_id"], complete=summary["complete"])
        _save_state(path, state)
      batches += 1
      logger.info(
        "reply backfill batch=%d scanned=%d filled=%d unavailable=%d last_id=%d through_id=%d dry_run=%s",
        batches,
        summary["scanned"],
        summary["filled"],
        summary["unavailable"],
        summary["last_id"],
        upper,
        not apply,
      )
      if (
        not apply
        or summary["complete"]
        or (max_batches is not None and batches >= max_batches)
      ):
        break
      await asyncio.sleep(delay)
    return summary


def _parser():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--config", required=True)
  parser.add_argument("--conversation-id", required=True, type=uuid.UUID)
  parser.add_argument("--state-file", required=True)
  parser.add_argument(
    "--apply", action="store_true", help="write reply IDs; default previews one batch"
  )
  parser.add_argument("--batch-size", type=int, default=100)
  parser.add_argument("--delay", type=float, default=1.0)
  parser.add_argument("--after-id", type=int, default=0)
  parser.add_argument("--through-id", type=int)
  parser.add_argument("--max-batches", type=int)
  parser.add_argument("--max-flood-wait", type=int, default=300)
  return parser


async def _main(args):
  _validate_options(
    args.apply,
    args.batch_size,
    args.delay,
    args.after_id,
    args.through_id,
    args.max_batches,
    args.max_flood_wait,
  )
  config = util.load_config(args.config)
  database = config["database"]
  # Do not construct an OCR client/session or run bootstrap/indexer/migrations.
  db = PostgreStore(
    {"url": database["url"], "first_year": database.get("first_year", 2016)}
  )
  client = None
  try:
    client = util.create_client(config["telegram"])
    # Let our explicit, bounded FloodWait handler own waiting; no implicit sleeps.
    client.flood_sleep_threshold = 0
    await asyncio.wait_for(client.connect(), timeout=RPC_TIMEOUT)
    authorized = await _rpc(client.is_user_authorized, args.max_flood_wait)
    if not authorized:
      raise BackfillError(
        "Telegram session is not authorized; authenticate separately, never in this tool"
      )
    await db.setup()
    summary = await run_backfill(
      db,
      client,
      args.conversation_id,
      args.state_file,
      apply=args.apply,
      batch_size=args.batch_size,
      delay=args.delay,
      after_id=args.after_id,
      through_id=args.through_id,
      max_batches=args.max_batches,
      max_flood_wait=args.max_flood_wait,
    )
    print(json.dumps(summary, sort_keys=True))
  finally:
    try:
      await db.close()
    finally:
      if client is not None:
        disconnected = client.disconnect()
        if inspect.isawaitable(disconnected):
          await disconnected


def main():
  args = _parser().parse_args()
  logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
  logger.setLevel(logging.INFO)
  logger.warning(
    "OFFLINE MAINTENANCE: stop core before sharing telegram.session_db; this tool stops nothing. Separate web may remain running."
  )
  try:
    asyncio.run(_main(args))
  except (KeyboardInterrupt, asyncio.CancelledError):
    logger.error("maintenance cancelled; resume with the same scope/checkpoint")
    return 130
  except Exception as exc:  # noqa: BLE001 -- CLI boundary must redact all failures.
    # Third-party exception text/tracebacks can contain credentials or message data.
    if isinstance(exc, BackfillError):
      logger.error("%s", exc)
    else:
      logger.error(
        "maintenance failed (%s); checkpoint retained, verify configuration/permissions/connectivity before retrying",
        type(exc).__name__,
      )
    return 1
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
