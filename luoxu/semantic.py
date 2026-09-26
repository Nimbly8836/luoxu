"""Optional, local semantic retrieval. No model runtime in the main application."""

import asyncio
import json
import math
from functools import partial

import aiohttp  # type: ignore[import-not-found]

from .storage import ArchiveConnection

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
MODEL_REVISION = "7999e1d3359715c523056ef9478215996d62a620"
# Include preprocessing/pooling version: incompatible vectors must never mix.
MODEL_ID = f"{MODEL_NAME}@{MODEL_REVISION}:sentence-transformers-strip-v1"
DIMENSIONS = 512
MAX_QUERY_CHARS = 2000
MAX_DOCUMENT_CHARS = 8192
MAX_OFFSET = 1000
# Share an explicit Unicode whitespace set with SQL and HTTP validation, rather
# than relying on PostgreSQL's locale-dependent regexes or space-only btrim.
STRIP_CHARS = (
  " \t\n\r\v\f\x1c\x1d\x1e\x1f\x85\xa0\u1680"
  "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
  "\u2028\u2029\u202f\u205f\u3000"
)


class SemanticUnavailable(Exception):
  pass


def vector_literal(vector):
  """Validate untrusted service output before casting a bound string to vector."""
  if not isinstance(vector, list) or len(vector) != DIMENSIONS:
    raise SemanticUnavailable("invalid embedding dimensions")
  if any(type(v) not in (int, float) or not math.isfinite(v) for v in vector):
    raise SemanticUnavailable("invalid embedding values")
  norm = math.sqrt(sum(v * v for v in vector))
  if not math.isfinite(norm) or norm == 0:
    raise SemanticUnavailable("invalid embedding norm")
  return json.dumps([v / norm for v in vector], allow_nan=False)


class EmbeddingClient:
  def __init__(self, endpoint):
    if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
      raise ValueError("database.semantic.endpoint must be an HTTP(S) URL")
    self.endpoint = endpoint
    self.session = None
    self.slots = asyncio.Semaphore(2)

  async def close(self):
    if self.session is not None:
      await self.session.close()
      self.session = None

  async def embed(self, texts, *, query=False):
    # Bound the entire request, including waiting for a client slot.
    try:
      async with asyncio.timeout(60), self.slots:
        if self.session is None:
          self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
            # Avoid up to 12-byte ASCII escapes for supplementary characters.
            json_serialize=partial(json.dumps, ensure_ascii=False),
          )
        async with self.session.post(
          self.endpoint,
          json={
            "texts": [text.strip(STRIP_CHARS)[:MAX_DOCUMENT_CHARS] for text in texts],
            "query": query,
          },
          allow_redirects=False,
        ) as response:
          if response.status != 200:
            raise SemanticUnavailable("embedding service unavailable")
          data = await response.json()
        if not isinstance(data, dict) or data.get("model") != MODEL_ID:
          raise SemanticUnavailable("embedding model mismatch")
        vectors = data.get("vectors")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
          raise SemanticUnavailable("invalid embedding count")
        return [vector_literal(vector) for vector in vectors]
    except (aiohttp.ClientError, TimeoutError, ValueError, OverflowError) as exc:
      raise SemanticUnavailable("embedding service unavailable") from exc


async def search_vectors(conn, q, allowed, vector, limit):
  # Exact cosine ranking is deliberate at ~100k messages: all ACL/metadata
  # predicates apply before LIMIT, with no ANN post-filter recall loss.
  # The content hash also excludes stale vectors during an edit/backfill race.
  return await conn.fetch(
    """
    SELECT m.msgid, m.conversation_id, m.group_id, m.from_user,
      m.from_user_name, m.created_at, m.updated_at, m.text,
      1 - (e.embedding <=> $1::text::vector) AS score
    FROM {messages} m JOIN {embeddings} e
      ON e.conversation_id = m.conversation_id AND e.msgid = m.msgid
        AND e.created_at = m.created_at AND e.content_hash = md5(m.text)
    WHERE e.model = $2 AND m.deleted_at IS NULL
      AND m.conversation_id = ANY($3::uuid[])
      AND ($4::bigint IS NULL OR m.group_id = $4)
      AND ($5::uuid IS NULL OR m.conversation_id = $5)
      AND ($6::bigint[] IS NULL OR m.from_user = ANY($6))
      AND ($7::bigint[] IS NULL OR m.from_user IS NULL
           OR NOT (m.from_user = ANY($7)))
      AND ($8::timestamptz IS NULL OR m.created_at > $8)
      AND ($9::timestamptz IS NULL OR m.created_at < $9)
    ORDER BY e.embedding <=> $1::text::vector, m.created_at DESC,
      m.conversation_id, m.msgid DESC
    LIMIT $10 OFFSET $11
    """,
    vector,
    MODEL_ID,
    allowed or [],
    q.group or None,
    q.conversation_id,
    q.sender,
    q.exclude_sender,
    q.start,
    q.end,
    limit,
    q.offset,
  )


async def index_batch(db, embedder, batch_size=16, *, archive_id):
  """Resumable backfill and incremental indexing, without blocking ingestion."""
  async with db.get_conn() as raw_conn:
    conn = ArchiveConnection(raw_conn, archive_id)
    rows = await conn.fetch(
      """
      SELECT m.conversation_id, m.msgid, m.created_at, m.text,
        md5(m.text) AS content_hash
      FROM {messages} m LEFT JOIN {embeddings} e
        ON e.conversation_id = m.conversation_id AND e.msgid = m.msgid
          AND e.created_at = m.created_at AND e.model = $1
      WHERE m.deleted_at IS NULL AND btrim(m.text, $3) <> ''
        AND (e.model IS NULL OR e.content_hash <> md5(m.text))
      ORDER BY m.created_at, m.conversation_id, m.msgid
      LIMIT $2
      """,
      MODEL_ID,
      batch_size,
      STRIP_CHARS,
    )
  if not rows:
    return 0
  # No DB transaction/row locks are held while the CPU service is running.
  vectors = await embedder.embed([row["text"] for row in rows])
  for row, vector in zip(rows, vectors, strict=True):
    async with db.get_conn() as raw_conn:
      conn = ArchiveConnection(raw_conn, archive_id)
      # Lock and recheck only after inference. Edits/deletes either precede this
      # check or wait and invalidate the vector through the database trigger.
      current = await conn.fetchval(
        """
        SELECT 1 FROM {messages}
        WHERE conversation_id = $1 AND msgid = $2 AND created_at = $3
          AND deleted_at IS NULL AND md5(text) = $4
        FOR SHARE
        """,
        row["conversation_id"], row["msgid"], row["created_at"], row["content_hash"],
      )
      if not current:
        continue
      # Literal SQL; the vector and all identifiers are bound values, not SQL.
      # pi-lens-ignore: python-sql-injection
      await conn.execute(
        """
        INSERT INTO {embeddings}
          (conversation_id, msgid, created_at, model, content_hash, embedding)
        VALUES ($1, $2, $3, $4, $5, $6::text::vector)
        ON CONFLICT (conversation_id, msgid, created_at, model) DO UPDATE
          SET content_hash = EXCLUDED.content_hash, embedding = EXCLUDED.embedding
        """,
        row["conversation_id"], row["msgid"], row["created_at"], MODEL_ID,
        row["content_hash"], vector,
      )
  return len(rows)
