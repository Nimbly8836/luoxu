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
        app = setup_app(
            self.db,
            None,
            str(self.cache),
            str(self.default),
            str(self.ghost),
            prefix=PREFIX,
        )
        app.router.add_get(PREFIX + r"/avatar/{uid:\d+}.jpg", self.handler.get)
        app.router.add_get(PREFIX + r"/avatar/{name:\w+}.jpg", self.handler.get)
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
        self.assertEqual((await self.fetch())[1], DEFAULT)
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
        responses = await asyncio.gather(*(self.fetch() for _ in range(5)))
        self.assertTrue(
            all(r.status == 200 and data == DEFAULT for r, data in responses)
        )
        self.assertEqual(list(self.cache.iterdir()), [])

    async def test_download_timeout_removes_partial_file(self):
        async def stalled(_entity, *, file):
            Path(file).write_bytes(b"partial")
            await asyncio.Event().wait()

        self.telegram.download_profile_photo.side_effect = stalled
        response, data = await self.fetch()
        self.assertEqual((response.status, data), (200, DEFAULT))
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
            (User(id=42), DEFAULT),
        ):
            self.telegram.get_entity.return_value = entity
            response, data = await self.fetch()
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
