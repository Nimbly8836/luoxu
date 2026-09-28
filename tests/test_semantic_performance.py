"""Exact SQL equivalence and bounded lookup plans on disposable PostgreSQL."""

import datetime
import json
import unittest
from unittest.mock import patch

from luoxu.semantic import MODEL_ID, revalidate_candidates, search_vectors, vector_literal
from luoxu.storage import ArchiveConnection
from luoxu.types import SearchQuery
from test_semantic_search import SemanticDatabaseFixture, UTC, vector


# Frozen pre-optimization SQL: compare full Records, not just IDs/scores.
ORIGINAL_SEARCH = """
SELECT m.msgid, m.conversation_id, m.group_id, m.from_user,
  m.from_user_name, m.created_at, m.updated_at, m.text,
  m.deleted_at, 'current'::text AS content_source,
  NULL::timestamptz AS snapshot_captured_at,
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
"""


class SemanticPerformanceTests(SemanticDatabaseFixture):
  async def test_search_vectors_matches_original_fields_filters_ties_and_offsets(self):
    async with self.db.get_conn() as conn:
      topic = await self.db._ensure_conversation(
        conn, "topic", "channel", 123, "Topic", topic_id=42, legacy_group_id=123,
      )
    for cid in (self.public, topic["id"], self.private):
      for mid, text, sender, year in (
        (1, "job older", 100, 2020), (1, "job newer", 100, 2025),
        (2, "job tied", 200, 2025), (3, "food", None, 2025),
        (4, "job deleted", 100, 2025), (5, "job hash", 100, 2025),
        (6, "job model", 100, 2025), (7, "job absent", 100, 2025),
      ):
        await self.seed(mid, text, cid=cid, sender=sender, year=year)
    await self.backfill()
    async with self.db.get_conn() as raw:
      for cid in (self.public, self.private):
        conn = await self.db.archive_conn(raw, cid)
        await conn.execute("UPDATE {messages} SET deleted_at=now() WHERE msgid=4")
        await conn.execute("UPDATE {embeddings} SET content_hash='stale' WHERE msgid=5")
        await conn.execute("UPDATE {embeddings} SET model='other' WHERE msgid=6")
        await conn.execute("DELETE FROM {embeddings} WHERE msgid=7")
      base = SearchQuery(123, "job", None, None, None, mode="semantic")
      cases = ({}, {"sender": [100]}, {"sender": []}, {"exclude_sender": [100, 200]},
               {"sender": [100, 200], "exclude_sender": [200]},
               {"start": datetime.datetime(2025, 1, 2, tzinfo=UTC)},
               {"end": datetime.datetime(2025, 1, 2, tzinfo=UTC)},
               {"conversation_id": str(topic["id"])}, {"group": 999},
               {"group": 0, "conversation_id": str(self.private)})
      for changes in cases:
        for allowed in ([], [self.public], [self.public, topic["id"], self.private]):
          for offset in (0, 1, 3, 20):
            for limit in (1, 5, 51):
              q = base._replace(**changes, offset=offset)
              conn = await self.db.archive_conn(raw, self.private if q.group == 0 else self.public)
              v = vector_literal(vector())
              old = await conn.fetch(ORIGINAL_SEARCH, v, MODEL_ID, allowed, q.group or None,
                                     q.conversation_id, q.sender, q.exclude_sender,
                                     q.start, q.end, limit, offset)
              new = await search_vectors(conn, q, allowed, v, limit)
              with self.subTest(changes=changes, allowed=allowed, offset=offset, limit=limit):
                self.assertEqual([dict(r) for r in new], [dict(r) for r in old])
                self.assertEqual([list(r.keys()) for r in new], [list(r.keys()) for r in old])

  async def test_generic_prepared_ranking_preserves_results_and_materializes_one_query(self):
    await self.seed(1, "job")
    await self.seed(2, "food")
    await self.backfill()
    async with self.db.get_conn() as raw:
      await raw.execute("SET LOCAL plan_cache_mode = force_generic_plan")
      conn = await self.db.archive_conn(raw, self.public)
      q = SearchQuery(123, "job", None, None, None, mode="semantic")
      # Real queries have long decimal vectors, not just [1,0,...]. Repeated
      # text parsing is costly in a generic prepared plan even with one distance.
      v = vector_literal([float(i + 1) for i in range(512)])
      expected = await conn.fetch(ORIGINAL_SEARCH, v, MODEL_ID, [self.public],
                                  123, None, None, None, None, None, 50, 0)
      captured = []
      fetch = ArchiveConnection.fetch
      async def capture(scoped, sql, *args):
        captured.append((sql, args))
        return await fetch(scoped, sql, *args)
      with patch.object(ArchiveConnection, "fetch", capture):
        actual = await search_vectors(conn, q, [self.public], v, 50)
      self.assertEqual([dict(r) for r in actual], [dict(r) for r in expected])
      sql, args = captured[0]
      self.assertEqual(sql.count("$1::text::vector"), 1)
      # This plan checks the materialization boundary; the actual fetch above
      # separately exercises the forced generic prepared execution path.
      plan = json.loads(await conn.fetchval(
        "EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON) " + sql, *args,
      ))[0]["Plan"]
      query_plan = next(n for n in plan["Plans"] if n.get("Subplan Name") == "CTE query_vector")
      self.assertEqual((query_plan["Actual Rows"], query_plan["Actual Loops"]), (1, 1))

  async def test_revalidation_exact_tuple_keys_not_cross_product(self):
    async with self.db.get_conn() as raw:
      topic = await self.db._ensure_conversation(
        raw, "topic", "channel", 123, "Topic", topic_id=42, legacy_group_id=123,
      )
    for cid in (self.public, topic["id"]):
      for year in (2020, 2025):
        for mid in (1, 2):
          await self.seed(mid, cid=cid, year=year)
    await self.backfill()
    async with self.db.get_conn() as raw:
      conn = await self.db.archive_conn(raw, self.public)
      q = SearchQuery(123, "job", None, None, None, mode="semantic")
      rows = await search_vectors(conn, q, [self.public, topic["id"]], vector_literal(vector()), 50)
      keys = [(self.public, 1, datetime.datetime(2020, 1, 2, tzinfo=UTC)),
              (topic["id"], 2, datetime.datetime(2025, 1, 2, tzinfo=UTC))]
      key = lambda r: (r["conversation_id"], r["msgid"], r["created_at"])
      selected = [r for r in rows if key(r) in keys]
      fresh = await revalidate_candidates(conn, q, [self.public, topic["id"]], selected)
      self.assertEqual({key(r) for r in fresh}, set(keys))
      scoped = await revalidate_candidates(conn, q._replace(conversation_id=str(self.public)),
                                          [self.public, topic["id"]], selected)
      self.assertEqual([key(r) for r in scoped], keys[:1])
      self.assertEqual(await revalidate_candidates(conn, q, [], selected), [])
      self.assertEqual(await revalidate_candidates(conn, q, [self.public], []), [])

  async def test_real_plans_project_distance_once_and_lookup_only_candidate_keys(self):
    # Enough rows for normal planner choices (no enable_seqscan/session tuning).
    async with self.db.get_conn() as raw:
      conn = await self.db.archive_conn(raw, self.public)
      # Literal template; ArchiveConnection substitutes only registry UUIDs.
      # pi-lens-ignore: python-sql-injection
      await conn.execute("""
        INSERT INTO {messages} (conversation_id, group_id, msgid, from_user,
          from_user_name, text, created_at)
        SELECT $1, 123, n, 100, 'Sender', 'job ' || n,
          '2025-01-02'::timestamptz FROM generate_series(1, 12000) n
      """, self.public)
      await conn.execute("""
        INSERT INTO {embeddings} (conversation_id, msgid, created_at, model, content_hash, embedding)
        SELECT conversation_id, msgid, created_at, $1, md5(text), $2::text::vector
        FROM {messages}
      """, MODEL_ID, vector_literal(vector()))
      await conn.execute("ANALYZE {messages}")
      await conn.execute("ANALYZE {embeddings}")
      captured = []
      fetch = ArchiveConnection.fetch
      async def capture(scoped, sql, *args):
        captured.append((sql, args))
        return await fetch(scoped, sql, *args)
      q = SearchQuery(123, "job", None, None, None, mode="semantic")
      with patch.object(ArchiveConnection, "fetch", capture):
        rows = await search_vectors(conn, q, [self.public], vector_literal(vector()), 200)
        await revalidate_candidates(conn, q, [self.public], rows)
      plans = []
      for sql, args in captured:
        plan = json.loads(await conn.fetchval(
          "EXPLAIN (ANALYZE, BUFFERS, VERBOSE, FORMAT JSON) " + sql, *args,
        ))[0]
        plans.append(plan)
      # Local artifact for independent inspection, not a production benchmark.
      with open("/tmp/luoxu-search-perf-plans.log", "w") as output:
        json.dump(plans, output, indent=2)
      def nodes(plan):
        yield plan
        for child in plan.get("Plans", []):
          yield from nodes(child)
      ranking = list(nodes(plans[0]["Plan"]))
      self.assertEqual(ranking[0]["Node Type"], "Subquery Scan")
      self.assertTrue(any(n["Node Type"] == "Limit" and n.get("Parent Relationship") == "Subquery"
                          for n in ranking))
      query_plan = next(n for n in ranking if n.get("Subplan Name") == "CTE query_vector")
      self.assertEqual((query_plan["Actual Rows"], query_plan["Actual Loops"]), (1, 1))
      self.assertTrue(any("1" in s and "ranked.distance" in s for s in ranking[0]["Output"]))
      # No projected 1-distance below the bounded subquery, nor vector payload
      # passed out of the ranking join into its sort/limit.
      for node in ranking[1:]:
        output = node.get("Output", [])
        self.assertFalse(any("- (" in s and "<=>" in s for s in output))
        self.assertLessEqual(sum("<=>" in s for s in output), 1)
        if node["Node Type"] in ("Sort", "Limit"):
          self.assertNotIn("e.embedding", output)
      self.assertNotIn("<=>", captured[1][0])
      self.assertNotIn("e.embedding", captured[1][0])
      lookups = [n for n in nodes(plans[1]["Plan"]) if "Relation Name" in n]
      self.assertTrue(lookups)
      for node in lookups:
        self.assertIn(node["Node Type"], ("Index Scan", "Index Only Scan"))
        self.assertLessEqual(node["Actual Rows"], 1)
        self.assertLessEqual(node["Actual Loops"], 200)
        for column in ("conversation_id", "msgid", "created_at"):
          self.assertIn(column, node["Index Cond"])
      # Heap Index Scan's raw tuple slot can list every attribute, including
      # the untouched TOAST pointer. No parent projects/uses the vector payload.
      self.assertNotIn("<=>", json.dumps(plans[1]))
      for node in nodes(plans[1]["Plan"]):
        if "Relation Name" not in node:
          self.assertFalse(any("e.embedding" in s for s in node.get("Output", [])))
      toast_oid = await conn.fetchval(
        "SELECT reltoastrelid FROM pg_class WHERE oid=$1::regclass", self.embeddings,
      )
      await raw.execute("SELECT pg_stat_force_next_flush()")
    # Flush at transaction end and read from a separate backend. The ranking
    # above must actually fetch external vector payloads; the lookup must not.
    async def toast_blocks():
      await self.admin.execute("SELECT pg_stat_clear_snapshot()")
      return await self.admin.fetchrow(
        "SELECT heap_blks_read, heap_blks_hit FROM pg_statio_all_tables WHERE relid=$1",
        toast_oid,
      )
    before = await toast_blocks()
    self.assertGreater(sum(before.values()), 0)
    async with self.db.get_conn() as raw:
      conn = await self.db.archive_conn(raw, self.public)
      fresh = await revalidate_candidates(conn, q, [self.public], rows)
      self.assertEqual(len(fresh), 200)
      await raw.execute("SELECT pg_stat_force_next_flush()")
    after = await toast_blocks()
    self.assertEqual(dict(after), dict(before))
    with open("/tmp/luoxu-search-perf-toast.log", "w") as output:
      json.dump({"before": dict(before), "after": dict(after)}, output)


if __name__ == "__main__":
  unittest.main()
