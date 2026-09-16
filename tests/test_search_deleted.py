"""Opt-in deleted-message search through real HTTP and PostgreSQL, no Telegram I/O."""

import asyncio
import datetime
import unittest
from pathlib import Path

import asyncpg
import test_topics as fixtures
from aiohttp.test_utils import TestClient, TestServer

from luoxu.auth import AuthService
from luoxu.web import setup_app

PREFIX = "/api/luoxu"
UTC = datetime.timezone.utc


@unittest.skipUnless(fixtures.DATABASE_URL, "set LUOXU_TEST_DATABASE_URL")
class DeletedSearchTests(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    self.fixture = fixtures.TopicStorageTests(
      "test_ordinary_reply_ingestion_lists_only_one_group"
    )
    await self.fixture.asyncSetUp()
    self.addAsyncCleanup(self.fixture.asyncTearDown)
    self.db = self.fixture.db
    self.db.history_enabled = True
    self.cid = self.fixture.parent_id
    await self.db.grant_public(self.cid)
    self.auth = AuthService({"jwt_secret": "deleted-search-test-secret-" * 3})
    self.client = await self.api_client()
    self.bounds = {
      "start": str(int(datetime.datetime(2025, 1, 1, tzinfo=UTC).timestamp())),
      "end": str(int(datetime.datetime(2026, 1, 1, tzinfo=UTC).timestamp())),
    }

  async def api_client(self, *, history_enabled=True):
    root = Path(__file__).resolve().parents[1]
    client = TestClient(
      TestServer(
        setup_app(
          self.db,
          None,
          str(root / "tests"),
          str(root / "nobody.jpg"),
          str(root / "ghost.jpg"),
          prefix=PREFIX,
          auth_service=self.auth,
          history_enabled=history_enabled,
        )
      )
    )
    await client.start_server()
    self.addAsyncCleanup(client.close)
    return client

  async def user(self, name, *, admin=False):
    user = await self.db.create_user(name, "unused-test-hash", admin)
    return user, {"Authorization": "Bearer " + self.auth.access_token(user)}

  async def seed(self, mid, text, **kwargs):
    async with self.db.get_conn() as conn:
      await self.fixture.seed_message(conn, self.cid, mid, text=text, **kwargs)

  async def delete(self, *ids):
    await self.db.delete_messages(fixtures.GROUP_ID, list(ids), peer_type="channel")

  async def search(self, params=None, *, headers=None, client=None):
    response = await (client or self.client).get(
      PREFIX + "/search", params={**self.bounds, **(params or {})}, headers=headers
    )
    self.assertEqual(response.status, 200, await response.text())
    return response, await response.json()

  async def test_opt_in_searches_pre_deletion_snapshot_and_keeps_default_live_only(
    self,
  ):
    await self.seed(1, "needle live")
    await self.seed(2, "needle <b>deleted</b>")
    await self.delete(2)
    _, normal = await self.search({"q": "needle"})
    self.assertEqual([row["id"] for row in normal["messages"]], [1])
    response, included = await self.search({"q": "needle", "include_deleted": "true"})
    self.assertEqual([row["id"] for row in included["messages"]], [2, 1])
    deleted, live = included["messages"]
    self.assertTrue(deleted["deleted"])
    self.assertIsNotNone(deleted["deleted_at"])
    self.assertEqual(deleted["content_source"], "delete_snapshot")
    self.assertIsNotNone(deleted["snapshot_captured_at"])
    self.assertIn("&lt;b&gt;deleted&lt;/b&gt;", deleted["html"])
    self.assertIn('class="keyword"', deleted["html"])
    self.assertFalse(live["deleted"])
    self.assertEqual(live["content_source"], "current")
    self.assertIsNone(live["snapshot_captured_at"])
    self.assertEqual(response.headers["Cache-Control"], "private, no-store")
    self.assertEqual(response.headers["Vary"], "Authorization")
    # Opt-in search does not restore the deleted current message representation.
    response = await self.client.get(f"{PREFIX}/conversations/{self.cid}/messages/2")
    self.assertEqual(response.status, 200)
    current = (await response.json())["message"]
    self.assertTrue(current["deleted"])
    self.assertIsNone(current["text"])
    self.assertIsNone(current["html"])

  async def test_history_switches_fail_closed_but_default_search_still_works(self):
    await self.seed(1, "needle live")
    await self.seed(2, "needle deleted-secret")
    await self.delete(2)
    disabled = await self.api_client(history_enabled=False)
    for client in (disabled, self.client):
      if client is self.client:
        self.db.history_enabled = False
      response = await client.get(
        PREFIX + "/search", params={**self.bounds, "include_deleted": "true"}
      )
      self.assertEqual(response.status, 400, await response.text())
      self.assertNotIn("deleted-secret", await response.text())
      _, normal = await self.search({"q": "needle"}, client=client)
      self.assertEqual([r["id"] for r in normal["messages"]], [1])

  async def test_boolean_validation_and_explicit_false(self):
    await self.seed(1, "needle")
    await self.delete(1)
    for flag in ("", "1", "0", "yes", "no", "tru", " false "):
      response = await self.client.get(
        PREFIX + "/search", params={"include_deleted": flag}
      )
      self.assertEqual(response.status, 400, (flag, await response.text()))
    for flag in ("false", "FALSE"):
      _, body = await self.search({"include_deleted": flag})
      self.assertEqual(body["messages"], [])
    _, body = await self.search({"include_deleted": "TRUE"})
    self.assertEqual([r["id"] for r in body["messages"]], [1])
    # include_history belongs to /history; it does not opt search into snapshots.
    _, body = await self.search({"include_history": "true", "q": "needle"})
    self.assertEqual(body["messages"], [])

  async def test_missing_delete_snapshot_never_uses_edit_history_or_residual_text(self):
    await self.seed(1, "needle old edit")
    message = fixtures.message(1)
    message.message = "needle updated"
    message.edit_date = datetime.datetime(2025, 1, 3, tzinfo=UTC)
    token = fixtures.msg_source.set("editmsg")
    try:
      await self.db.insert_messages(
        [message], fixtures.UpdateLoaded.update_none, use_ocr=False
      )
    finally:
      fixtures.msg_source.reset(token)
    self.db.history_enabled = False
    await self.delete(1)
    self.db.history_enabled = True
    await self.seed(2, "needle residual text", deleted=datetime.datetime.now(UTC))
    _, body = await self.search({"include_deleted": "true"})
    self.assertEqual([r["id"] for r in body["messages"]], [2, 1])
    for row in body["messages"]:
      self.assertTrue(row["deleted"])
      self.assertIsNone(row["html"])
      self.assertEqual(row["content_source"], "unavailable")
      self.assertIsNone(row["snapshot_captured_at"])
    _, body = await self.search({"include_deleted": "true", "q": "needle"})
    self.assertEqual(body["messages"], [])

  async def test_public_personal_grants_admin_non_bypass_and_immediate_revocation(self):
    alice, alice_headers = await self.user("alice")
    bob, bob_headers = await self.user("bob")
    _, admin_headers = await self.user("administrator", admin=True)
    await self.seed(1, "needle public")
    await self.delete(1)
    private = []
    for offset, owner in ((1, alice), (2, bob)):
      peer = fixtures.GROUP_ID + offset
      async with self.db.get_conn() as conn:
        group = await self.db.insert_group(conn, fixtures.channel(peer))
        cid = group["conversation_uuid"]
        await self.fixture.seed_message(
          conn, cid, 1, text=f"needle private-{offset}", group_id=peer
        )
      await self.db.delete_messages(peer, [1], peer_type="channel")
      await self.db.grant_conversation(owner["id"], cid)
      private.append(cid)
    query = {"include_deleted": "true", "q": "needle"}
    for headers, expected in (
      (None, {str(self.cid)}),
      (alice_headers, {str(self.cid), str(private[0])}),
      (bob_headers, {str(self.cid), str(private[1])}),
      (admin_headers, {str(self.cid)}),
    ):
      _, body = await self.search(query, headers=headers)
      self.assertEqual({r["conversation_id"] for r in body["messages"]}, expected)
      self.assertTrue(all(r["deleted"] for r in body["messages"]))
    for headers in (None, bob_headers, admin_headers):
      _, body = await self.search(
        {**query, "conversation_id": str(private[0])}, headers=headers
      )
      self.assertEqual(body["messages"], [])
      response = await self.client.get(
        PREFIX + "/search",
        params={**query, "g": fixtures.GROUP_ID + 1},
        headers=headers,
      )
      self.assertEqual(response.status, 404)
    await self.db.revoke_conversation(alice["id"], private[0])
    _, body = await self.search(query, headers=alice_headers)
    self.assertEqual({r["conversation_id"] for r in body["messages"]}, {str(self.cid)})
    await self.db.revoke_public(self.cid)
    for headers in (None, alice_headers, admin_headers):
      _, body = await self.search(query, headers=headers)
      self.assertEqual(body["messages"], [])

  async def test_topic_only_grant_does_not_expose_parent_or_sibling_snapshots(self):
    await self.db.revoke_public(self.cid)
    user, headers = await self.user("topic-reader")
    await self.seed(1, "needle parent")
    async with self.db.get_conn() as conn:
      allowed = await self.fixture.seed_topic(conn, 20)
      hidden = await self.fixture.seed_topic(conn, 30)
      await self.fixture.seed_message(
        conn, allowed, 2, text="needle allowed", topic_id=20
      )
      await self.fixture.seed_message(
        conn, hidden, 3, text="needle hidden", topic_id=30
      )
    await self.delete(1, 2, 3)
    await self.db.grant_conversation(user["id"], allowed)
    query = {"include_deleted": "true", "q": "needle"}
    _, body = await self.search(query, headers=headers)
    self.assertEqual([r["conversation_id"] for r in body["messages"]], [str(allowed)])
    await self.db.revoke_conversation(user["id"], allowed)
    _, body = await self.search(query, headers=headers)
    self.assertEqual(body["messages"], [])
    await self.db.grant_conversation(user["id"], self.cid)
    _, body = await self.search(query, headers=headers)
    self.assertEqual(
      {r["conversation_id"] for r in body["messages"]},
      {str(self.cid), str(allowed), str(hidden)},
    )

  async def test_private_chat_snapshots_require_an_individual_grant(self):
    user, headers = await self.user("private-reader")
    _, admin_headers = await self.user("administrator", admin=True)
    async with self.db.get_conn() as conn:
      private = await self.db._ensure_conversation(
        conn, "private_chat", "user", 777, "Private chat"
      )
      cid = private["id"]
      await self.fixture.seed_message(
        conn, cid, 1, text="needle private", group_id=None
      )
    await self.db.delete_messages(777, [1], kind="private_chat", peer_type="user")
    query = {"include_deleted": "true", "q": "needle"}
    for identity in (None, headers, admin_headers):
      _, body = await self.search(query, headers=identity)
      self.assertEqual(body["messages"], [])
    await self.db.grant_conversation(user["id"], cid)
    _, body = await self.search(query, headers=headers)
    self.assertEqual([r["conversation_id"] for r in body["messages"]], [str(cid)])
    self.assertIn("private", body["messages"][0]["html"])
    self.assertTrue(body["messages"][0]["deleted"])
    self.assertEqual(body["messages"][0]["content_source"], "delete_snapshot")
    await self.db.revoke_conversation(user["id"], cid)
    _, body = await self.search(query, headers=headers)
    self.assertEqual(body["messages"], [])

  async def test_sender_group_conversation_and_time_filters_apply_before_limit(self):
    for mid in range(1, 55):
      await self.seed(mid, f"needle message {mid}")
    async with self.db.get_conn() as conn:
      await conn.execute(
        """UPDATE messages SET from_user = CASE msgid
          WHEN 1 THEN 101 WHEN 3 THEN NULL WHEN 4 THEN 100 ELSE 202 END
          WHERE conversation_id = $1""",
        self.cid,
      )
    await self.delete(1, 2, 3, *range(5, 55))
    query = {"include_deleted": "true", "q": "needle"}
    _, body = await self.search({**query, "exclude_sender": "202"})
    self.assertEqual([r["id"] for r in body["messages"]], [4, 3, 1])
    _, body = await self.search(
      {
        **query,
        "g": fixtures.GROUP_ID,
        "conversation_id": str(self.cid),
        "sender": "101,202",
        "exclude_sender": "202",
      }
    )
    self.assertEqual([r["id"] for r in body["messages"]], [1])
    _, body = await self.search(
      {
        **query,
        "start": str(int(datetime.datetime(2025, 1, 3, tzinfo=UTC).timestamp())),
      }
    )
    self.assertEqual(body["messages"], [])

  async def test_combined_page_is_sorted_and_capped_across_live_and_deleted(self):
    for mid in range(1, 61):
      await self.seed(mid, f"needle message {mid}")
    await self.delete(*range(2, 61, 2))
    _, body = await self.search({"include_deleted": "true", "q": "needle"})
    self.assertEqual([r["id"] for r in body["messages"]], list(range(60, 10, -1)))
    self.assertTrue(body["has_more"])
    self.assertEqual(sum(r["deleted"] for r in body["messages"]), 25)

  async def test_snapshot_selection_and_highlights_preserve_physical_year_variant(self):
    await self.seed(1, "needle older", year=2024)
    await self.seed(1, "needle newer", year=2025)
    query = {
      "q": "needle",
      "start": str(int(datetime.datetime(2024, 1, 1, tzinfo=UTC).timestamp())),
    }
    # Live highlights must not accidentally read another year's same numeric ID.
    _, body = await self.search(query)
    self.assertEqual(len(body["messages"]), 2)
    self.assertIn("newer", body["messages"][0]["html"])
    self.assertIn("older", body["messages"][1]["html"])
    await self.delete(1)
    # Deletion captures only the latest variant. The older physical row must
    # not borrow its snapshot, even though both rows share conversation and ID.
    _, body = await self.search({**query, "include_deleted": "true"})
    self.assertEqual(len(body["messages"]), 1)
    self.assertIn("newer", body["messages"][0]["html"])
    _, body = await self.search({"start": query["start"], "include_deleted": "true"})
    self.assertEqual(len(body["messages"]), 2)
    self.assertEqual(body["messages"][1]["content_source"], "unavailable")
    self.assertIsNone(body["messages"][1]["html"])

  async def test_concurrent_deletion_does_not_duplicate_a_search_result(self):
    await self.seed(1, "needle concurrent")
    selected = asyncio.Event()
    resume = asyncio.Event()

    class PausingConnection(asyncpg.Connection):
      async def fetch(self, query, *args, **kwargs):
        rows = await super().fetch(query, *args, **kwargs)
        # Pause only the external DB driver's first completed live-message
        # read. Every query and the concurrent deletion still run on real PG.
        if rows and "deleted_at IS NULL" in query and not selected.is_set():
          selected.set()
          await resume.wait()
        return rows

    pool = self.db.pool
    assert pool is not None
    await pool.close()
    self.db.pool = await asyncpg.create_pool(
      fixtures.DATABASE_URL,
      min_size=1,
      max_size=2,
      connection_class=PausingConnection,
      server_settings={"search_path": f"{self.fixture.schema},public"},
    )
    request = asyncio.create_task(
      self.search({"include_deleted": "true", "q": "needle"})
    )
    try:
      await asyncio.wait_for(selected.wait(), timeout=5)
      await asyncio.wait_for(self.delete(1), timeout=5)
    finally:
      resume.set()
      _, body = await asyncio.wait_for(request, timeout=5)
    self.assertEqual([r["id"] for r in body["messages"]], [1])
    self.assertFalse(body["messages"][0]["deleted"])
    _, after = await self.search({"include_deleted": "true", "q": "needle"})
    self.assertEqual([r["id"] for r in after["messages"]], [1])
    self.assertTrue(after["messages"][0]["deleted"])

  async def test_each_saved_year_uses_its_own_delete_snapshot(self):
    await self.seed(1, "needle older", year=2024)
    await self.delete(1)
    await self.seed(1, "needle newer", year=2025)
    await self.delete(1)
    _, body = await self.search(
      {
        "q": "needle",
        "include_deleted": "true",
        "start": str(int(datetime.datetime(2024, 1, 1, tzinfo=UTC).timestamp())),
      }
    )
    self.assertEqual(len(body["messages"]), 2)
    self.assertIn("newer", body["messages"][0]["html"])
    self.assertIn("older", body["messages"][1]["html"])
    self.assertTrue(
      all(r["content_source"] == "delete_snapshot" for r in body["messages"])
    )

  async def test_latest_delete_snapshot_wins_and_edit_revisions_are_not_searchable(
    self,
  ):
    await self.seed(1, "", deleted=datetime.datetime.now(UTC))
    created = datetime.datetime(2025, 1, 2, tzinfo=UTC)
    captured = datetime.datetime(2025, 1, 3, tzinfo=UTC)
    async with self.db.get_conn() as conn:
      # Competing snapshots can survive archive repair/import. A tied capture
      # timestamp must still deterministically choose the newer delete record.
      for kind, text, moment in (
        ("delete", "needle oldsnapshot", captured - datetime.timedelta(days=1)),
        ("delete", "needle tiedloser", captured),
        ("delete", "needle winner", captured),
        ("edit", "needle neweredit", captured + datetime.timedelta(days=1)),
      ):
        # Literal fixture SQL; every variable value uses a $1-$5 bind parameter.
        # pi-lens-ignore: python-sql-injection
        await conn.execute(
          """INSERT INTO message_revisions
            (conversation_id, msgid, revision_type, text, from_user_name,
             created_at, captured_at)
            VALUES ($1, 1, $2, $3, 'Sender', $4, $5)""",
          self.cid,
          kind,
          text,
          created,
          moment,
        )
    _, body = await self.search({"include_deleted": "true", "q": "needle"})
    self.assertEqual(len(body["messages"]), 1)
    self.assertIn("winner", body["messages"][0]["html"])
    self.assertEqual(body["messages"][0]["snapshot_captured_at"], captured.timestamp())
    for term in ("oldsnapshot", "tiedloser", "neweredit"):
      _, body = await self.search({"include_deleted": "true", "q": term})
      self.assertEqual(body["messages"], [])
