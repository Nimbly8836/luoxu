import asyncio
import contextlib
import datetime
import json
import logging
import uuid
from typing import Any, Literal

import asyncpg  # type: ignore[import-not-found]
from telethon.tl import types

from .auth import Principal  # type: ignore[import-not-found]
from .ctxvars import group_title, msg_source
from .indexing import format_msg, text_to_query
from .mediamgr import MediaMgr
from .ocr import OCRService
from .semantic import EmbeddingClient, MAX_QUERY_CHARS, SemanticUnavailable, search_vectors
from .rerank import RerankerClient, valid_score
from .types import GroupNotFound, SearchQuery
from .storage import ArchiveConnection, conversation_archive
from .util import UpdateLoaded, format_name

logger = logging.getLogger(__name__)


class PostgreStore:
  SEARCH_LIMIT = 50

  def __init__(
    self,
    config: dict[str, Any],
    client=None,
    *,
    history_enabled=False,
    repair_non_forum_groups=(),
  ) -> None:
    self.address = config["url"]
    first_year = config.get("first_year", 2016)
    self.mediamgr = MediaMgr(client)
    if ocr_url := config.get("ocr_url"):
      self.ocrsvc = OCRService(self.mediamgr, ocr_url, config.get("ocr_socket"))
    else:
      self.ocrsvc = None
    self.earliest_time = datetime.datetime(
      first_year,
      1,
      1,
      tzinfo=datetime.timezone.utc,
    )
    self.history_enabled = bool(history_enabled)
    if not isinstance(repair_non_forum_groups, (list, tuple)) or any(
      type(g) is not int or not 0 < g < 2**63 for g in repair_non_forum_groups
    ):
      raise ValueError(
        "telegram.repair_non_forum_groups must be a list of positive 64-bit integer IDs"
      )
    self.repair_non_forum_groups = frozenset(repair_non_forum_groups)
    self.pool = None
    semantic_config = config.get("semantic", {})
    if not isinstance(semantic_config, dict) or type(semantic_config.get("enabled", False)) is not bool:
      raise ValueError("database.semantic.enabled must be boolean")
    reranker = RerankerClient(semantic_config.get("rerank", {}))
    self.reranker = reranker if reranker.enabled else None
    self.embedder = (
      EmbeddingClient(semantic_config.get("endpoint", "http://embeddings:8080/embed"))
      if semantic_config.get("enabled", False) else None
    )

  async def setup(self) -> None:
    self.pool = await asyncpg.create_pool(self.address)
    try:
      ready = await self.pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM bootstrap_state WHERE name='per-peer-storage-v1')"
      )
      if not ready:
        raise RuntimeError("database storage migration 006_per_group_storage.sql is required")
    except Exception:
      await self.pool.close()
      self.pool = None
      raise

  async def close(self) -> None:
    if self.reranker is not None:
      await self.reranker.close()
    if self.embedder is not None:
      await self.embedder.close()
    if self.pool:
      await self.pool.close()

  async def bootstrap(
    self, auth_config: dict[str, Any], public_groups=None, auth_service=None
  ) -> None:
    if not auth_service:
      return
    async with self.get_conn() as conn:
      admin = auth_config.get("bootstrap_admin") or {}
      username = admin.get("username")
      password_hash = admin.get("password_hash")
      if username and password_hash:
        existing = await conn.fetchrow(
          "SELECT id, is_admin FROM auth_users WHERE username = $1",
          username,
        )
        if existing:
          if not existing["is_admin"]:
            raise RuntimeError(
              "bootstrap administrator username belongs to a non-administrator"
            )
          await conn.execute(
            """
            INSERT INTO bootstrap_state (name) VALUES ($1)
            ON CONFLICT (name) DO NOTHING
            """,
            "admin_bootstrap",
          )
        else:
          await conn.fetchval(
            """
            INSERT INTO auth_users (username, password_hash, is_admin)
            VALUES ($1, $2, true)
            RETURNING id
            """,
            username,
            password_hash,
          )
          await conn.execute(
            """
            INSERT INTO bootstrap_state (name) VALUES ($1)
            ON CONFLICT (name) DO NOTHING
            """,
            "admin_bootstrap",
          )
      if public_groups is None:
        return
      public_groups = tuple(public_groups)
      public_rows = []
      for group_id in public_groups:
        try:
          group_id = int(group_id)
        except (TypeError, ValueError) as exc:
          raise ValueError(f"invalid public group id: {group_id!r}") from exc
        row = await conn.fetchrow(
          """
          SELECT conversation_id FROM tg_groups WHERE group_id = $1
        """,
          group_id,
        )
        if not row:
          return
        public_rows.append(row["conversation_id"])
      if len(public_rows) == len(public_groups):
        seeded = await conn.fetchval(
          """
          INSERT INTO bootstrap_state (name) VALUES ('public_groups')
          ON CONFLICT (name) DO NOTHING RETURNING name
          """
        )
        if seeded:
          await conn.executemany(
            """
            INSERT INTO public_conversation_access (conversation_id)
            VALUES ($1) ON CONFLICT DO NOTHING
            """,
            [(conversation_id,) for conversation_id in public_rows],
          )

  @contextlib.asynccontextmanager
  async def get_conn(self):
    if self.pool is None:
      raise RuntimeError("database pool is not initialized")
    for attempt in range(5):
      try:
        async with self.pool.acquire() as conn, conn.transaction():
          yield conn
        break
      except FileNotFoundError:
        if attempt < 4:
          logger.error("database connection failed, retrying")
          await asyncio.sleep(1)
        else:
          raise

  async def _get_conversation(
    self, conn, kind: str, peer_type: str, peer_id: int, topic_id: int | None = None
  ):
    return await conn.fetchrow(
      """
      SELECT * FROM conversations
      WHERE kind = $1 AND telegram_peer_type = $2 AND telegram_peer_id = $3
        AND coalesce(topic_id, 0) = coalesce($4, 0)
    """,
      kind,
      peer_type,
      peer_id,
      topic_id,
    )

  async def _ensure_conversation(
    self,
    conn,
    kind: str,
    peer_type: str,
    peer_id: int,
    name: str,
    pub_id: str | None = None,
    topic_id: int | None = None,
    legacy_group_id: int | None = None,
  ):
    row = await self._get_conversation(conn, kind, peer_type, peer_id, topic_id)
    if row:
      if name and row["name"] != name:
        # Literal SQL; both name and UUID are separately bound as $1/$2.
        # pi-lens-ignore: python-sql-injection
        await conn.execute(
          "UPDATE conversations SET name = $1 WHERE id = $2",
          name,
          row["id"],
        )
      return row
    archive_id = await conn.fetchval("SELECT ensure_message_archive($1, $2)", peer_type, peer_id)
    inserted = await conn.fetchrow(
      """
      INSERT INTO conversations
        (kind, telegram_peer_type, telegram_peer_id, topic_id, name, pub_id,
         legacy_group_id, archive_id)
      VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
      ON CONFLICT DO NOTHING RETURNING *
      """,
      kind,
      peer_type,
      peer_id,
      topic_id,
      name,
      pub_id,
      legacy_group_id,
      archive_id,
    )
    return inserted or await self._get_conversation(
      conn, kind, peer_type, peer_id, topic_id
    )

  async def archive_conn(self, conn, conversation_id):
    archive_id = await conversation_archive(conn, conversation_id)
    if archive_id is None:
      raise GroupNotFound(conversation_id)
    return ArchiveConnection(conn, archive_id)

  async def list_archives(self):
    async with self.get_conn() as conn:
      return await conn.fetch("SELECT id FROM message_archives ORDER BY id")

  async def get_group(self, conn, group_id: int):
    return await conn.fetchrow(
      """
      SELECT g.*, c.id AS conversation_uuid, c.kind, c.topic_id, c.archive_id
      FROM tg_groups g JOIN conversations c ON c.id = g.conversation_id
      WHERE g.group_id = $1
    """,
      group_id,
    )

  @staticmethod
  async def _lock_peer_writes(conn, peer_id, *, exclusive=False):
    # Repair relocates rows, so writers must wait BEFORE their lookup snapshot.
    # Transaction-scoped locks also coordinate separate indexer processes.
    # Use the numeric peer ID so deletion events without a peer type cooperate.
    if exclusive:
      sql = "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))"
    else:
      sql = "SELECT pg_advisory_xact_lock_shared(hashtextextended($1, 0))"
    await conn.fetchval(sql, f"luoxu:peer-write:{peer_id}")

  async def insert_group(self, conn, group):
    await self._lock_peer_writes(
      conn,
      group.id,
      exclusive=group.id in self.repair_non_forum_groups,
    )
    existing = await self.get_group(conn, group.id)
    if existing:
      await self._repair_non_forum_topics(conn, group, existing["conversation_uuid"])
      return existing
    peer_type = "chat" if type(group).__name__ == "Chat" else "channel"
    conversation = await self._ensure_conversation(
      conn,
      "group",
      peer_type,
      group.id,
      group.title,
      getattr(group, "username", None),
      legacy_group_id=group.id,
    )
    info = await conn.fetchrow(
      """
      INSERT INTO tg_groups (group_id, name, pub_id, conversation_id)
      VALUES ($1, $2, $3, $4)
      ON CONFLICT (group_id) DO UPDATE SET name = EXCLUDED.name
      RETURNING *, conversation_id AS conversation_uuid
    """,
      group.id,
      group.title,
      getattr(group, "username", None),
      conversation["id"],
    )
    await self._repair_non_forum_topics(conn, group, info["conversation_uuid"])
    return info

  async def _repair_non_forum_topics(self, conn, group, parent_id):
    """Undo legacy reply-thread conversations using a full Telegram entity.

    Called during group initialization in the caller's transaction, not by Web
    reads. Missing/minimal metadata is not proof that a group is non-forum.
    The operator must also confirm the peer: a forum might have been disabled
    after genuine topic messages were archived.
    """
    if group.id not in self.repair_non_forum_groups:
      return
    if isinstance(group, types.Channel):
      if group.min or group.forum or getattr(group, "monoforum", False):
        return
      peer_type = "channel"
    elif isinstance(group, types.Chat):
      peer_type = "chat"
    else:
      return
    topics = await conn.fetch(
      """
      SELECT c.id FROM conversations c
      JOIN conversations parent ON parent.id = $1 AND parent.kind = 'group'
        AND parent.telegram_peer_type = c.telegram_peer_type
        AND parent.telegram_peer_id = c.telegram_peer_id
      WHERE c.kind = 'topic' AND c.telegram_peer_type = $2
        AND c.telegram_peer_id = $3
      ORDER BY c.id FOR UPDATE OF c
      """,
      parent_id,
      peer_type,
      group.id,
    )
    if not topics:
      return
    topic_ids = [row["id"] for row in topics]
    grant_count = await conn.fetchval(
      """
      SELECT (SELECT count(*) FROM conversation_access
              WHERE conversation_id = ANY($1::uuid[]))
           + (SELECT count(*) FROM public_conversation_access
              WHERE conversation_id = ANY($1::uuid[]))
      """,
      topic_ids,
    )
    for topic_id in topic_ids:
      await self._merge_topic_messages(conn, parent_id, topic_id, group.id)
    # Preserve previously captured revisions even if collection is now disabled.
    # Literal SQL; parent ID and topic IDs are separately bound as $1/$2.
    # pi-lens-ignore: python-sql-injection
    await conn.execute(
      """
      UPDATE message_revisions SET conversation_id = $1, topic_id = NULL
      WHERE conversation_id = ANY($2::uuid[])
      """,
      parent_id,
      topic_ids,
    )
    # FK cascades remove grants on invalid IDs. Never promote them to group
    # grants: that would silently widen a topic-only user's or pub's access.
    # Literal SQL; the UUID array is separately bound as $1.
    # pi-lens-ignore: python-sql-injection
    await conn.execute(
      "DELETE FROM conversations WHERE id = ANY($1::uuid[])",
      topic_ids,
    )
    logger.warning(
      "Consolidated %d reply-thread conversations into non-forum group %s; "
      "%d obsolete topic grants removed (group grants unchanged)",
      len(topic_ids),
      group.id,
      grant_count,
    )

  async def _merge_topic_messages(self, conn, parent_id, topic_id, group_id):
    conn = await self.archive_conn(conn, parent_id)
    # An older duplicate can contain a parent that the newer body never stored.
    # Recover only that missing field before removing the topic copy; keep the
    # original deletion/content winner rules below and the enclosing repair lock.
    # Fixed SQL; both conversation IDs are separately bound parameters.
    # pi-lens-ignore: python-sql-injection
    await conn.execute(
      """
      UPDATE {messages} AS destination SET reply_to_id = source.reply_to_id
      FROM {messages} AS source
      WHERE destination.conversation_id = $1 AND source.conversation_id = $2
        AND destination.msgid = source.msgid
        AND destination.created_at = source.created_at
        AND destination.reply_to_id IS NULL AND destination.deleted_at IS NULL
        AND source.reply_to_id > 0 AND source.reply_to_id <> source.msgid
      """,
      parent_id,
      topic_id,
    )
    # Move atomically within the peer's ordinary table. Replayed duplicates may
    # already exist in the parent; retain the latest state and never resurrect
    # a deletion. This repair does not create new historical snapshots.
    # Literal CTE; all variable IDs are separately bound as $1/$2/$3.
    # pi-lens-ignore: python-sql-injection
    await conn.execute(
      """
      WITH moved AS (
        DELETE FROM {messages} WHERE conversation_id = $2 RETURNING *
      )
      INSERT INTO {messages} (
        conversation_id, group_id, msgid, reply_to_id, topic_id, quote_text,
        from_user, from_user_name, text, media, created_at, updated_at, deleted_at
      )
      SELECT $1, $3, msgid, reply_to_id, NULL, quote_text,
             from_user, from_user_name,
             CASE WHEN deleted_at IS NULL THEN text ELSE '' END,
             media, created_at, updated_at, deleted_at
      FROM moved ORDER BY created_at, msgid
      ON CONFLICT (conversation_id, msgid, created_at) DO UPDATE SET
        group_id = EXCLUDED.group_id, topic_id = NULL,
        reply_to_id = coalesce(EXCLUDED.reply_to_id, {messages}.reply_to_id),
        quote_text = EXCLUDED.quote_text,
        from_user = EXCLUDED.from_user, from_user_name = EXCLUDED.from_user_name,
        text = EXCLUDED.text, media = EXCLUDED.media,
        updated_at = EXCLUDED.updated_at, deleted_at = EXCLUDED.deleted_at
      WHERE (EXCLUDED.deleted_at IS NOT NULL,
             coalesce(EXCLUDED.deleted_at, EXCLUDED.updated_at, EXCLUDED.created_at))
          > ({messages}.deleted_at IS NOT NULL,
             coalesce({messages}.deleted_at, {messages}.updated_at, {messages}.created_at))
      """,
      parent_id,
      topic_id,
      group_id,
    )

  async def loaded_upto(
    self, conn, group_id: int, direction: Literal[1, -1], msgid: int
  ) -> None:
    if direction == 1:
      sql = """UPDATE tg_groups SET loaded_last_id = $1
               WHERE group_id = $2 AND (loaded_last_id < $1 OR loaded_last_id IS NULL)"""
    elif direction == -1:
      sql = "UPDATE tg_groups SET loaded_first_id = $1 WHERE group_id = $2"
    else:
      raise ValueError(direction)
    # sql is one of the two literal statements above; values use $1/$2.
    # pi-lens-ignore: python-sql-injection
    await conn.execute(sql, msgid, group_id)

  @staticmethod
  def _peer_info(msg):
    peer = msg.peer_id
    if (peer_id := getattr(peer, "channel_id", None)) is not None:
      peer_type = "channel"
      kind = "group"
    elif (peer_id := getattr(peer, "chat_id", None)) is not None:
      peer_type = "chat"
      kind = "group"
    elif (peer_id := getattr(peer, "user_id", None)) is not None:
      peer_type = "user"
      kind = "private_chat"
    else:
      raise ValueError(f"unsupported Telegram peer: {peer!r}")
    reply = getattr(msg, "reply_to", None)
    topic_id = None
    # reply_to_top_id also identifies ordinary reply/comment threads. Only
    # Telegram's explicit forum_topic flag makes the reference a forum topic.
    if peer_type == "channel" and getattr(reply, "forum_topic", False):
      topic_id = getattr(reply, "reply_to_top_id", None) or getattr(
        reply, "reply_to_msg_id", None
      )
      if topic_id:
        kind = "topic"
    return kind, peer_type, peer_id, topic_id

  @staticmethod
  def _reply_to_id(msg):
    reply = getattr(msg, "reply_to", None)
    return getattr(reply, "reply_to_msg_id", None) if reply else None

  @classmethod
  def _local_reply_id(cls, msg) -> int | None:
    reply_id = cls._reply_to_id(msg)
    if type(reply_id) is not int or not 0 < reply_id < 2**63 or reply_id == msg.id:
      return None
    reply_peer = getattr(getattr(msg, "reply_to", None), "reply_to_peer_id", None)
    if reply_peer is not None and reply_peer != msg.peer_id:
      # A foreign-peer quote must not become a same-ID local parent link.
      return None
    return reply_id

  async def _fill_missing_reply_id(self, conn, old, msg, *, dry_run=False) -> bool:
    if (
      old["deleted_at"] is not None
      or old["reply_to_id"] is not None
      or old["created_at"] != msg.date
    ):
      return False
    reply_id = self._local_reply_id(msg)
    if reply_id is None:
      return False
    if dry_run:
      return True
    changed = await conn.fetchval(
      """
      UPDATE {messages} SET reply_to_id = $4
      WHERE conversation_id = $1 AND msgid = $2 AND created_at = $3
        AND reply_to_id IS NULL AND deleted_at IS NULL
      RETURNING true
      """,
      old["conversation_id"],
      old["msgid"],
      old["created_at"],
      reply_id,
    )
    return bool(changed)

  @staticmethod
  def _quote_text(msg):
    reply = getattr(msg, "reply_to", None)
    quote = getattr(msg, "quote_text", None) or getattr(reply, "quote_text", None)
    return str(quote) if quote else None

  @staticmethod
  def _media_metadata(msg):
    media = getattr(msg, "media", None)
    if not media:
      return None
    data = {"type": type(media).__name__}
    for key in ("photo", "document"):
      value = getattr(media, key, None)
      if value is not None and getattr(value, "id", None) is not None:
        data["id"] = value.id
    document = getattr(media, "document", None)
    if document is not None and (mime_type := getattr(document, "mime_type", None)):
      data["mime_type"] = str(mime_type)
    return data

  async def _conversation_for_message(self, conn, msg):
    kind, peer_type, peer_id, topic_id = self._peer_info(msg)
    chat = getattr(msg, "chat", None)
    name = format_name(chat) if kind == "private_chat" else getattr(chat, "title", None)
    if not name:
      name = str(peer_id)
    pub_id = getattr(chat, "username", None)
    legacy_group_id = peer_id if kind in ("group", "topic") else None
    return await self._ensure_conversation(
      conn,
      kind,
      peer_type,
      peer_id,
      name,
      pub_id,
      topic_id,
      legacy_group_id,
    )

  async def _save_revision(self, conn, old, revision_type: str) -> None:
    if not self.history_enabled or not old:
      return
    await conn.execute(
      """
      INSERT INTO message_revisions
        (conversation_id, msgid, revision_type, text, from_user,
         from_user_name, reply_to_id, topic_id, quote_text, media,
         created_at, updated_at, deleted_at)
      VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
    """,
      old["conversation_id"],
      old["msgid"],
      revision_type,
      old["text"],
      old["from_user"],
      old["from_user_name"],
      old["reply_to_id"],
      old["topic_id"],
      old["quote_text"],
      json.dumps(old["media"]) if old["media"] else None,
      old["created_at"],
      old["updated_at"],
      old["deleted_at"],
    )

  async def _insert_one_message(self, conn, msg, text, conversation) -> None:
    conn = ArchiveConnection(conn, conversation["archive_id"])
    sender = await msg.get_sender()
    old = await conn.fetchrow(
      """
      SELECT * FROM {messages}
      WHERE conversation_id = $1 AND msgid = $2
      ORDER BY created_at DESC LIMIT 1 FOR UPDATE
    """,
      conversation["id"],
      msg.id,
    )
    incoming_edit = msg_source.get() == "editmsg"
    if old and old["deleted_at"]:
      return
    # Preserve newer content, but legacy archives may still lack the immutable
    # parent link. Fill only that field from a matching local-peer reply header.
    if old and not incoming_edit and old["updated_at"] is not None:
      await self._fill_missing_reply_id(conn, old, msg)
      return
    if old and incoming_edit:
      if msg.edit_date and old["updated_at"] and msg.edit_date <= old["updated_at"]:
        await self._fill_missing_reply_id(conn, old, msg)
        return
      if self.history_enabled:
        await self._save_revision(conn, old, "edit")
    media = self._media_metadata(msg)
    logger.info(
      "%7s <%s> [%s] %s: %s",
      msg_source.get(),
      getattr(msg.chat, "title", None),
      msg.id,
      format_name(sender),
      text,
    )
    await conn.execute(
      """
      INSERT INTO {messages}
        (conversation_id, group_id, msgid, reply_to_id, topic_id, quote_text,
         from_user, from_user_name, text, media, created_at, updated_at, deleted_at)
      VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NULL)
      ON CONFLICT (conversation_id, msgid, created_at) DO UPDATE SET
        text = EXCLUDED.text, updated_at = EXCLUDED.updated_at,
        reply_to_id = coalesce(EXCLUDED.reply_to_id, {messages}.reply_to_id),
        quote_text = EXCLUDED.quote_text,
        media = EXCLUDED.media, deleted_at = NULL
    """,
      conversation["id"],
      conversation["legacy_group_id"],
      msg.id,
      self._reply_to_id(msg),
      conversation["topic_id"],
      self._quote_text(msg),
      sender.id if sender else msg.sender_id,
      format_name(sender, sender_id=msg.sender_id),
      text,
      json.dumps(media) if media else None,
      msg.date,
      msg.edit_date,
    )

  async def insert_messages(self, msgs, update_loaded, use_ocr=True):
    use_ocr = bool(self.ocrsvc and use_ocr)
    formatted = []
    for msg in msgs:
      group_title.set(getattr(msg.chat, "title", None))
      text = await format_msg(msg, self.ocrsvc if use_ocr else None)
      if text is not None:
        formatted.append((msg, text))
    if not formatted:
      return
    async with self.get_conn() as conn:
      # Serialize writes within each peer (including sender-counter updates).
      # Stable lock order handles multi-peer batches without cross-peer cycles.
      for peer_id in sorted({self._peer_info(msg)[2] for msg, _ in formatted}):
        await self._lock_peer_writes(conn, peer_id, exclusive=True)
      data = [
        (msg, text, await self._conversation_for_message(conn, msg))
        for msg, text in formatted
      ]
      for msg, text, conversation in data:
        await self._insert_one_message(conn, msg, text, conversation)
      if data and update_loaded in (
        UpdateLoaded.update_last,
        UpdateLoaded.update_both,
      ):
        first = data[-1][2]["legacy_group_id"]
        if first:
          await self.loaded_upto(conn, first, 1, formatted[-1][0].id)
      if data and update_loaded in (
        UpdateLoaded.update_first,
        UpdateLoaded.update_both,
      ):
        first = data[0][2]["legacy_group_id"]
        if first:
          await self.loaded_upto(conn, first, -1, formatted[0][0].id)

  async def _reply_backfill_group(self, conn, conversation_id):
    group = await conn.fetchrow(
      """
      SELECT * FROM conversations WHERE id = $1 AND kind = 'group'
        AND telegram_peer_type IN ('chat', 'channel')
      """,
      conversation_id,
    )
    if group is None:
      raise ValueError("reply backfill requires an existing group conversation")
    return group

  async def reply_backfill_upper_bound(self, conversation_id) -> int:
    """Bound an explicit operator-maintenance job; not a content API."""
    async with self.get_conn() as conn:
      await self._reply_backfill_group(conn, conversation_id)
      conn = await self.archive_conn(conn, conversation_id)
      return await conn.fetchval(
        "SELECT coalesce(max(msgid), 0) FROM {messages} WHERE conversation_id = $1",
        conversation_id,
      )

  async def reply_backfill_candidates(
    self, conversation_id, after_id: int, through_id: int, limit: int
  ) -> list[int]:
    if (
      type(after_id) is not int
      or type(through_id) is not int
      or not 0 <= after_id <= through_id < 2**63
      or type(limit) is not int
      or not 1 <= limit <= 100
    ):
      raise ValueError("invalid reply backfill range or batch size")
    async with self.get_conn() as conn:
      await self._reply_backfill_group(conn, conversation_id)
      conn = await self.archive_conn(conn, conversation_id)
      rows = await conn.fetch(
        """
        SELECT msgid FROM (
          SELECT DISTINCT ON (msgid) msgid, reply_to_id, deleted_at
          FROM {messages} WHERE conversation_id = $1 AND msgid > $2 AND msgid <= $3
          ORDER BY msgid, created_at DESC
        ) latest
        WHERE reply_to_id IS NULL AND deleted_at IS NULL
        ORDER BY msgid LIMIT $4
        """,
        conversation_id,
        after_id,
        through_id,
        limit,
      )
      return [row["msgid"] for row in rows]

  async def backfill_reply_ids(
    self, conversation_id, messages, *, dry_run=False
  ) -> int:
    """Fill missing parent IDs only, from verified Telegram message objects.

    This is an explicit operator-maintenance interface, not an HTTP permission
    bypass. It creates no messages/conversations and changes no other metadata.
    """
    messages = list(messages)
    if len(messages) > 100:
      raise ValueError("reply backfill batches cannot exceed 100 messages")
    changed = 0
    async with self.get_conn() as conn:
      group = await self._reply_backfill_group(conn, conversation_id)
      expected_peer = (group["telegram_peer_type"], group["telegram_peer_id"])
      for msg in messages:
        if (
          not isinstance(msg, types.Message)
          or self._peer_info(msg)[1:3] != expected_peer
        ):
          raise ValueError("reply backfill message does not match the archived peer")
        if type(msg.id) is not int or not 0 < msg.id < 2**63:
          raise ValueError("invalid reply backfill message ID")
      await self._lock_peer_writes(conn, group["telegram_peer_id"], exclusive=True)
      conn = ArchiveConnection(conn, group["archive_id"])
      for msg in sorted(messages, key=lambda item: item.id):
        old = await conn.fetchrow(
          """
          SELECT * FROM {messages} WHERE conversation_id = $1 AND msgid = $2
          ORDER BY created_at DESC LIMIT 1 FOR UPDATE
          """,
          conversation_id,
          msg.id,
        )
        if old is not None:
          changed += await self._fill_missing_reply_id(conn, old, msg, dry_run=dry_run)
    return changed

  async def delete_messages(
    self,
    peer_id: int,
    message_ids: list[int],
    kind="group",
    topic_id=None,
    peer_type=None,
  ) -> None:
    async with self.get_conn() as conn:
      await self._lock_peer_writes(conn, peer_id, exclusive=True)
      kinds = ("private_chat",) if kind == "private_chat" else ("group", "topic")
      peers = await conn.fetch(
        """SELECT DISTINCT archive_id FROM conversations
           WHERE telegram_peer_id=$1 AND kind=ANY($2::text[])
             AND ($3::text IS NULL OR telegram_peer_type=$3)""",
        peer_id, kinds, peer_type,
      )
      # Never guess between a basic group, a channel and a private peer sharing
      # a numeric ID. Telegram deletion callbacks normally provide peer_type.
      if len(peers) != 1:
        return
      conn = ArchiveConnection(conn, peers[0]["archive_id"])
      for msgid in message_ids:
        old = await conn.fetchrow(
          """
          SELECT m.* FROM {messages} m JOIN conversations c ON c.id = m.conversation_id
          WHERE c.telegram_peer_id = $1 AND c.kind = ANY($2::text[])
            AND ($3::bigint IS NULL OR c.topic_id = $3)
            AND ($4::text IS NULL OR c.telegram_peer_type = $4)
            AND m.msgid = $5
          ORDER BY m.created_at DESC LIMIT 1 FOR UPDATE
        """,
          peer_id,
          kinds,
          topic_id,
          peer_type,
          msgid,
        )
        if not old or old["deleted_at"]:
          continue
        await self._save_revision(conn, old, "delete")
        await conn.execute(
          """
          UPDATE {messages} SET deleted_at = now(), text = ''
          WHERE conversation_id = $1 AND msgid = $2
        """,
          old["conversation_id"],
          msgid,
        )

  async def _accessible_ids(self, conn, principal: Principal):
    if principal.is_anonymous:
      return await conn.fetchval("""
        SELECT coalesce(array_agg(DISTINCT id), '{}') FROM (
          SELECT p.conversation_id AS id
          FROM public_conversation_access p JOIN conversations direct
            ON direct.id = p.conversation_id
          WHERE direct.kind <> 'private_chat'
          UNION
          SELECT c.id
          FROM public_conversation_access p
          JOIN conversations parent ON parent.id = p.conversation_id
          JOIN conversations c ON c.telegram_peer_type = parent.telegram_peer_type
            AND c.telegram_peer_id = parent.telegram_peer_id
          WHERE parent.kind = 'group' AND c.kind IN ('group', 'topic')
        ) public_ids
      """)
    return await conn.fetchval(
      """
      SELECT coalesce(array_agg(id), '{}') FROM (
        SELECT p.conversation_id AS id
        FROM public_conversation_access p JOIN conversations direct
          ON direct.id = p.conversation_id
        WHERE direct.kind <> 'private_chat'
        UNION
        SELECT DISTINCT c.id AS id
        FROM public_conversation_access p
        JOIN conversations parent ON parent.id = p.conversation_id
        JOIN conversations c ON c.telegram_peer_type = parent.telegram_peer_type
          AND c.telegram_peer_id = parent.telegram_peer_id
        WHERE parent.kind = 'group' AND c.kind IN ('group', 'topic')
        UNION
        SELECT conversation_id AS id FROM conversation_access WHERE user_id = $1
        UNION
        SELECT c.id
        FROM conversation_access a
        JOIN conversations parent ON parent.id = a.conversation_id
        JOIN conversations c ON c.telegram_peer_type = parent.telegram_peer_type
          AND c.telegram_peer_id = parent.telegram_peer_id
        WHERE a.user_id = $1 AND parent.kind = 'group'
          AND c.kind IN ('group', 'topic')
      ) allowed
    """,
      principal.user_id,
    )

  async def can_access(self, conversation_id, principal: Principal) -> bool:
    async with self.get_conn() as conn:
      ids = await self._accessible_ids(conn, principal)
      return conversation_id in (ids or [])

  async def can_view_user(self, user_id: int, principal: Principal) -> bool:
    async with self.get_conn() as conn:
      allowed = await self._accessible_ids(conn, principal)
      return bool(
        await conn.fetchval(
          """
        SELECT EXISTS (
          SELECT 1 FROM archive_senders
          WHERE conversation_id = ANY($1::uuid[])
            AND uid = $2 AND live_messages > 0
        )
        """,
          allowed or [],
          user_id,
        )
      )

  async def _search_scope(self, conn, q, principal):
    if not q.group and not q.conversation_id:
      raise ValueError("g or conversation_id is required; cross-group search is disabled")
    allowed = await self._accessible_ids(conn, principal)
    group = await self.get_group(conn, q.group) if q.group else None
    if q.group and not group:
      raise GroupNotFound(q.group)
    if q.conversation_id:
      cid = uuid.UUID(str(q.conversation_id))
    elif group is not None:
      cid = group["conversation_uuid"]
    else:
      raise ValueError("g or conversation_id is required")
    if cid not in (allowed or []):
      raise GroupNotFound(cid)
    target = await conn.fetchrow("SELECT * FROM conversations WHERE id=$1", cid)
    if not target or (group and group["archive_id"] != target["archive_id"]):
      raise GroupNotFound(cid)
    # Even when the requested topic is visible, don't expose metadata from a
    # different or unauthorized parent through groupinfo.
    groups = await conn.fetch(
      """SELECT g.* FROM tg_groups g JOIN conversations c ON c.id=g.conversation_id
         WHERE c.archive_id=$1 AND c.id=ANY($2::uuid[])""",
      target["archive_id"], allowed or [],
    )
    info = {r["group_id"]: [r["pub_id"], r["name"]] for r in groups}
    return ArchiveConnection(conn, target["archive_id"]), allowed or [], info

  async def search(self, q: SearchQuery, principal: Principal):
    if q.min_score is not None and (
      q.mode != "semantic" or self.reranker is None or not valid_score(q.min_score)
    ):
      raise ValueError("min_score requires reranking and must be a finite number in 0-1")
    if q.include_deleted and q.mode == "semantic":
      raise ValueError("include_deleted is not supported for semantic search")
    if q.include_deleted and not self.history_enabled:
      raise ValueError("include_deleted requires message history enabled")
    # Resolve and authorize before model calls or physical table routing.
    async with self.get_conn() as conn:
      if q.include_deleted:
        # Isolation must precede ACL/routing reads as well as both content reads.
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
      scoped, allowed, groupinfo = await self._search_scope(conn, q, principal)
      if q.mode == "keyword":
        return groupinfo, await self._search_keywords(scoped, q, allowed)
    if q.mode != "semantic":
      raise ValueError("unknown search mode")
    if self.embedder is None:
      raise SemanticUnavailable("semantic search is not enabled")
    if not q.terms or not q.terms.strip() or len(q.terms) > MAX_QUERY_CHARS:
      raise ValueError("semantic search requires q of 1-2000 characters")
    vector, = await self.embedder.embed([q.terms.strip()], query=True)
    try:
      async with self.get_conn() as conn:
        # Recheck both permissions and routing after network/inference delay.
        scoped, allowed, groupinfo = await self._search_scope(conn, q, principal)
        window_query = q._replace(offset=0) if self.reranker else q
        limit = self.reranker.candidates if self.reranker else self.SEARCH_LIMIT + 1
        rows = await search_vectors(scoped, window_query, allowed, vector, limit)
      if self.reranker is not None:
        # The transaction has closed before HTTP/CPU inference, including waits.
        scores = await self.reranker.rerank(q.terms, [r["text"] for r in rows])
        def key(row):
          return row["conversation_id"], row["msgid"], row["created_at"]
        scored = {
          key(r): (r["text"], score, r["score"], index)
          for index, (r, score) in enumerate(zip(rows, scores, strict=True))
        }
        # Always recheck, even when every score is below the threshold. Fresh
        # metadata and unchanged text only; never backfill unscored candidates.
        async with self.get_conn() as conn:
          scoped, allowed, groupinfo = await self._search_scope(conn, q, principal)
          fresh = await search_vectors(scoped, window_query, allowed, vector, limit)
        cutoff = self.reranker.min_score if q.min_score is None else q.min_score
        rows = []
        for row in fresh:
          previous = scored.get(key(row))
          if previous is not None and previous[0] == row["text"] and previous[1] >= cutoff:
            rows.append({**dict(row), "vector_score": previous[2], "score": previous[1]})
        # Ties retain the original deterministic cosine/date/physical-key order.
        rows.sort(key=lambda r: (-r["score"], scored[key(r)][3]))
        rows = rows[q.offset:q.offset + self.reranker.page_size + 1]
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedObjectError) as exc:
      raise SemanticUnavailable("semantic search migration is required") from exc
    return groupinfo, rows

  async def _search_keywords(self, conn, q, allowed):
    query = text_to_query(q.terms.strip()) if q.terms else None
    if q.terms and not query:
      raise ValueError
    rows = await conn.fetch(
      """
      SELECT m.msgid, m.conversation_id, m.group_id, m.from_user,
        m.from_user_name, m.created_at, m.updated_at, m.text, c.telegram_peer_id,
        m.deleted_at, 'current'::text AS content_source,
        NULL::timestamptz AS snapshot_captured_at
      FROM {messages} m JOIN conversations c ON c.id = m.conversation_id
      WHERE m.deleted_at IS NULL AND m.conversation_id = ANY($1::uuid[])
        AND ($2::timestamptz IS NULL OR m.created_at > $2)
        AND ($3::timestamptz IS NULL OR m.created_at < $3)
        AND ($4::bigint IS NULL OR m.group_id = $4)
        AND ($5::uuid IS NULL OR m.conversation_id = $5)
        AND ($6::text IS NULL OR m.text &@~ $6)
        AND ($7::bigint[] IS NULL OR m.from_user = ANY($7))
        AND ($9::bigint[] IS NULL OR m.from_user IS NULL
             OR NOT (m.from_user = ANY($9)))
      ORDER BY m.created_at DESC, m.msgid DESC LIMIT $8
      """,
      allowed or [],
      q.start,
      q.end,
      q.group or None,
      q.conversation_id,
      query,
      q.sender,
      self.SEARCH_LIMIT,
      q.exclude_sender,
    )
    if q.include_deleted:
      # Keep the default live-text query/index path unchanged. Each source is
      # bounded before merging, then the combined page is limited once more.
      deleted_rows = await conn.fetch(
        """
        SELECT m.msgid, m.conversation_id, m.group_id, m.from_user,
          m.from_user_name, m.created_at, m.updated_at, snapshot.text,
          c.telegram_peer_id, m.deleted_at, snapshot.captured_at AS snapshot_captured_at,
          CASE WHEN snapshot.id IS NULL THEN 'unavailable'
               ELSE 'delete_snapshot' END AS content_source
        FROM {messages} m JOIN conversations c ON c.id = m.conversation_id
        LEFT JOIN LATERAL (
          SELECT r.id, r.text, r.captured_at FROM message_revisions r
          WHERE r.conversation_id = m.conversation_id AND r.msgid = m.msgid
            AND r.created_at = m.created_at AND r.revision_type = 'delete'
          ORDER BY r.captured_at DESC, r.id DESC LIMIT 1
        ) snapshot ON true
        WHERE m.deleted_at IS NOT NULL AND m.conversation_id = ANY($1::uuid[])
          AND ($2::timestamptz IS NULL OR m.created_at > $2)
          AND ($3::timestamptz IS NULL OR m.created_at < $3)
          AND ($4::bigint IS NULL OR m.group_id = $4)
          AND ($5::uuid IS NULL OR m.conversation_id = $5)
          AND ($6::text IS NULL OR snapshot.text &@~ $6)
          AND ($7::bigint[] IS NULL OR m.from_user = ANY($7))
          AND ($9::bigint[] IS NULL OR m.from_user IS NULL
               OR NOT (m.from_user = ANY($9)))
        ORDER BY m.created_at DESC, m.msgid DESC LIMIT $8
        """,
        allowed or [],
        q.start,
        q.end,
        q.group or None,
        q.conversation_id,
        query,
        q.sender,
        self.SEARCH_LIMIT,
        q.exclude_sender,
      )
      rows = sorted(
        [*rows, *deleted_rows],
        key=lambda row: (row["created_at"], row["msgid"]),
        reverse=True,
      )[: self.SEARCH_LIMIT]
    if query and rows:
      # Highlight exactly the authorized, limited texts already selected,
      # including snapshots. Re-reading messages by ID would lose snapshots
      # and could substitute a different physical (created_at) variant.
      highlighted = await conn.fetch(
        """
        SELECT pgroonga_highlight_html(selected.text,
          pgroonga_query_extract_keywords($1)) AS html
        FROM unnest($2::text[]) WITH ORDINALITY AS selected(text, position)
        ORDER BY selected.position
        """,
        query,
        [row["text"] for row in rows],
      )
      rows = [
        dict(row) | {"html": rendered["html"]}
        for row, rendered in zip(rows, highlighted, strict=True)
      ]
    return rows

  async def get_groups(self, principal: Principal):
    async with self.get_conn() as conn:
      allowed = await self._accessible_ids(conn, principal)
      return await conn.fetch(
        """
        SELECT g.*, c.id AS conversation_uuid FROM tg_groups g
        JOIN conversations c ON c.id = g.conversation_id
        WHERE c.id = ANY($1::uuid[])
      """,
        allowed or [],
      )

  async def find_names(self, group: int, q: str, principal: Principal, conversation_id=None):
    q = q.strip()
    if not q:
      raise ValueError
    async with self.get_conn() as conn:
      conn, allowed, _ = await self._search_scope(
        conn, SearchQuery(group, None, None, None, None, conversation_id), principal
      )
      # Search names only in authorized, live messages.  The legacy usernames
      # table is intentionally not used here: it has no conversation UUID and
      # would leak names from private chats and from revoked conversations.
      sql = """
        SELECT from_user_name AS name, array_agg(DISTINCT from_user) AS uid,
               max(created_at) AS last_seen
        FROM {messages}
        WHERE deleted_at IS NULL AND from_user IS NOT NULL
          AND conversation_id = ANY($1::uuid[])
          AND from_user_name ILIKE $2
      """
      params = [allowed, f"%{q}%"]
      if conversation_id:
        sql += " AND conversation_id = $3"
        params.append(uuid.UUID(str(conversation_id)))
      elif group:
        sql += " AND group_id = $3"
        params.append(group)
      sql += " GROUP BY from_user_name ORDER BY last_seen DESC LIMIT 15"
      rows = await conn.fetch(sql, *params)
      return [(uid, r["name"]) for r in rows for uid in r["uid"]]

  async def find_group_message_conversation(
    self, group_id, msgid, principal: Principal
  ):
    """Resolve a legacy group/message pair without exposing inaccessible topics."""
    async with self.get_conn() as conn:
      allowed = await self._accessible_ids(conn, principal)
      if not allowed:
        return None
      group = await self.get_group(conn, group_id)
      if not group or not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM conversations WHERE archive_id=$1 AND id=ANY($2::uuid[]))",
        group["archive_id"], allowed,
      ):
        return None
      conn = ArchiveConnection(conn, group["archive_id"])
      return await conn.fetchval(
        """
        SELECT m.conversation_id FROM {messages} m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE m.group_id = $1 AND m.msgid = $2
          AND c.kind IN ('group', 'topic') AND c.id = ANY($3::uuid[])
        ORDER BY m.created_at DESC, (c.kind = 'group') DESC LIMIT 1
        """,
        group_id,
        msgid,
        allowed,
      )

  async def get_message(self, conversation_id, msgid, principal: Principal):
    async with self.get_conn() as conn:
      allowed = await self._accessible_ids(conn, principal)
      if uuid.UUID(str(conversation_id)) not in (allowed or []):
        return None
      conn = await self.archive_conn(conn, conversation_id)
      return await conn.fetchrow(
        """
        SELECT m.*, c.kind, c.name, c.telegram_peer_id, c.topic_id AS conversation_topic_id
        FROM {messages} m JOIN conversations c ON c.id = m.conversation_id
        WHERE m.conversation_id = $1 AND m.msgid = $2 AND m.conversation_id = ANY($3::uuid[])
        ORDER BY m.created_at DESC LIMIT 1
      """,
        conversation_id,
        msgid,
        allowed or [],
      )

  async def _reply_candidates(
    self, conn, peer_ids, ids, seen, preferred, limit, *, children
  ):
    # Separate predicates keep both paths indexable, even with a generic prepared
    # plan. All message bodies are restricted to authorized conversations first.
    if children:
      sql = """
        SELECT DISTINCT ON (msgid) * FROM {messages}
        WHERE conversation_id = ANY($1::uuid[]) AND reply_to_id = ANY($2::bigint[])
          AND NOT (msgid = ANY($3::bigint[]))
        ORDER BY msgid, (conversation_id = $4) DESC, created_at DESC, conversation_id
        LIMIT $5
      """
    else:
      sql = """
        SELECT DISTINCT ON (msgid) * FROM {messages}
        WHERE conversation_id = ANY($1::uuid[]) AND msgid = ANY($2::bigint[])
          AND NOT (msgid = ANY($3::bigint[]))
        ORDER BY msgid, (conversation_id = $4) DESC, created_at DESC, conversation_id
        LIMIT $5
      """
    return await conn.fetch(sql, peer_ids, ids, list(seen), preferred, limit)

  async def _reply_thread(self, conn, target, allowed, depth, limit):
    peers = await conn.fetch(
      """
      SELECT c.id FROM conversations c JOIN conversations target ON target.id = $1
      WHERE c.telegram_peer_type = target.telegram_peer_type
        AND c.telegram_peer_id = target.telegram_peer_id
        AND c.id = ANY($2::uuid[])
    """,
      target["conversation_id"],
      allowed or [],
    )
    peer_ids = [r["id"] for r in peers]
    preferred = target["conversation_id"]
    seen = {target["msgid"]}
    result = []
    current = target
    # Keep the old nearest-to-oldest ancestor ordering. Every reachable original
    # is loaded independently of search matches and the chronological window.
    for _ in range(min(depth, limit)):
      reply_id = current.get("reply_to_id")
      if not reply_id or reply_id in seen:
        break
      rows = await self._reply_candidates(
        conn,
        peer_ids,
        [reply_id],
        seen,
        preferred,
        1,
        children=False,
      )
      current = rows[0] if rows else {"msgid": reply_id, "status": "unavailable"}
      result.append(current)
      seen.add(reply_id)
    truncated = bool(current.get("reply_to_id") and current["reply_to_id"] not in seen)

    # Expand from the entire ancestor spine, including the target. This returns
    # parallel replies to the original question as well as the target's replies.
    # A missing/inaccessible original remains opaque; its visible children may
    # still be connected through a reply ID already exposed by visible content.
    frontier = sorted(seen)
    for _ in range(depth):
      remaining = limit - len(result)
      rows = await self._reply_candidates(
        conn,
        peer_ids,
        frontier,
        seen,
        preferred,
        remaining + 1,
        children=True,
      )
      overflow = len(rows) > remaining
      frontier = []
      for row in rows[:remaining]:
        result.append(row)
        seen.add(row["msgid"])
        frontier.append(row["msgid"])
      if overflow:
        truncated = True
        break
      if not frontier:
        break
    # Hitting the depth boundary is not proof of completeness. Probe only for
    # an authorized unseen child; never count or disclose hidden branches.
    if frontier and not truncated:
      truncated = bool(
        await self._reply_candidates(
          conn,
          peer_ids,
          frontier,
          seen,
          preferred,
          1,
          children=True,
        )
      )
    return result, truncated

  async def get_context(
    self,
    conversation_id,
    msgid,
    principal: Principal,
    before=5,
    after=5,
    depth=5,
    reply_limit=100,
  ):
    async with self.get_conn() as conn:
      allowed = await self._accessible_ids(conn, principal)
      if uuid.UUID(str(conversation_id)) not in (allowed or []):
        return None
      conn = await self.archive_conn(conn, conversation_id)
      target = await conn.fetchrow(
        """
        SELECT m.*, c.kind, c.name, c.telegram_peer_id
        FROM {messages} m JOIN conversations c ON c.id = m.conversation_id
        WHERE m.conversation_id = $1 AND m.msgid = $2
          AND m.conversation_id = ANY($3::uuid[])
        ORDER BY m.created_at DESC LIMIT 1
      """,
        conversation_id,
        msgid,
        allowed or [],
      )
      if not target:
        return None
      topic = target["topic_id"]
      before_rows = await conn.fetch(
        """
        SELECT * FROM {messages} WHERE conversation_id = $1
          AND msgid <> $2 AND ($3::bigint IS NULL OR topic_id = $3)
          AND created_at < $4 ORDER BY created_at DESC LIMIT $5
      """,
        conversation_id,
        msgid,
        topic,
        target["created_at"],
        before,
      )
      after_rows = await conn.fetch(
        """
        SELECT * FROM {messages} WHERE conversation_id = $1
          AND msgid <> $2 AND ($3::bigint IS NULL OR topic_id = $3)
          AND created_at > $4 ORDER BY created_at ASC LIMIT $5
      """,
        conversation_id,
        msgid,
        topic,
        target["created_at"],
        after,
      )
      replies, truncated = await self._reply_thread(
        conn,
        target,
        allowed,
        depth,
        reply_limit,
      )
      return {
        "target": target,
        "before": list(reversed(before_rows)),
        "after": list(after_rows),
        "replies": replies,
        "replies_truncated": truncated,
      }

  async def get_revisions(self, conversation_id, msgid, principal: Principal):
    async with self.get_conn() as conn:
      allowed = await self._accessible_ids(conn, principal)
      return await conn.fetch(
        """
        SELECT r.* FROM message_revisions r
        WHERE r.conversation_id = $1 AND r.msgid = $2
          AND r.conversation_id = ANY($3::uuid[])
        ORDER BY r.captured_at ASC
      """,
        conversation_id,
        msgid,
        allowed or [],
      )

  async def get_user_by_username(self, username: str):
    async with self.get_conn() as conn:
      return await conn.fetchrow(
        "SELECT * FROM auth_users WHERE username = $1", username
      )

  async def get_user(self, user_id):
    async with self.get_conn() as conn:
      return await conn.fetchrow("SELECT * FROM auth_users WHERE id = $1", user_id)

  async def list_users(self):
    async with self.get_conn() as conn:
      return await conn.fetch("""SELECT id, username, is_admin, is_active, created_at, updated_at
                                FROM auth_users ORDER BY username""")

  async def list_user_grants(self, user_id):
    async with self.get_conn() as conn:
      return await conn.fetch(
        """
        SELECT c.* FROM conversation_access a
        JOIN conversations c ON c.id = a.conversation_id
        WHERE a.user_id = $1 ORDER BY c.name
        """,
        user_id,
      )

  async def list_public_grants(self):
    async with self.get_conn() as conn:
      return await conn.fetch(
        """
        SELECT c.* FROM public_conversation_access a
        JOIN conversations c ON c.id = a.conversation_id
        ORDER BY c.name
        """
      )

  async def create_user(self, username, password_hash, is_admin=False):
    async with self.get_conn() as conn:
      return await conn.fetchrow(
        """INSERT INTO auth_users (username, password_hash, is_admin)
                                    VALUES ($1, $2, $3) RETURNING id, username, is_admin, is_active,
                                    created_at, updated_at""",
        username,
        password_hash,
        is_admin,
      )

  async def update_user(
    self, user_id, *, password_hash=None, is_active=None, is_admin=None
  ):
    if password_hash is None and is_active is None and is_admin is None:
      return await self.get_user(user_id)
    async with self.get_conn() as conn:
      current = await conn.fetchrow(
        "SELECT is_admin, is_active FROM auth_users WHERE id = $1 FOR UPDATE",
        user_id,
      )
      if not current:
        return None
      will_be_active_admin = (
        is_active if is_active is not None else current["is_active"]
      ) and (is_admin if is_admin is not None else current["is_admin"])
      if current["is_admin"] and current["is_active"] and not will_be_active_admin:
        count = await conn.fetchval(
          "SELECT count(*) FROM auth_users WHERE is_admin AND is_active"
        )
        if count <= 1:
          raise ValueError(
            "the last active administrator cannot be disabled or demoted"
          )
      return await conn.fetchrow(
        """
        UPDATE auth_users SET
          password_hash = coalesce($1, password_hash),
          is_active = coalesce($2, is_active),
          is_admin = coalesce($3, is_admin),
          updated_at = now()
        WHERE id = $4
        RETURNING id, username, is_admin, is_active, created_at, updated_at
      """,
        password_hash,
        is_active,
        is_admin,
        user_id,
      )

  async def delete_user(self, user_id):
    async with self.get_conn() as conn:
      current = await conn.fetchrow(
        "SELECT is_admin, is_active FROM auth_users WHERE id = $1 FOR UPDATE",
        user_id,
      )
      if not current:
        return False
      if current["is_admin"] and current["is_active"]:
        count = await conn.fetchval(
          "SELECT count(*) FROM auth_users WHERE is_admin AND is_active"
        )
        if count <= 1:
          raise ValueError("the last active administrator cannot be deleted")
      await conn.execute("DELETE FROM auth_users WHERE id = $1", user_id)
      return True

  async def grant_conversation(self, user_id, conversation_id):
    async with self.get_conn() as conn:
      if not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM auth_users WHERE id = $1)", user_id
      ):
        raise ValueError("user does not exist")
      if not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM conversations WHERE id = $1)",
        conversation_id,
      ):
        raise ValueError("conversation does not exist")
      await conn.execute(
        """INSERT INTO conversation_access (user_id, conversation_id)
                            VALUES ($1, $2) ON CONFLICT DO NOTHING""",
        user_id,
        conversation_id,
      )

  async def revoke_conversation(self, user_id, conversation_id):
    async with self.get_conn() as conn:
      await conn.execute(
        "DELETE FROM conversation_access WHERE user_id = $1 AND conversation_id = $2",
        user_id,
        conversation_id,
      )

  async def grant_public(self, conversation_id):
    async with self.get_conn() as conn:
      row = await conn.fetchrow(
        "SELECT kind FROM conversations WHERE id = $1", conversation_id
      )
      if not row or row["kind"] == "private_chat":
        raise ValueError("private conversations cannot be public")
      await conn.execute(
        """INSERT INTO public_conversation_access (conversation_id)
                            VALUES ($1) ON CONFLICT DO NOTHING""",
        conversation_id,
      )

  async def revoke_public(self, conversation_id):
    async with self.get_conn() as conn:
      await conn.execute(
        "DELETE FROM public_conversation_access WHERE conversation_id = $1",
        conversation_id,
      )

  async def save_refresh_token(self, user_id, token_hash, expires_at):
    async with self.get_conn() as conn:
      await conn.execute(
        """INSERT INTO auth_refresh_tokens (user_id, token_hash, expires_at)
                            VALUES ($1, $2, $3)""",
        user_id,
        token_hash,
        expires_at,
      )

  async def consume_refresh_token(self, token_hash):
    async with self.get_conn() as conn:
      return await conn.fetchrow(
        """
        UPDATE auth_refresh_tokens r SET revoked_at = now()
        FROM auth_users u
        WHERE r.user_id = u.id AND r.token_hash = $1
          AND r.revoked_at IS NULL AND r.expires_at > now()
          AND u.is_active
        RETURNING u.*
      """,
        token_hash,
      )

  async def revoke_refresh_token(self, token_hash):
    async with self.get_conn() as conn:
      await conn.execute(
        "UPDATE auth_refresh_tokens SET revoked_at = now() WHERE token_hash = $1",
        token_hash,
      )

  async def revoke_user_sessions(self, user_id):
    async with self.get_conn() as conn:
      await conn.execute(
        """UPDATE auth_refresh_tokens SET revoked_at = now()
                            WHERE user_id = $1 AND revoked_at IS NULL""",
        user_id,
      )

  async def list_conversations(self, principal: Principal):
    async with self.get_conn() as conn:
      allowed = await self._accessible_ids(conn, principal)
      return await conn.fetch(
        """SELECT * FROM conversations WHERE id = ANY($1::uuid[])
                                 ORDER BY name""",
        allowed or [],
      )

  async def list_all_conversations(self):
    async with self.get_conn() as conn:
      return await conn.fetch("SELECT * FROM conversations ORDER BY name")

  async def get_conversation(self, conversation_id):
    async with self.get_conn() as conn:
      return await conn.fetchrow(
        "SELECT * FROM conversations WHERE id = $1", conversation_id
      )

  async def monitoring_config_imported(self):
    async with self.get_conn() as conn:
      return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM bootstrap_state WHERE name = $1)",
        "group-monitoring-config-v1",
      )

  async def import_monitoring_config(self, groups):
    # Resolve Telegram entities before entering this transaction. Serialize the
    # one-time import across processes; disabled manual rows are never overwritten.
    async with self.get_conn() as conn:
      await conn.fetchval(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        "luoxu:monitoring-config-import",
      )
      if await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM bootstrap_state WHERE name = $1)",
        "group-monitoring-config-v1",
      ):
        return False
      for group in sorted(groups, key=lambda g: g.id):
        info = await self.insert_group(conn, group)
        await conn.execute(
          """INSERT INTO group_monitoring (conversation_id) VALUES ($1)
             ON CONFLICT (conversation_id) DO NOTHING""",
          info["conversation_uuid"],
        )
      await conn.execute(
        "INSERT INTO bootstrap_state (name) VALUES ($1)",
        "group-monitoring-config-v1",
      )
      return True

  async def add_monitored_group(self, group):
    async with self.get_conn() as conn:
      info = await self.insert_group(conn, group)
      await conn.execute(
        """INSERT INTO group_monitoring (conversation_id) VALUES ($1)
           ON CONFLICT (conversation_id) DO UPDATE
           SET manual_enabled = true, updated_at = now()""",
        info["conversation_uuid"],
      )
      return info

  async def set_manual_monitoring(self, conversation_id, enabled):
    async with self.get_conn() as conn:
      return bool(
        await conn.fetchval(
          """INSERT INTO group_monitoring (conversation_id, manual_enabled)
           SELECT c.id, $2 FROM conversations c
           JOIN tg_groups g ON g.conversation_id = c.id
           WHERE c.id = $1 AND c.kind = 'group'
           ON CONFLICT (conversation_id) DO UPDATE
           SET manual_enabled = EXCLUDED.manual_enabled, updated_at = now()
           RETURNING conversation_id""",
          conversation_id,
          enabled,
        )
      )

  async def list_group_monitoring(self, conversation_id=None):
    # Count references to a group's peer, including topic-only grants. These
    # are indexing reasons, NOT a new path through the content-access checks.
    async with self.get_conn() as conn:
      return await conn.fetch(
        """
        WITH access_refs AS (
          SELECT c.telegram_peer_type, c.telegram_peer_id,
                 sum(r.user_ref)::bigint AS user_references,
                 sum(r.public_ref)::bigint AS public_references
          FROM (
            SELECT conversation_id, 1 AS user_ref, 0 AS public_ref
              FROM conversation_access
            UNION ALL
            SELECT conversation_id, 0, 1 FROM public_conversation_access
          ) r JOIN conversations c ON c.id = r.conversation_id
          WHERE c.kind IN ('group', 'topic')
          GROUP BY c.telegram_peer_type, c.telegram_peer_id
        ), counts AS (
          SELECT c.*, coalesce(m.manual_enabled, false) AS manual_reference,
                 coalesce(a.user_references, 0) AS user_references,
                 coalesce(a.public_references, 0) AS public_references
          FROM tg_groups g JOIN conversations c ON c.id = g.conversation_id
          LEFT JOIN group_monitoring m ON m.conversation_id = c.id
          LEFT JOIN access_refs a
            ON a.telegram_peer_type = c.telegram_peer_type
           AND a.telegram_peer_id = c.telegram_peer_id
          WHERE c.kind = 'group' AND ($1::uuid IS NULL OR c.id = $1)
        )
        SELECT *, manual_reference::int + user_references + public_references
                  AS reference_count
        FROM counts ORDER BY telegram_peer_type, telegram_peer_id
      """,
        conversation_id,
      )
