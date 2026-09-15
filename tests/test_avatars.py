"""Avatar HTTP regressions without Telegram or database network access."""

import asyncio
import datetime
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp.test_utils import TestClient, TestServer
from telethon.client.downloads import DownloadMethods
from telethon.errors.rpcerrorlist import ChannelPrivateError
from telethon.tl.types import User, UserProfilePhoto

from luoxu.web import AvatarHandler, setup_app

PHOTO = b"\xff\xd8downloaded-avatar\xff\xd9"
DEFAULT = b"\xff\xd8default-avatar\xff\xd9"
GHOST = b"\xff\xd8ghost-avatar\xff\xd9"
PREFIX = "/api/luoxu"


def user(uid=42, photo_id=420):
    return User(
        id=uid,
        first_name="Sender",
        username=f"sender{uid}",
        photo=UserProfilePhoto(photo_id=photo_id, dc_id=1),
    )


class AvatarTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.default = self.root / "nobody.jpg"
        self.default.write_bytes(DEFAULT)
        self.ghost = self.root / "ghost.jpg"
        self.ghost.write_bytes(GHOST)
        self.telegram = SimpleNamespace(
            get_entity=AsyncMock(return_value=user()),
            download_profile_photo=AsyncMock(side_effect=self.download),
        )
        self.db = SimpleNamespace(can_view_user=AsyncMock(return_value=True))
        self.handler = AvatarHandler(
            self.telegram,
            self.db,
            str(self.cache),
            str(self.default),
            str(self.ghost),
        )
        timeout = patch.object(AvatarHandler, "FETCH_TIMEOUT", 0.1)
        timeout.start()
        self.addCleanup(timeout.stop)
        load_timeout = patch.object(AvatarHandler, "LOAD_TIMEOUT", 0.4)
        load_timeout.start()
        self.addCleanup(load_timeout.stop)
        with patch("luoxu.web.AvatarHandler", return_value=self.handler):
            app = setup_app(
                self.db,
                self.telegram,
                str(self.cache),
                str(self.default),
                str(self.ghost),
                prefix=PREFIX,
            )
        self.client = TestClient(TestServer(app, shutdown_timeout=0.1))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def download(self, entity, *, file):
        Path(file).write_bytes(PHOTO)
        return file

    async def fetch(self, name="42"):
        async with asyncio.timeout(0.75):
            response = await self.client.get(f"{PREFIX}/avatar/{name}.jpg")
            return response, await response.read()

    async def test_cold_avatar_does_not_deadlock(self):
        response, data = await self.fetch()
        self.assertEqual(response.status, 200)
        self.assertEqual(data, PHOTO)
        self.assertEqual((self.cache / "420.jpg").read_bytes(), PHOTO)
        self.telegram.download_profile_photo.assert_awaited_once()
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")

    async def test_http_deadline_does_not_cancel_a_slow_valid_avatar(self):
        release = asyncio.Event()
        finished = asyncio.Event()
        cancelled = asyncio.Event()

        async def delayed(entity, *, file):
            try:
                await release.wait()
                result = await self.download(entity, file=file)
                finished.set()
                return result
            except asyncio.CancelledError:
                cancelled.set()
                raise

        self.telegram.download_profile_photo.side_effect = delayed
        try:
            response, data = await self.fetch()
            self.assertEqual((response.status, data), (200, DEFAULT))
            self.assertFalse(
                cancelled.is_set(), "HTTP wait must not abort useful avatar loading"
            )
            self.assertEqual(response.headers["X-Luoxu-Avatar-Status"], "pending")
        finally:
            release.set()
        await asyncio.wait_for(finished.wait(), 0.5)
        response, data = await self.fetch()
        self.assertEqual((response.status, data), (200, PHOTO))
        self.telegram.get_entity.assert_awaited_once()
        self.telegram.download_profile_photo.assert_awaited_once()

    async def test_warm_user_avatar_does_not_require_telegram_to_be_online(self):
        self.assertEqual((await self.fetch())[1], PHOTO)

        async def offline(_uid):
            await asyncio.Event().wait()

        self.telegram.get_entity.side_effect = offline
        response, data = await self.fetch()
        self.assertEqual((response.status, data), (200, PHOTO))
        self.telegram.get_entity.assert_awaited_once()

    async def test_slow_entity_lookup_can_finish_after_http_wait(self):
        release = asyncio.Event()

        async def delayed(uid):
            await release.wait()
            return user(uid)

        self.telegram.get_entity.side_effect = delayed
        response, data = await self.fetch()
        self.assertEqual(data, DEFAULT)
        self.assertEqual(response.headers["X-Luoxu-Avatar-Status"], "pending")
        task = self.handler._pending[42]
        self.assertFalse(task.done())
        release.set()
        await task
        self.assertEqual((await self.fetch())[1], PHOTO)
        self.telegram.get_entity.assert_awaited_once()

    async def test_cancelled_waiter_does_not_cancel_shared_load(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(entity, *, file):
            started.set()
            await release.wait()
            return await self.download(entity, file=file)

        self.telegram.download_profile_photo.side_effect = delayed
        waiter = asyncio.create_task(self.handler._avatar_file(42))
        await asyncio.wait_for(started.wait(), 0.5)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        task = self.handler._pending[42]
        self.assertFalse(task.done())
        release.set()
        await task
        self.assertEqual((await self.fetch())[1], PHOTO)

    async def test_permission_revocation_blocks_pending_and_warm_cache(self):
        release = asyncio.Event()

        async def delayed(entity, *, file):
            await release.wait()
            return await self.download(entity, file=file)

        self.telegram.download_profile_photo.side_effect = delayed
        self.assertEqual((await self.fetch())[1], DEFAULT)
        task = self.handler._pending[42]
        self.db.can_view_user.return_value = False
        self.assertEqual((await self.fetch())[0].status, 404)
        release.set()
        await task
        self.assertIn(42, self.handler._known)
        self.assertEqual((await self.fetch())[0].status, 404)
        self.telegram.get_entity.assert_awaited_once()

    async def test_expired_cache_serves_stale_while_photo_refreshes(self):
        self.assertEqual((await self.fetch())[1], PHOTO)
        self.handler._known[42] = (str(self.cache / "420.jpg"), 0)
        release = asyncio.Event()
        updated = PHOTO + b"updated"
        self.telegram.get_entity.return_value = user(photo_id=421)

        async def delayed(_entity, *, file):
            await release.wait()
            Path(file).write_bytes(updated)
            return file

        self.telegram.download_profile_photo.side_effect = delayed
        self.assertEqual((await self.fetch())[1], PHOTO)
        task = self.handler._pending[42]
        release.set()
        await task
        self.assertEqual((await self.fetch())[1], updated)
        self.assertEqual(self.telegram.get_entity.await_count, 2)

    async def test_background_queue_and_entity_concurrency_are_bounded(self):
        release = asyncio.Event()

        async def delayed(uid):
            await release.wait()
            return user(uid, uid * 10)

        self.telegram.get_entity.side_effect = delayed
        self.handler._downloads = asyncio.Semaphore(1)
        with patch.object(self.handler, "MAX_PENDING", 3):
            responses = await asyncio.gather(
                *(self.fetch(str(uid)) for uid in range(40, 44))
            )
            self.assertTrue(all(data == DEFAULT for _, data in responses))
            self.assertEqual(len(self.handler._pending), 3)
            self.telegram.get_entity.assert_awaited_once()
            statuses = [r.headers["X-Luoxu-Avatar-Status"] for r, _ in responses]
            self.assertEqual(statuses.count("pending"), 3)
            self.assertEqual(statuses.count("unavailable"), 1)
            tasks = tuple(self.handler._pending.values())
            release.set()
            await asyncio.gather(*tasks)
        self.assertFalse(self.handler._pending)

    async def test_success_cache_is_bounded_and_recovers_missing_files(self):
        self.telegram.get_entity.side_effect = lambda uid: user(uid, uid * 10)
        with patch.object(self.handler, "MAX_CACHED_USERS", 2):
            for uid in (40, 41, 42):
                self.assertEqual((await self.fetch(str(uid)))[1], PHOTO)
            self.assertEqual(list(self.handler._known), [41, 42])
        (self.cache / "420.jpg").unlink()
        self.assertEqual((await self.fetch())[1], PHOTO)
        self.assertEqual(self.telegram.download_profile_photo.await_count, 4)

    async def test_app_cleanup_cancels_workers_and_removes_partial_files(self):
        async def stalled(_entity, *, file):
            Path(file).write_bytes(b"partial")
            await asyncio.Event().wait()

        self.telegram.download_profile_photo.side_effect = stalled
        self.assertEqual((await self.fetch())[1], DEFAULT)
        tasks = tuple(self.handler._pending.values())
        self.assertTrue(tasks)
        await self.client.close()
        self.assertTrue(all(task.cancelled() for task in tasks))
        self.assertFalse(self.handler._pending)
        self.assertEqual(list(self.cache.iterdir()), [])
        self.assertEqual(await self.handler._avatar_file(43), str(self.default))
        self.telegram.get_entity.assert_awaited_once()

    async def test_real_telethon_profile_download_uses_existing_temp_path(self):
        # Exercise Telethon's real filename/result handling, mocking only transport.
        async def write_file(_location, file, **_kwargs):
            Path(file).write_bytes(PHOTO)
            return file

        transport = SimpleNamespace(
            _get_proper_filename=DownloadMethods._get_proper_filename,
            download_file=AsyncMock(side_effect=write_file),
        )

        async def profile_photo(entity, *, file):
            return await DownloadMethods.download_profile_photo(
                transport, entity, file=file
            )

        self.telegram.download_profile_photo.side_effect = profile_photo
        self.assertEqual((await self.fetch())[1], PHOTO)
        self.assertEqual([p.name for p in self.cache.iterdir()], ["420.jpg"])
        transport.download_file.assert_awaited_once()

    async def test_cached_avatar_does_not_download_again(self):
        (self.cache / "420.jpg").write_bytes(PHOTO)
        response, data = await self.fetch()
        self.assertEqual(response.status, 200)
        self.assertEqual(data, PHOTO)
        self.telegram.download_profile_photo.assert_not_awaited()

    async def test_same_photo_is_downloaded_once_for_concurrent_requests(self):
        async def download(entity, *, file):
            await asyncio.sleep(0.01)
            return await self.download(entity, file=file)

        self.telegram.download_profile_photo.side_effect = download
        results = await asyncio.gather(*(self.fetch() for _ in range(5)))
        self.assertTrue(
            all(response.status == 200 and data == PHOTO for response, data in results)
        )
        self.telegram.download_profile_photo.assert_awaited_once()
        self.assertEqual([p.name for p in self.cache.iterdir()], ["420.jpg"])

    async def test_slow_photo_does_not_block_another_photo(self):
        started = asyncio.Event()
        self.telegram.get_entity.side_effect = lambda uid: user(uid, uid * 10)

        async def download(entity, *, file):
            if entity.id == 42:
                started.set()
                await asyncio.Event().wait()
            return await self.download(entity, file=file)

        self.telegram.download_profile_photo.side_effect = download
        async with asyncio.timeout(0.75), asyncio.TaskGroup() as tasks:
            slow = tasks.create_task(self.fetch("42"))
            await started.wait()
            response, data = await self.fetch("43")
            self.assertEqual(data, PHOTO)
            self.assertEqual(response.status, 200)
            self.assertFalse(
                slow.done(), "unrelated avatar must not wait for the slow download"
            )
        self.assertEqual(slow.result()[1], DEFAULT)

    async def test_entity_timeout_returns_default_and_backs_off(self):
        async def stalled(_uid):
            await asyncio.Event().wait()

        self.telegram.get_entity.side_effect = stalled
        response, data = await self.fetch()
        self.assertEqual((response.status, data), (200, DEFAULT))
        self.assertEqual(response.history, ())
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.assertEqual(response.headers["X-Luoxu-Avatar-Status"], "pending")
        self.assertNotIn(42, self.handler._retry_after)
        await asyncio.gather(*tuple(self.handler._pending.values()))
        retry, data = await self.fetch()
        self.assertEqual(data, DEFAULT)
        self.assertEqual(retry.headers["X-Luoxu-Avatar-Status"], "unavailable")
        self.telegram.get_entity.assert_awaited_once()
        self.telegram.download_profile_photo.assert_not_awaited()
        # A retry-backoff entry never bypasses a newly revoked permission.
        self.db.can_view_user.return_value = False
        self.assertEqual((await self.fetch())[0].status, 404)

    async def test_expired_backoff_retries_and_is_memory_bounded(self):
        self.telegram.get_entity.side_effect = ConnectionError("offline")
        with patch.object(AvatarHandler, "MAX_FAILURES", 2):
            for uid in (40, 41, 42):
                self.assertEqual((await self.fetch(str(uid)))[1], DEFAULT)
            self.assertEqual(list(self.handler._retry_after), [41, 42])
        self.handler._retry_after[42] = 0  # expired, without a 30-second test sleep
        self.telegram.get_entity.side_effect = None
        response, data = await self.fetch("42")
        self.assertEqual((response.status, data), (200, PHOTO))
        self.assertNotIn(42, self.handler._retry_after)

    async def test_same_photo_lock_waiters_have_bounded_waits(self):
        async def stalled(_entity, *, file):
            Path(file).write_bytes(b"partial")
            await asyncio.Event().wait()

        self.telegram.download_profile_photo.side_effect = stalled
        responses = await asyncio.gather(
            *(self.fetch(str(uid)) for uid in range(40, 45))
        )
        self.assertTrue(
            all(r.status == 200 and data == DEFAULT for r, data in responses)
        )
        await asyncio.gather(*tuple(self.handler._pending.values()))
        self.assertEqual(list(self.cache.iterdir()), [])

    async def test_download_timeout_removes_partial_file(self):
        async def stalled(_entity, *, file):
            Path(file).write_bytes(b"partial")
            await asyncio.Event().wait()

        self.telegram.download_profile_photo.side_effect = stalled
        response, data = await self.fetch()
        self.assertEqual((response.status, data), (200, DEFAULT))
        await asyncio.gather(*tuple(self.handler._pending.values()))
        self.assertEqual(list(self.cache.iterdir()), [])

    async def test_unavailable_telegram_avatar_returns_default(self):
        for error in (
            ConnectionError("offline"),
            ValueError("unknown user"),
            ChannelPrivateError(request=None),
        ):
            with self.subTest(error=type(error).__name__):
                # Different IDs prevent a prior failure's retry backoff masking this case.
                self.telegram.get_entity.side_effect = error
                response, data = await self.fetch(str(100 + len(type(error).__name__)))
                self.assertEqual((response.status, data), (200, DEFAULT))
                self.assertEqual(response.history, ())

    async def test_missing_download_does_not_cache_empty_avatar(self):
        self.telegram.download_profile_photo.side_effect = None
        self.telegram.download_profile_photo.return_value = None
        response, data = await self.fetch()
        self.assertEqual((response.status, data), (200, DEFAULT))
        self.assertEqual(list(self.cache.iterdir()), [])

    async def test_deleted_and_photoless_users_return_defaults_directly(self):
        for entity, expected in (
            (User(id=42, deleted=True), GHOST),
            (User(id=43), DEFAULT),
        ):
            self.telegram.get_entity.return_value = entity
            response, data = await self.fetch(str(entity.id))
            self.assertEqual((response.status, data), (200, expected))
            self.assertEqual(response.history, ())
        self.telegram.download_profile_photo.assert_not_awaited()

    async def test_denied_cached_avatar_never_reaches_telegram(self):
        (self.cache / "420.jpg").write_bytes(PHOTO)
        self.db.can_view_user.return_value = False
        response, _ = await self.fetch()
        self.assertEqual(response.status, 404)
        self.telegram.get_entity.assert_not_awaited()

    async def test_cancellation_cleans_up_and_releases_download_lock(self):
        started = asyncio.Event()

        async def stalled(_entity, *, file):
            Path(file).write_bytes(b"partial")
            started.set()
            await asyncio.Event().wait()

        self.telegram.download_profile_photo.side_effect = stalled
        task = asyncio.create_task(self.handler._get_avatar(user()))
        try:
            await asyncio.wait_for(started.wait(), 0.5)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(list(self.cache.iterdir()), [])
        self.telegram.download_profile_photo.side_effect = self.download
        path = await asyncio.wait_for(self.handler._get_avatar(user()), 0.5)
        self.assertEqual(Path(path).read_bytes(), PHOTO)

    async def test_stalled_avatars_release_connections_for_context(self):
        entities_ready = asyncio.Event()
        entities = []

        def get_entity(uid):
            entities.append(uid)
            if len(entities) == 2:
                entities_ready.set()
            return user(uid, uid * 10)

        async def stalled(_entity, *, file):
            await asyncio.Event().wait()

        self.telegram.get_entity.side_effect = get_entity
        self.telegram.download_profile_photo.side_effect = stalled
        cid = uuid.uuid4()
        self.db.find_group_message_conversation = AsyncMock(return_value=cid)
        self.db.get_context = AsyncMock(
            return_value={
                "target": {
                    "conversation_id": cid,
                    "msgid": 402868,
                    "group_id": 1998301990,
                    "from_user": 42,
                    "from_user_name": "Sender",
                    "text": "context target",
                    "created_at": datetime.datetime(
                        2025, 1, 1, tzinfo=datetime.timezone.utc
                    ),
                },
                "before": [],
                "after": [],
                "replies": [],
            }
        )
        async with (
            ClientSession(
                connector=TCPConnector(limit=2, limit_per_host=2),
                timeout=ClientTimeout(total=0.75),
            ) as session,
            asyncio.TaskGroup() as tasks,
        ):

            async def fetch_avatar(uid):
                async with session.get(
                    self.client.make_url(f"{PREFIX}/avatar/{uid}.jpg")
                ) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(await response.read(), DEFAULT)

            tasks.create_task(fetch_avatar(42))
            tasks.create_task(fetch_avatar(43))
            await asyncio.wait_for(entities_ready.wait(), 0.5)
            async with session.get(
                self.client.make_url(f"{PREFIX}/context?g=1998301990&id=402868")
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["target"]["id"], 402868)
        self.db.get_context.assert_awaited_once()

    async def test_named_defaults_do_not_contact_telegram(self):
        for name, expected in (("nobody", DEFAULT), ("ghost", GHOST)):
            response, data = await self.fetch(name)
            self.assertEqual(response.status, 200)
            self.assertEqual(data, expected)
        self.telegram.get_entity.assert_not_awaited()
        self.db.can_view_user.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
