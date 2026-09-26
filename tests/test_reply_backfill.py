"""Offline reply-header maintenance against the disposable PostgreSQL fixture."""

import asyncio
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import test_topics as fixtures
from telethon.errors import FloodWaitError, RPCError
from telethon.tl import types

from luoxu.auth import Principal
from luoxu.backfill_replies import BackfillError, main, run_backfill


@unittest.skipUnless(fixtures.DATABASE_URL, "set LUOXU_TEST_DATABASE_URL")
class ReplyBackfillTests(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    self.fixture = fixtures.TopicStorageTests(
      "test_ordinary_reply_ingestion_lists_only_one_group"
    )
    await self.fixture.asyncSetUp()
    self.addAsyncCleanup(self.fixture.asyncTearDown)
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.state = Path(self.tmp.name) / "reply-state.json"
    self.db = self.fixture.db
    self.cid = self.fixture.parent_id
    await self.db.grant_public(self.cid)
    async with self.db.get_conn() as conn:
      for msgid in (1, 2, 3):
        await self.fixture.seed_message(conn, self.cid, msgid)
    self.client = AsyncMock()
    self.client.get_me.return_value = types.User(id=700, first_name="Maintainer")
    self.client.get_entity.return_value = fixtures.channel()
    self.client.get_messages.side_effect = self.fetch

  async def fetch(self, entity, *, ids):
    return [fixtures.message(i, reply_id=None if i == 1 else 1) for i in ids]

  async def run_job(self, **kwargs):
    return await run_backfill(self.db, self.client, self.cid, self.state, **kwargs)

  async def archived(self, msgid):
    row = await self.db.get_message(self.cid, msgid, Principal(None, None))
    assert row is not None
    return row

  def checkpoint(self):
    return json.loads(self.state.read_text())

  async def test_backfill_routes_only_to_the_selected_peer_archive(self):
    peer = fixtures.GROUP_ID + 1
    async with self.db.get_conn() as conn:
      other = await self.db.insert_group(conn, fixtures.channel(peer))
      other_id = other["conversation_uuid"]
      for msgid in (2, 99):
        await self.fixture.seed_message(conn, other_id, msgid, group_id=peer)
    await self.db.grant_public(other_id)
    result = await self.run_job(apply=True)
    self.assertEqual((result["through_id"], result["filled"]), (3, 2))
    for msgid in (2, 99):
      row = await self.db.get_message(other_id, msgid, Principal(None, None))
      assert row is not None
      self.assertIsNone(row["reply_to_id"])
    self.assertEqual(await self.db.reply_backfill_candidates(other_id, 0, 99, 100), [2, 99])

  async def test_backfill_waits_for_peer_writers_and_rechecks_deletion(self):
    async with (
      asyncio.timeout(10),
      asyncio.TaskGroup() as tasks,
      self.db.get_conn() as conn,
    ):
      # A shared peer lock must also block this exclusive metadata writer.
      await self.db._lock_peer_writes(conn, fixtures.GROUP_ID)
      task = tasks.create_task(self.db.backfill_reply_ids(self.cid, [fixtures.message(2)]))
      await self.fixture.wait_for_blocked_writer(conn.get_server_pid())
      scoped = await self.db.archive_conn(conn, self.cid)
      await scoped.execute(
        "UPDATE {messages} SET deleted_at = now(), text = '' "
        "WHERE conversation_id = $1 AND msgid = 2", self.cid,
      )
    self.assertEqual(task.result(), 0)
    row = await self.archived(2)
    self.assertIsNotNone(row["deleted_at"])
    self.assertIsNone(row["reply_to_id"])
    self.assertEqual(row["text"], "")

  async def test_preview_is_one_batch_without_database_or_checkpoint_writes(self):
    before = [dict(await self.archived(i)) for i in (1, 2, 3)]
    async with self.db.get_conn() as conn:
      tables_before = await self.unchanged_tables(conn)
    result = await self.run_job(batch_size=2)
    self.assertEqual(result["scanned"], 2)
    self.assertEqual(result["filled"], 1)
    self.assertTrue(result["dry_run"])
    self.assertFalse(self.state.exists())
    self.assertEqual([dict(await self.archived(i)) for i in (1, 2, 3)], before)
    async with self.db.get_conn() as conn:
      self.assertEqual(await self.unchanged_tables(conn), tables_before)
    self.client.get_messages.assert_awaited_once()

  async def test_committed_batch_resumes_its_fixed_range(self):
    first = await self.run_job(apply=True, batch_size=2, max_batches=1)
    self.assertEqual(first["last_id"], 2)
    self.assertFalse(first["complete"])
    self.assertEqual(self.checkpoint()["last_id"], 2)
    self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o600)
    self.assertEqual((await self.archived(2))["reply_to_id"], 1)
    async with self.db.get_conn() as conn:
      await self.fixture.seed_message(conn, self.cid, 4)
    resumed = await self.run_job(apply=True)
    self.assertTrue(resumed["complete"])
    self.assertEqual(resumed["through_id"], 3)
    self.assertEqual((await self.archived(3))["reply_to_id"], 1)
    self.assertIsNone((await self.archived(4))["reply_to_id"])
    self.assertEqual(self.client.get_messages.await_args.kwargs["ids"], [3])

  async def test_missing_messages_are_unavailable_not_roots_and_need_a_new_state(self):
    self.client.get_messages.side_effect = None
    self.client.get_messages.return_value = [
      None,
      types.MessageEmpty(id=2, peer_id=types.PeerChannel(fixtures.GROUP_ID)),
      fixtures.message(3),
    ]
    result = await self.run_job(apply=True)
    self.assertEqual(result["unavailable"], 2)
    self.assertEqual(result["filled"], 1)
    self.assertTrue(result["complete"])
    self.assertIsNone((await self.archived(2))["reply_to_id"])
    self.assertEqual((await self.archived(3))["reply_to_id"], 1)
    calls = self.client.get_messages.await_count
    await self.run_job(apply=True)
    self.assertEqual(self.client.get_messages.await_count, calls)
    self.state = self.state.with_name("retry-unavailable.json")
    self.client.get_messages.side_effect = self.fetch
    retried = await self.run_job(apply=True)
    self.assertEqual(retried["filled"], 1)
    self.assertEqual((await self.archived(2))["reply_to_id"], 1)

  async def test_foreign_duplicate_unrequested_or_invalid_response_rejects_whole_batch(
    self,
  ):
    variants = [
      [
        fixtures.message(2),
        fixtures.message(3, peer=types.PeerChannel(fixtures.GROUP_ID + 1)),
      ],
      [
        fixtures.message(2),
        fixtures.message(3, peer=types.PeerChat(fixtures.GROUP_ID)),
      ],
      [fixtures.message(2), fixtures.message(2)],
      [
        fixtures.message(2),
        types.MessageEmpty(id=2, peer_id=types.PeerChannel(fixtures.GROUP_ID)),
      ],
      [fixtures.message(2), fixtures.message(99)],
      [fixtures.message(2), types.User(id=3)],
      None,
    ]
    self.client.get_messages.side_effect = None
    for response in variants:
      with self.subTest(response_type=type(response).__name__):
        self.client.get_messages.return_value = response
        with self.assertRaises(BackfillError):
          await self.run_job(apply=True)
        self.assertEqual(self.checkpoint()["last_id"], 0)
        self.assertFalse(self.checkpoint()["complete"])
        self.assertIsNone((await self.archived(2))["reply_to_id"])

  async def test_rpc_error_cancellation_and_timeout_leave_cursor_unchanged(self):
    for failure in (
      RPCError(None, "permission denied", 403),
      asyncio.CancelledError(),
      TimeoutError(),
    ):
      with self.subTest(failure=type(failure).__name__):
        self.client.get_messages.side_effect = failure
        with self.assertRaises(type(failure)):
          await self.run_job(apply=True)
        self.assertEqual(self.checkpoint()["last_id"], 0)
        self.assertIsNone((await self.archived(2))["reply_to_id"])
    self.client.get_messages.side_effect = self.fetch
    self.assertTrue((await self.run_job(apply=True))["complete"])

  async def test_flood_wait_is_capped_and_retries_are_finite(self):
    self.client.get_messages.side_effect = FloodWaitError(None, capture=301)
    with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
      with self.assertRaises(BackfillError):
        await self.run_job(apply=True)
      sleep.assert_not_awaited()
    self.assertEqual(self.checkpoint()["last_id"], 0)
    self.client.get_messages.reset_mock()
    self.client.get_messages.side_effect = FloodWaitError(None, capture=2)
    with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
      with self.assertRaises(BackfillError):
        await self.run_job(apply=True, max_flood_wait=2)
      self.assertEqual(sleep.await_count, 3)
      self.assertTrue(all(call.args == (2,) for call in sleep.await_args_list))
    self.assertEqual(self.client.get_messages.await_count, 4)
    self.assertEqual(self.checkpoint()["last_id"], 0)
    self.assertIsNone((await self.archived(2))["reply_to_id"])

  async def test_flood_retry_and_rate_limit_do_not_fetch_parents(self):
    self.client.get_messages.side_effect = [
      FloodWaitError(None, capture=1),
      [fixtures.message(2)],
      [fixtures.message(3)],
    ]
    with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
      result = await self.run_job(apply=True, after_id=1, batch_size=1, delay=2)
      self.assertEqual([call.args for call in sleep.await_args_list], [(1,), (2,)])
    self.assertEqual(result["filled"], 2)
    self.assertEqual(
      [call.kwargs["ids"] for call in self.client.get_messages.await_args_list],
      [[2], [2], [3]],
    )
    peer = self.client.get_entity.await_args.args[0]
    self.assertIsInstance(peer, types.PeerChannel)
    self.assertEqual(peer.channel_id, fixtures.GROUP_ID)
    self.client.download_media.assert_not_called()

  async def test_cancellation_while_waiting_does_not_skip_the_next_batch(self):
    with (
      patch(
        "asyncio.sleep", new_callable=AsyncMock, side_effect=asyncio.CancelledError()
      ),
      self.assertRaises(asyncio.CancelledError),
    ):
      await self.run_job(apply=True, after_id=1, batch_size=1)
    self.assertEqual(self.checkpoint()["last_id"], 2)
    self.assertEqual((await self.archived(2))["reply_to_id"], 1)
    self.assertIsNone((await self.archived(3))["reply_to_id"])
    await self.run_job(apply=True, after_id=1)
    self.assertEqual((await self.archived(3))["reply_to_id"], 1)

  async def test_crash_after_commit_before_atomic_checkpoint_is_idempotent(self):
    prior = self.state.with_suffix(".prior")

    async def obstruct_checkpoint(entity, *, ids):
      # A real filesystem fault, not a mocked database/save function: the initial
      # cursor survives separately, but atomic replacement fails after DB commit.
      self.state.rename(prior)
      self.state.mkdir()
      return await self.fetch(entity, ids=ids)

    self.client.get_messages.side_effect = obstruct_checkpoint
    with self.assertRaises(OSError):
      await self.run_job(apply=True, batch_size=2)
    self.assertEqual((await self.archived(2))["reply_to_id"], 1)
    self.assertEqual(json.loads(prior.read_text())["last_id"], 0)
    before = dict(await self.archived(2))
    self.state.rmdir()
    prior.rename(self.state)
    self.client.get_messages.side_effect = self.fetch
    result = await self.run_job(apply=True)
    self.assertEqual(result["filled"], 1)
    self.assertEqual(dict(await self.archived(2)), before)
    self.assertEqual(self.client.get_messages.await_args.kwargs["ids"], [1, 3])
    self.assertTrue(result["complete"])

  async def test_checkpoint_rejects_replayed_range_account_and_peer(self):
    await self.run_job(apply=True, batch_size=1, max_batches=1)
    original = self.state.read_bytes()
    for options in ({"after_id": 1}, {"through_id": 4}):
      with self.subTest(options=options), self.assertRaises(BackfillError):
        await self.run_job(apply=True, **options)
      self.assertEqual(self.state.read_bytes(), original)
    self.client.get_me.return_value = types.User(id=701)
    with self.assertRaises(BackfillError):
      await self.run_job(apply=True)
    self.assertEqual(self.state.read_bytes(), original)
    self.client.get_me.return_value = types.User(id=700)
    self.client.get_entity.return_value = fixtures.channel(fixtures.GROUP_ID + 1)
    with self.assertRaises(BackfillError):
      await self.run_job(apply=True)
    self.assertEqual(self.state.read_bytes(), original)
    self.client.get_entity.return_value = fixtures.channel()
    state = self.checkpoint()
    state["telegram_peer_type"] = "chat"
    self.state.write_text(json.dumps(state))
    changed = self.state.read_bytes()
    with self.assertRaises(BackfillError):
      await self.run_job(apply=True)
    self.assertEqual(self.state.read_bytes(), changed)

  async def test_malformed_or_inconsistent_checkpoint_is_not_overwritten(self):
    await self.run_job(apply=True, batch_size=1, max_batches=1)
    good = self.checkpoint()
    invalid = [
      "{broken",
      "[]",
      json.dumps({**good, "last_id": 999}),
      json.dumps({**good, "version": True}),
      json.dumps({**good, "complete": 1}),
      json.dumps({**good, "account_id": False}),
      json.dumps({**good, "extra": 1}),
      json.dumps(good)[:-1] + ', "version": 1}',
    ]
    for text in invalid:
      with self.subTest(checkpoint=text):
        self.state.write_text(text)
        with self.assertRaises(BackfillError):
          await self.run_job(apply=True)
        self.assertEqual(self.state.read_text(), text)

  async def test_nonblocking_checkpoint_lock_prevents_second_job(self):
    import fcntl

    lock = Path(str(self.state) + ".lock")
    with lock.open("w") as stream:  # noqa: ASYNC230 -- real nonblocking lock fixture.
      fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
      with self.assertRaisesRegex(BackfillError, "locked"):
        await self.run_job(apply=True)
    self.assertFalse(self.state.exists())
    self.client.get_me.assert_not_awaited()
    await self.run_job(apply=True)
    self.assertTrue(self.checkpoint()["complete"])

  async def test_dry_run_of_existing_checkpoint_preserves_bytes_and_only_previews_one_batch(
    self,
  ):
    await self.run_job(apply=True, batch_size=1, max_batches=1)
    before = self.state.read_bytes()
    result = await self.run_job(batch_size=1)
    self.assertEqual(result["filled"], 1)
    self.assertEqual(result["scanned"], 1)
    self.assertEqual(self.state.read_bytes(), before)
    self.assertIsNone((await self.archived(2))["reply_to_id"])

  async def test_exact_group_only_does_not_visit_topics_or_change_other_tables(self):
    async with self.db.get_conn() as conn:
      topic_id = await self.fixture.seed_topic(conn, 42)
      await self.fixture.seed_message(conn, topic_id, 4, topic_id=42)
      before = await self.unchanged_tables(conn)
      conn = await self.db.archive_conn(conn, topic_id)
      topic_before = dict(
        await conn.fetchrow(
          "SELECT * FROM {messages} WHERE conversation_id = $1", topic_id
        )
      )
    with self.assertRaises(BackfillError):
      await run_backfill(self.db, self.client, topic_id, self.state, apply=True)
    self.assertFalse(self.state.exists())
    messages_before = [dict(await self.archived(i)) for i in (1, 2, 3)]
    await self.run_job(apply=True)
    for msgid, archived in enumerate(messages_before, 1):
      actual = dict(await self.archived(msgid))
      actual.pop("reply_to_id")
      archived.pop("reply_to_id")
      self.assertEqual(actual, archived)
    async with self.db.get_conn() as conn:
      self.assertEqual(await self.unchanged_tables(conn), before)
      conn = await self.db.archive_conn(conn, topic_id)
      self.assertEqual(
        dict(
          await conn.fetchrow(
            "SELECT * FROM {messages} WHERE conversation_id = $1", topic_id
          )
        ),
        topic_before,
      )
    self.assertEqual(self.client.get_messages.await_args.kwargs["ids"], [1, 2, 3])
    async with self.fixture.api_client() as client:
      response = await client.get(
        f"/api/luoxu/conversations/{self.cid}/messages/2/context?before=0&after=0"
      )
      self.assertEqual(response.status, 200)
      payload = await response.json()
      self.assertIn(1, [message["id"] for message in payload["replies"]])

  async def unchanged_tables(self, conn):
    # Fixed test-only SQL, not operator input: snapshots catch cursor/ACL drift.
    # pi-lens-ignore: python-sql-injection
    return {
      "groups": [
        dict(row)
        for row in await conn.fetch("SELECT * FROM tg_groups ORDER BY group_id")
      ],
      "conversations": [
        dict(row) for row in await conn.fetch("SELECT * FROM conversations ORDER BY id")
      ],
      "revisions": [
        dict(row)
        for row in await conn.fetch("SELECT * FROM message_revisions ORDER BY id")
      ],
      "public_access": [
        dict(row)
        for row in await conn.fetch(
          "SELECT * FROM public_conversation_access ORDER BY conversation_id"
        )
      ],
      "personal_access": [
        dict(row)
        for row in await conn.fetch(
          "SELECT * FROM conversation_access ORDER BY user_id, conversation_id"
        )
      ],
      "monitoring": [
        dict(row)
        for row in await conn.fetch(
          "SELECT * FROM group_monitoring ORDER BY conversation_id"
        )
      ],
    }

  async def test_basic_chat_entity_is_resolved_by_type_and_backfilled(self):
    entity = types.Chat(
      id=456,
      title="Basic group",
      photo=types.ChatPhotoEmpty(),
      participants_count=2,
      date=fixtures.datetime.datetime(2025, 1, 2, tzinfo=fixtures.UTC),
      version=1,
    )
    async with self.db.get_conn() as conn:
      row = await self.db.insert_group(conn, entity)
      cid = row["conversation_uuid"]
      await self.fixture.seed_message(conn, cid, 2, group_id=456)
    await self.db.grant_public(cid)
    self.client.get_entity.return_value = entity
    self.client.get_messages.side_effect = None
    self.client.get_messages.return_value = [
      fixtures.message(2, peer=types.PeerChat(456))
    ]
    result = await run_backfill(self.db, self.client, cid, self.state, apply=True)
    self.assertEqual(result["filled"], 1)
    self.assertEqual(self.checkpoint()["telegram_peer_type"], "chat")
    peer = self.client.get_entity.await_args.args[0]
    self.assertIsInstance(peer, types.PeerChat)
    self.assertEqual(peer.chat_id, 456)
    message = await self.db.get_message(cid, 2, Principal(None, None))
    assert message is not None
    self.assertEqual(message["reply_to_id"], 1)

  async def test_empty_fixed_range_completes_without_requesting_messages(self):
    result = await self.run_job(apply=True, after_id=3, through_id=3)
    self.assertTrue(result["complete"])
    self.assertEqual(result["scanned"], 0)
    self.assertEqual(self.checkpoint()["last_id"], 3)
    self.client.get_messages.assert_not_awaited()

  async def cli(self):
    # Real configuration, PostgreStore/pool and CLI entrypoint; replace only the
    # external Telegram transport constructor. The DSN selects this test schema.
    database_url = fixtures.DATABASE_URL
    assert database_url is not None
    parts = urlsplit(database_url)
    query = dict(parse_qsl(parts.query))
    query["search_path"] = self.fixture.schema + ",public"
    dsn = urlunsplit(parts._replace(query=urlencode(query)))
    config = Path(self.tmp.name) / "config.toml"
    config.write_text(
      "[database]\nurl = "
      + json.dumps(dsn)
      + '\nocr_url = "https://must-not-contact.invalid/"\n'
      + '[telegram]\napi_id = 123\napi_hash = "test-only"\nsession_db = "unused-test-session"\n'
    )
    args = [
      "reply-backfill",
      "--config",
      str(config),
      "--conversation-id",
      str(self.cid),
      "--state-file",
      str(self.state),
      "--apply",
    ]
    with (
      patch("sys.argv", args),
      patch("sys.stdout", new_callable=io.StringIO) as output,
      patch("luoxu.util.TelegramClient", return_value=self.client),
      patch("aiohttp.ClientSession", side_effect=AssertionError("OCR is forbidden")),
    ):
      status = await asyncio.to_thread(main)
    return status, output.getvalue()

  async def test_cli_uses_existing_session_and_closes_transport_without_ocr(self):
    self.client.is_user_authorized.return_value = True
    status, output = await self.cli()
    self.assertEqual(status, 0)
    self.assertEqual(json.loads(output)["filled"], 2)
    self.assertEqual((await self.archived(2))["reply_to_id"], 1)
    self.client.connect.assert_awaited_once()
    self.client.disconnect.assert_awaited_once()
    self.client.start.assert_not_called()
    self.client.sign_in.assert_not_called()

  async def test_cli_redacts_remote_errors_and_always_disconnects(self):
    self.client.is_user_authorized.return_value = True
    self.client.get_messages.side_effect = RPCError(None, "sensitive-test-body", 403)
    with self.assertLogs("luoxu.backfill_replies", level="ERROR") as logs:
      status, output = await self.cli()
    self.assertEqual(status, 1)
    self.assertEqual(output, "")
    self.assertNotIn("sensitive-test-body", "\n".join(logs.output))
    self.assertEqual(self.checkpoint()["last_id"], 0)
    self.client.disconnect.assert_awaited_once()

  async def test_cli_unauthorized_session_never_starts_interactive_login(self):
    self.client.is_user_authorized.return_value = False
    with self.assertLogs("luoxu.backfill_replies", level="ERROR"):
      status, output = await self.cli()
    self.assertEqual(status, 1)
    self.assertEqual(output, "")
    self.assertFalse(self.state.exists())
    self.client.disconnect.assert_awaited_once()
    self.client.start.assert_not_called()
    self.client.sign_in.assert_not_called()
    self.client.get_messages.assert_not_called()

  async def test_invalid_options_fail_before_transport_or_state_creation(self):
    cases = [
      {"batch_size": 0},
      {"batch_size": 101},
      {"batch_size": True},
      {"delay": 0},
      {"delay": -1},
      {"delay": float("nan")},
      {"delay": float("inf")},
      {"delay": True},
      {"delay": 3601},
      {"after_id": -1},
      {"after_id": 1.2},
      {"after_id": True},
      {"after_id": 2, "through_id": 1},
      {"through_id": True},
      {"max_batches": 0},
      {"max_batches": False},
      {"max_flood_wait": -1},
      {"max_flood_wait": 1.2},
      {"apply": "yes"},
    ]
    for options in cases:
      with self.subTest(options=options), self.assertRaises(BackfillError):
        await self.run_job(**options)
    self.assertFalse(self.state.exists())
    self.client.get_me.assert_not_awaited()
