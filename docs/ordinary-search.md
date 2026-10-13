# Database-only ordinary search

`GET /luoxu/search?g=456&q=test&sort=relevance` orders keyword hits by
lexical frequency, without embeddings, model requests or reranking. Alternatively,
`match=literal` finds an exact contiguous substring of original archived text.
These options are independent: `match=literal&sort=relevance` is supported.
No migration, pgvector, model service, index rebuild or storage setting change is
required. PGroonga must provide `pgroonga_query_extract_keywords` and
`pgroonga_match_positions_character` (tested with PGroonga 4.0.8).
Frontend controls/integration are separate; this repository supplies the API only.

## Request contract

- `mode=keyword` (default), `match=query` (default), `sort=time` (default) preserve
  existing AND/OR/negative/parenthesis syntax, OpenCC expansion, highlighting and
  descending original-message-time pagination. Omitting `q` or sending `q=` still
  browses messages in this default mode. NUL is rejected with 400.
- `match=literal` bypasses operator parsing, trimming, case folding and OpenCC.
  Spaces (including whitespace-only queries), newlines, punctuation, quotes,
  backslashes, `%`, `_`, Chinese variants and emoji are significant. URL-encode
  the complete value. A substring is not whole-message equality.
- Literal matching or relevance sorting requires `q` of 1–2000 Unicode characters.
  Query-syntax relevance also requires a nonblank, valid effective query.
- Unknown enums, nonzero keyword/time offsets, and nondefault match/sort options
  with `mode=semantic` return 400. Semantic defaults, vector/reranker scores and
  metadata are unchanged. `min_score` remains semantic-reranker-only.
- `g` or `conversation_id` is required. Current public/personal grants, group,
  archive, topic, private-chat, sender/excluded-sender and open-interval `start`
  and `end` filters apply **before** ranking. Exclusions retain unknown senders.
  Administrators do not bypass grants. Inaccessible targets return 404.
- `include_deleted=true` still requires both history gates. Matching, scoring and
  highlighting use only the latest delete snapshot (capture time then revision ID)
  for the exact `(conversation_id,msgid,created_at)` key, never an older edit or
  residual deleted text. Missing snapshots cannot match. Live and snapshot rows
  share one global ranking under the existing REPEATABLE READ transaction.

## Ranking and response

New options return `mode=keyword`, `match`, `sort`, `ranking`, and `score_kind`.
Literal/time uses `ranking=time`, `score_kind=null`, no per-message score, and
legacy 50-row/date pagination. Default query/time responses are unchanged.

Relevance uses `ranking=database_lexical` and a nonnegative **integer** `score`:

- `match=query`: `score_kind=pgroonga_hit_spans`. Transform the query using existing
  syntax/OpenCC, extract positive keywords once with
  `pgroonga_query_extract_keywords(transformed_query)`, then score each effective
  text as `coalesce(array_length(pgroonga_match_positions_character(text, keywords), 1), 0)`.
  This counts PGroonga-reported normalized hit spans. Excluded terms do not score;
  negative-only queries can match with score zero. Normalization, expanded
  variants and PGroonga span handling affect counts: these are not independent
  token/term counts, document-length-normalized weights, BM25, native
  `pgroonga_score`, semantic cosine similarity or probabilities.
- `match=literal`: `score_kind=literal_occurrences`. SQL computes
  `(char_length(text) - char_length(replace(text, query, ''))) / char_length(query)`:
  the count of non-overlapping case-sensitive exact occurrences. For example,
  `aaa` in `aaaaa` scores 1, not 3. `strpos(text, query)>0` is the authoritative
  filter, with no PGroonga prefilter that could discard punctuation/space matches.

Both scoring rules apply equally to live and snapshot text and are independent
of index-versus-sequential execution plans. Every eligible matching row is scored
before LIMIT, **not just the newest 50**. Order is score descending, creation time
descending, message ID descending, then conversation UUID descending (full
physical-key tie break). Literal/time uses the same tie order without score.

Relevance returns `page_size=50`, `next_offset` (integer or null), and `has_more`.
Start with `offset=0`, then send `next_offset` with unchanged filters. Offset is
bounded to 0–1000. A 51st-row sentinel after combined ranking determines more
results; at the offset bound `has_more=false` and `next_offset=null`, not a total
count claim. Do not advance using the last timestamp in relevance mode. Each
request is consistent internally, but **there is no snapshot across requests**:
concurrent edits, deletion or grant changes can shift offset pages.

Messages retain existing HTML, IDs, timestamps and deletion provenance fields.
Literal highlighting splits raw text on the raw query, then escapes every piece
and highlighted query independently; it never replaces inside HTML entities.
Responses retain `Cache-Control: private, no-store` and `Vary: Authorization`.

## Cost and failure behavior

Literal search may scan all eligible original text, including whitespace-only
queries. Relevance must score all eligible matches, and snapshot reads add work.
Narrow the group/conversation and creation-time range on large archives. New
search work uses transaction-local `statement_timeout` capped at 10 seconds per
statement; an already stricter nonzero owner timeout is preserved. No global
configuration changes occur. Timeout returns explicit HTTP 503 and never silently
falls back to time ordering, semantic search or incomplete results.
