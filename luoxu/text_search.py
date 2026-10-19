"""Database-only literal matching and plan-independent lexical ranking."""

from html import escape

from .indexing import text_to_query
from .types import SearchQuery

MAX_QUERY_CHARS = 2000
MAX_OFFSET = 1000
STATEMENT_TIMEOUT_MS = 10000


def advanced_text_search(q: SearchQuery) -> bool:
  return q.mode == "keyword" and (q.match != "query" or q.sort != "time")


def validate_text_options(q: SearchQuery) -> None:
  # Shared by HTTP parsing and direct store callers; appended tuple fields keep
  # existing positional callers compatible.
  if q.mode not in ("keyword", "semantic"):
    raise ValueError("unknown search mode")
  if q.match not in ("query", "literal") or q.sort not in ("time", "relevance"):
    raise ValueError("match must be query or literal; sort must be time or relevance")
  if q.order not in ("asc", "desc"):
    raise ValueError("order must be asc or desc")
  if q.order == "asc" and (q.mode != "keyword" or q.sort != "time"):
    raise ValueError("order=asc requires keyword mode with sort=time")
  if q.mode == "semantic":
    if q.match != "query" or q.sort != "time":
      raise ValueError("match and sort options require keyword mode")
    return
  if type(q.offset) is not int or not 0 <= q.offset <= MAX_OFFSET:
    raise ValueError("offset must be an integer in 0-1000")
  if q.sort == "time" and q.offset != 0:
    raise ValueError("keyword offset requires sort=relevance")
  if q.terms is not None and "\0" in q.terms:
    raise ValueError("q must not contain NUL")
  if advanced_text_search(q):
    if not q.terms or len(q.terms) > MAX_QUERY_CHARS:
      raise ValueError("literal/relevance search requires q of 1-2000 characters")
    if q.match == "query" and not q.terms.strip():
      raise ValueError("relevance search requires a nonblank query")


async def set_text_search_timeout(conn) -> None:
  # pg_settings.setting is in milliseconds even if the owner used '2s'.
  # A transaction-local change cannot leak into the connection pool.
  await conn.fetchval(
    """SELECT set_config('statement_timeout',
      least(coalesce(nullif(setting::int, 0), $1), $1)::text, true)
      FROM pg_settings WHERE name = 'statement_timeout'""",
    STATEMENT_TIMEOUT_MS,
  )


def literal_highlight(text: str, query: str) -> str:
  marker = '<span class="keyword">' + escape(query) + '</span>'
  return marker.join(escape(piece) for piece in text.split(query))


async def search_text(conn, q: SearchQuery, allowed, limit: int):
  assert q.terms is not None  # validated at the store boundary
  try:
    query = q.terms if q.match == "literal" else text_to_query(q.terms.strip())
  except RuntimeError as exc:
    # The Rust query parser exposes eyre parse errors as RuntimeError.
    raise ValueError("invalid keyword query") from exc
  if not query:
    raise ValueError("empty effective query")
  keywords = (
    await conn.fetchval("SELECT pgroonga_query_extract_keywords($1)", query)
    if q.match == "query" else []
  )
  # These fragments are server-owned alternatives, never request SQL. Literal
  # matching deliberately has no normalized PGroonga prefilter (false negatives).
  live_predicate = "strpos(m.text, $6) > 0" if q.match == "literal" else "m.text &@~ $6"
  snapshot_predicate = "strpos(snapshot.text, $6) > 0" if q.match == "literal" else "snapshot.text &@~ $6"
  score = (
    "(char_length(text) - char_length(replace(text, $6, ''))) / char_length($6)"
    if q.match == "literal" else
    "coalesce(array_length(pgroonga_match_positions_character(text, $10::text[]), 1), 0)"
  ) if q.sort == "relevance" else "NULL::integer"
  order = "score DESC, " if q.sort == "relevance" else ""
  # Complete server-owned clauses: never interpolate a request as SQL.
  order += (
    "created_at ASC, msgid ASC, conversation_id ASC" if q.order == "asc"
    else "created_at DESC, msgid DESC, conversation_id DESC"
  )
  rows = await conn.fetch(
    """
    WITH eligible AS NOT MATERIALIZED (
      SELECT m.*, c.telegram_peer_id
      FROM {messages} m JOIN conversations c ON c.id = m.conversation_id
      WHERE m.conversation_id = ANY($1::uuid[])
        AND ($2::timestamptz IS NULL OR m.created_at > $2)
        AND ($3::timestamptz IS NULL OR m.created_at < $3)
        AND ($4::bigint IS NULL OR m.group_id = $4)
        AND ($5::uuid IS NULL OR m.conversation_id = $5)
        AND ($7::bigint[] IS NULL OR m.from_user = ANY($7))
        AND ($9::bigint[] IS NULL OR m.from_user IS NULL
             OR NOT (m.from_user = ANY($9)))
        AND $10::text[] IS NOT NULL
    ), effective AS (
      SELECT m.msgid, m.conversation_id, m.group_id, m.from_user,
        m.from_user_name, m.created_at, m.updated_at, m.telegram_peer_id,
        m.deleted_at, m.text, 'current'::text AS content_source,
        NULL::timestamptz AS snapshot_captured_at
      FROM eligible m WHERE m.deleted_at IS NULL AND """ + live_predicate + """
      UNION ALL
      SELECT m.msgid, m.conversation_id, m.group_id, m.from_user,
        m.from_user_name, m.created_at, m.updated_at, m.telegram_peer_id,
        m.deleted_at, snapshot.text, 'delete_snapshot'::text AS content_source,
        snapshot.captured_at AS snapshot_captured_at
      FROM eligible m JOIN LATERAL (
        SELECT r.id, r.text, r.captured_at FROM message_revisions r
        WHERE m.deleted_at IS NOT NULL AND $11::boolean
          AND r.conversation_id = m.conversation_id AND r.msgid = m.msgid
          AND r.created_at = m.created_at AND r.revision_type = 'delete'
        ORDER BY r.captured_at DESC, r.id DESC LIMIT 1
      ) snapshot ON true
      WHERE m.deleted_at IS NOT NULL AND $11::boolean
        AND """ + snapshot_predicate + """
    )
    SELECT *, """ + score + """ AS score FROM effective
    ORDER BY """ + order + " LIMIT $8 OFFSET $12",
    allowed or [], q.start, q.end, q.group or None, q.conversation_id,
    query, q.sender, limit + (q.sort == "relevance"), q.exclude_sender,
    keywords, q.include_deleted, q.offset,
  )
  if q.match == "literal":
    return [dict(row) | {"html": literal_highlight(row["text"], query)} for row in rows]
  if rows:
    highlighted = await conn.fetch(
      """SELECT pgroonga_highlight_html(selected.text, $1::text[]) AS html
      FROM unnest($2::text[]) WITH ORDINALITY AS selected(text, position)
      ORDER BY selected.position""",
      keywords, [row["text"] for row in rows],
    )
    return [dict(row) | {"html": html["html"]}
            for row, html in zip(rows, highlighted, strict=True)]
  return rows
