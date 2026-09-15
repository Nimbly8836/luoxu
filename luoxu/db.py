import asyncio
import contextlib
import datetime
import json
import logging
from typing import Any, Literal

import asyncpg  # type: ignore[import-not-found]
from telethon.tl import types

from .auth import Principal  # type: ignore[import-not-found]
from .ctxvars import group_title, msg_source
from .indexing import format_msg, text_to_query
from .mediamgr import MediaMgr
from .ocr import OCRService
from .types import GroupNotFound, SearchQuery
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

  async def setup(self) -> None:
    self.pool = await asyncpg.create_pool(self.address)

  async def close(self) -> None:
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
        await conn.execute(
          "UPDATE conversations SET name = $1 WHERE id = $2",
          name,
          row["id"],
        )
      return row
    inserted = await conn.fetchrow(
      """
      INSERT INTO conversations
        (kind, telegram_peer_type, telegram_peer_id, topic_id, name, pub_id,
         legacy_group_id)
      VALUES ($1, $2, $3, $4, $5, $6, $7)
      ON CONFLICT DO NOTHING RETURNING *
      """,
      kind,
      peer_type,
      peer_id,
      topic_id,
      name,
      pub_id,
      legacy_group_id,
    )
    return inserted or await self._get_conversation(
      conn, kind, peer_type, peer_id, topic_id
    )

  async def get_group(self, conn, group_id: int):
    return await conn.fetchrow(
      """
      SELECT g.*, c.id AS conversation_uuid, c.kind, c.topic_id
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
    # Move atomically, including yearly partitions. Replayed duplicates may
    # already exist in the parent; retain the latest state and never resurrect
    # a deletion. This repair does not create new historical snapshots.
    await conn.execute(
      """
      WITH moved AS (
        DELETE FROM messages WHERE conversation_id = $2 RETURNING *
      )
      INSERT INTO messages (
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
        reply_to_id = EXCLUDED.reply_to_id, quote_text = EXCLUDED.quote_text,
        from_user = EXCLUDED.from_user, from_user_name = EXCLUDED.from_user_name,
        text = EXCLUDED.text, media = EXCLUDED.media,
        updated_at = EXCLUDED.updated_at, deleted_at = EXCLUDED.deleted_at
      WHERE (EXCLUDED.deleted_at IS NOT NULL,
             coalesce(EXCLUDED.deleted_at, EXCLUDED.updated_at, EXCLUDED.created_at))
          > (messages.deleted_at IS NOT NULL,
             coalesce(messages.deleted_at, messages.updated_at, messages.created_at))
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
    sender = await msg.get_sender()
    old = await conn.fetchrow(
      """
      SELECT * FROM messages
      WHERE conversation_id = $1 AND msgid = $2
      ORDER BY created_at DESC LIMIT 1 FOR UPDATE
    """,
      conversation["id"],
      msg.id,
    )
    incoming_edit = msg_source.get() == "editmsg"
    if old and old["deleted_at"]:
      return
    # Startup history replay must not overwrite a newer edit or restore a
    # message that was deleted while the indexer was offline.
    if old and not incoming_edit and old["updated_at"] is not None:
      return
    if old and incoming_edit:
      if msg.edit_date and old["updated_at"] and msg.edit_date <= old["updated_at"]:
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
      INSERT INTO messages
        (conversation_id, group_id, msgid, reply_to_id, topic_id, quote_text,
         from_user, from_user_name, text, media, created_at, updated_at, deleted_at)
      VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NULL)
      ON CONFLICT (conversation_id, msgid, created_at) DO UPDATE SET
        text = EXCLUDED.text, updated_at = EXCLUDED.updated_at,
        reply_to_id = EXCLUDED.reply_to_id, quote_text = EXCLUDED.quote_text,
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
      # Lock peers in a stable order for multi-peer batches. Normal writers can
      # run concurrently, but all yield to an exclusive relocation transaction.
      for peer_id in sorted({self._peer_info(msg)[2] for msg, _ in formatted}):
        await self._lock_peer_writes(conn, peer_id)
      data = [
        (msg, text, await self._conversation_for_message(conn, msg))
        for msg, text in formatted
      ]
      for msg, text, conversation in data:
        await self._insert_one_message(conn, msg, text, conversation)
      if data and update_loaded in (UpdateLoaded.update_last, UpdateLoaded.update_both):
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

  async def delete_messages(
    self,
    peer_id: int,
    message_ids: list[int],
    kind="group",
    topic_id=None,
    peer_type=None,
  ) -> None:
    async with self.get_conn() as conn:
      await self._lock_peer_writes(conn, peer_id)
      kinds = ("private_chat",) if kind == "private_chat" else ("group", "topic")
      for msgid in message_ids:
        old = await conn.fetchrow(
          """
          SELECT m.* FROM messages m JOIN conversations c ON c.id = m.conversation_id
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
          UPDATE messages SET deleted_at = now(), text = ''
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
          SELECT 1 FROM messages
          WHERE conversation_id = ANY($1::uuid[])
            AND from_user = $2 AND deleted_at IS NULL
        )
        """,
          allowed or [],
          user_id,
        )
      )

  async def search(self, q: SearchQuery, principal: Principal):
    async with self.get_conn() as conn:
      allowed = await self._accessible_ids(conn, principal)
      if q.group:
        group = await self.get_group(conn, q.group)
        if not group or group["conversation_uuid"] not in (allowed or []):
          raise GroupNotFound(q.group)
        groupinfo = {q.group: [group["pub_id"], group["name"]]}
      else:
        rows = await conn.fetch(
          """
          SELECT c.id, c.telegram_peer_id, c.pub_id, c.name
          FROM conversations c
          WHERE c.kind = 'group' AND c.id = ANY($1::uuid[])
        """,
          allowed or [],
        )
        groupinfo = {r["telegram_peer_id"]: [r["pub_id"], r["name"]] for r in rows}
    ret = []
    now = datetime.datetime.now(datetime.timezone.utc)
    this_year = min(q.end, now).year if q.end else now.year
    while True:
      start = datetime.datetime(this_year, 1, 1, tzinfo=datetime.timezone.utc)
      end = datetime.datetime(this_year + 1, 1, 1, tzinfo=datetime.timezone.utc)
      date_start = max(q.start, start) if q.start else start
      date_end = min(q.end, end) if q.end else end
      if date_start > date_end:
        break
      ret += await self._search_one_year(
        q, date_start, date_end, self.SEARCH_LIMIT - len(ret), principal
      )
      if len(ret) >= self.SEARCH_LIMIT or date_start < self.earliest_time:
        break
      this_year -= 1
    return groupinfo, ret

  async def _search_one_year(self, q, date_start, date_end, limit, principal):
    async with self.get_conn() as conn:
      allowed = await self._accessible_ids(conn, principal)
      query = text_to_query(q.terms.strip()) if q.terms else None
      if q.terms and not query:
        raise ValueError
      rows = await conn.fetch(
        """
        SELECT m.msgid, m.conversation_id, m.group_id, m.from_user,
          m.from_user_name, m.created_at, m.updated_at, m.text, c.telegram_peer_id
        FROM messages m JOIN conversations c ON c.id = m.conversation_id
        WHERE m.deleted_at IS NULL AND m.conversation_id = ANY($1::uuid[])
          AND m.created_at > $2 AND m.created_at < $3
          AND ($4::bigint IS NULL OR m.group_id = $4)
          AND ($5::uuid IS NULL OR m.conversation_id = $5)
          AND ($6::text IS NULL OR m.text &@~ $6)
          AND ($7::bigint[] IS NULL OR m.from_user = ANY($7))
        ORDER BY m.created_at DESC, m.msgid DESC LIMIT $8
        """,
        allowed or [],
        date_start,
        date_end,
        q.group or None,
        q.conversation_id,
        query,
        q.sender,
        max(0, limit),
      )
      highlight_query = query
      if highlight_query and rows:
        # Highlight only the limited result set, avoiding a full-table highlight.
        by_id = {(r["conversation_id"], r["msgid"]): r for r in rows}
        highlighted = await conn.fetch(
          """
          SELECT m.conversation_id, m.msgid, pgroonga_highlight_html(m.text,
            pgroonga_query_extract_keywords($1)) AS html
          FROM messages m
          JOIN unnest($3::uuid[], $4::bigint[]) AS selected(conversation_id, msgid)
            ON selected.conversation_id = m.conversation_id
           AND selected.msgid = m.msgid
          WHERE m.conversation_id = ANY($2::uuid[])
        """,
          highlight_query,
          allowed or [],
          [row["conversation_id"] for row in rows],
          [row["msgid"] for row in rows],
        )
        for row in highlighted:
          key = (row["conversation_id"], row["msgid"])
          if key in by_id:
            by_id[key] = dict(by_id[key]) | {"html": row["html"]}
        rows = [by_id[(r["conversation_id"], r["msgid"])] for r in rows]
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

  async def find_names(self, group: int, q: str, principal: Principal):
    q = q.strip()
    if not q:
      raise ValueError
    async with self.get_conn() as conn:
      allowed = self._accessible_ids(conn, principal)
      # Search names only in authorized, live messages.  The legacy usernames
      # table is intentionally not used here: it has no conversation UUID and
      # would leak names from private chats and from revoked conversations.
      sql = """
        SELECT from_user_name AS name, array_agg(DISTINCT from_user) AS uid,
               max(created_at) AS last_seen
        FROM messages
        WHERE deleted_at IS NULL AND from_user IS NOT NULL
          AND conversation_id = ANY($1::uuid[])
          AND from_user_name ILIKE $2
      """
      params = [await allowed, f"%{q}%"]
      if group:
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
      return await conn.fetchval(
        """
        SELECT m.conversation_id FROM messages m
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
      return await conn.fetchrow(
        """
        SELECT m.*, c.kind, c.name, c.telegram_peer_id, c.topic_id AS conversation_topic_id
        FROM messages m JOIN conversations c ON c.id = m.conversation_id
        WHERE m.conversation_id = $1 AND m.msgid = $2 AND m.conversation_id = ANY($3::uuid[])
        ORDER BY m.created_at DESC LIMIT 1
      """,
        conversation_id,
        msgid,
        allowed or [],
      )

  async def get_context(
    self, conversation_id, msgid, principal: Principal, before=5, after=5, depth=5
  ):
    async with self.get_conn() as conn:
      allowed = await self._accessible_ids(conn, principal)
      target = await conn.fetchrow(
        """
        SELECT m.*, c.kind, c.name, c.telegram_peer_id
        FROM messages m JOIN conversations c ON c.id = m.conversation_id
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
        SELECT * FROM messages WHERE conversation_id = $1
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
        SELECT * FROM messages WHERE conversation_id = $1
          AND msgid <> $2 AND ($3::bigint IS NULL OR topic_id = $3)
          AND created_at > $4 ORDER BY created_at ASC LIMIT $5
      """,
        conversation_id,
        msgid,
        topic,
        target["created_at"],
        after,
      )
      chain = []
      seen = {msgid}
      current = target
      for _ in range(depth):
        reply_id = current["reply_to_id"]
        if not reply_id or reply_id in seen:
          break
        seen.add(reply_id)
        current = await conn.fetchrow(
          """
          SELECT m.* FROM messages m
          JOIN conversations c ON c.id = m.conversation_id
          WHERE c.telegram_peer_type = (
                  SELECT telegram_peer_type FROM conversations WHERE id = $1
                )
            AND c.telegram_peer_id = (
                  SELECT telegram_peer_id FROM conversations WHERE id = $1
                )
            AND c.id = ANY($3::uuid[]) AND m.msgid = $2
          ORDER BY (m.conversation_id = $1) DESC, m.created_at DESC
          LIMIT 1
        """,
          conversation_id,
          reply_id,
          allowed or [],
        )
        if not current:
          chain.append({"msgid": reply_id, "status": "unavailable"})
          break
        chain.append(current)
      return {
        "target": target,
        "before": list(reversed(before_rows)),
        "after": list(after_rows),
        "replies": chain,
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
        "SELECT is_admin, is_active FROM auth_users WHERE id = $1 FOR UPDATE", user_id
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
        "SELECT is_admin, is_active FROM auth_users WHERE id = $1 FOR UPDATE", user_id
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
        "SELECT EXISTS (SELECT 1 FROM conversations WHERE id = $1)", conversation_id
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
