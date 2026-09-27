"""Тесты логики поиска, прогресс-бара и фильтра премиум-треков."""

import asyncio
import unittest

from core import app_logic
from core.app_logic import (
    BAR_EMPTY,
    BAR_FILLED,
    MusicClient,
    TelegramProgressReporter,
    _clean_track_id,
    format_progress_bar,
    is_premium_track,
    track_display_name,
)


TWO_QUESTIONS = "?" * 2


def _allow_state(**extra) -> dict:
    state = {
        "policy": "ALLOW",
        "streamable": True,
        "state": "finished",
        "monetization_model": "NOT_APPLICABLE",
        "duration": 180000,
        "full_duration": 180000,
        "media": {"transcodings": [
            {"format": {"protocol": "progressive"}, "url": "https://api/media/stream/1", "snipped": False},
        ]},
    }
    state.update(extra)
    return state


def _snip_state(**extra) -> dict:
    state = {
        "policy": "SNIP",
        "streamable": True,
        "state": "finished",
        "monetization_model": "NOT_APPLICABLE",
        "duration": 30000,
        "full_duration": 210000,
        "media": {"transcodings": [
            {"format": {"protocol": "hls"}, "url": "https://api/media/preview/0/30/1", "snipped": True},
        ]},
    }
    state.update(extra)
    return state


class _FakeYDL:
    """Заглушка yt-dlp: возвращает заранее заданные записи."""

    handler = staticmethod(lambda url: {"entries": []})
    calls: list = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def extract_info(self, url, download=False):
        _FakeYDL.calls.append(url)
        return _FakeYDL.handler(url)


class TestProgressBar(unittest.TestCase):
    def test_unknown_total_shows_megabytes(self):
        text = format_progress_bar(3 * 1_048_576, 0)
        self.assertIn("Загрузка", text)
        self.assertIn("3.0 MB", text)
        self.assertNotIn(TWO_QUESTIONS, text)

    def test_empty_bar(self):
        text = format_progress_bar(0, 1000)
        self.assertIn(f"[{BAR_EMPTY * 10}]", text)
        self.assertIn("0%", text)

    def test_half_bar(self):
        text = format_progress_bar(500, 1000)
        self.assertIn(f"[{BAR_FILLED * 5}{BAR_EMPTY * 5}]", text)
        self.assertIn("50%", text)

    def test_full_bar(self):
        text = format_progress_bar(1000, 1000)
        self.assertIn(f"[{BAR_FILLED * 10}]", text)
        self.assertIn("100%", text)

    def test_overflow_is_clamped(self):
        text = format_progress_bar(5000, 1000)
        self.assertIn("100%", text)
        self.assertIn(f"[{BAR_FILLED * 10}]", text)
        self.assertNotIn(BAR_EMPTY, text)

    def test_custom_width(self):
        text = format_progress_bar(500, 1000, width=4)
        self.assertIn(f"[{BAR_FILLED * 2}{BAR_EMPTY * 2}]", text)

    def test_negative_and_none_values(self):
        self.assertIn("0%", format_progress_bar(-10, 1000))
        self.assertIn("Загрузка", format_progress_bar(None, None))

    def test_no_question_marks_in_output(self):
        for downloaded, total in ((0, 0), (0, 10), (10, 10), (3, 0)):
            self.assertNotIn("?", format_progress_bar(downloaded, total))


class TestTrackDisplayName(unittest.TestCase):
    def test_artist_and_title(self):
        self.assertEqual(track_display_name("Linkin Park", "Numb"), "Linkin Park — Numb")

    def test_only_title(self):
        self.assertEqual(track_display_name("", "Numb"), "Numb")

    def test_only_artist(self):
        self.assertEqual(track_display_name("Linkin Park", None), "Linkin Park")

    def test_duplicates_are_collapsed(self):
        self.assertEqual(track_display_name("Numb", "Numb"), "Numb")

    def test_extra_dashes_are_stripped(self):
        self.assertEqual(track_display_name(" - Linkin Park - ", " - Numb "), "Linkin Park — Numb")

    def test_empty(self):
        self.assertEqual(track_display_name(None, None), "")
        self.assertEqual(track_display_name("   ", "  "), "")

    def test_no_question_marks(self):
        self.assertNotIn("?", track_display_name("Artist", "Title"))


class TestReporterRender(unittest.TestCase):
    def test_render_without_title(self):
        reporter = TelegramProgressReporter(lambda text: None)
        text = reporter.render(0, 100)
        self.assertIn("Загружаю трек", text)
        self.assertIn("0%", text)
        self.assertNotIn("<i>", text)

    def test_render_with_title(self):
        reporter = TelegramProgressReporter(lambda text: None, title="Artist — Title")
        text = reporter.render(50, 100)
        self.assertIn("<i>Artist — Title</i>", text)
        self.assertIn("50%", text)

    def test_title_is_html_escaped(self):
        reporter = TelegramProgressReporter(lambda text: None, title="<b>hack</b> & co")
        rendered = reporter.render(0, 0)
        self.assertIn("&lt;b&gt;hack&lt;/b&gt; &amp; co", rendered)
        self.assertNotIn("<b>hack", rendered)

    def test_no_question_marks(self):
        reporter = TelegramProgressReporter(lambda text: None, title="A — B")
        self.assertNotIn("?", reporter.render(10, 100))


class TestCleanTrackId(unittest.TestCase):
    def test_strips_soundcloud_prefix(self):
        self.assertEqual(_clean_track_id("soundcloud:tracks:123"), "123")

    def test_keeps_plain_id(self):
        self.assertEqual(_clean_track_id("123"), "123")
        self.assertEqual(_clean_track_id("yt_abc"), "yt_abc")

    def test_strips_spaces(self):
        self.assertEqual(_clean_track_id("  456  "), "456")


class TestIsGarbage(unittest.TestCase):
    def test_short_track_is_garbage(self):
        self.assertTrue(MusicClient._is_garbage({"title": "Long enough", "duration": 10}))

    def test_thirty_second_snippet_is_garbage(self):
        self.assertTrue(MusicClient._is_garbage({"title": "Snippet", "duration": 30.0}))

    def test_preview_title_is_garbage(self):
        self.assertTrue(MusicClient._is_garbage({"title": "Track (preview)", "duration": 200}))

    def test_deleted_title_is_garbage(self):
        self.assertTrue(MusicClient._is_garbage({"title": "[deleted]", "duration": 200}))
        self.assertTrue(MusicClient._is_garbage({"title": "", "duration": 200}))

    def test_normal_track_is_kept(self):
        self.assertFalse(MusicClient._is_garbage({"title": "Numb", "duration": 185}))

    def test_duration_unknown_is_kept(self):
        self.assertFalse(MusicClient._is_garbage({"title": "Numb", "duration": 0}))


class TestIsPremiumTrack(unittest.TestCase):
    def test_no_state_is_not_premium(self):
        self.assertFalse(is_premium_track(None))
        self.assertFalse(is_premium_track({}))

    def test_allow_policy_is_playable(self):
        self.assertFalse(is_premium_track(_allow_state()))

    def test_snip_policy_is_premium(self):
        self.assertTrue(is_premium_track(_snip_state()))

    def test_block_policy_is_premium(self):
        self.assertTrue(is_premium_track(_allow_state(policy="BLOCK")))

    def test_lowercase_policy_is_supported(self):
        self.assertTrue(is_premium_track(_allow_state(policy="snip")))

    def test_not_streamable_is_premium(self):
        self.assertTrue(is_premium_track(_allow_state(streamable=False)))

    def test_unfinished_state_is_premium(self):
        self.assertTrue(is_premium_track(_allow_state(state="processing")))

    def test_sub_high_tier_is_premium(self):
        self.assertTrue(is_premium_track(_allow_state(monetization_model="SUB_HIGH_TIER")))

    def test_drm_only_transcodings_are_premium(self):
        state = _allow_state(media={"transcodings": [
            {"format": {"protocol": "cbc-encrypted-hls"}, "url": "https://api/media/stream/1", "snipped": False},
            {"format": {"protocol": "ctr-encrypted-hls"}, "url": "https://api/media/stream/2", "snipped": False},
        ]})
        self.assertTrue(is_premium_track(state))

    def test_encrypted_url_without_protocol_is_premium(self):
        state = _allow_state(media={"transcodings": [
            {"format": {"protocol": "hls"}, "url": "https://api/media/encrypted-hls/1", "snipped": False},
        ]})
        self.assertTrue(is_premium_track(state))

    def test_drm_plus_playable_is_not_premium(self):
        state = _allow_state(media={"transcodings": [
            {"format": {"protocol": "cbc-encrypted-hls"}, "url": "https://api/media/stream/1", "snipped": False},
            {"format": {"protocol": "ctr-encrypted-hls"}, "url": "https://api/media/stream/2", "snipped": False},
            {"format": {"protocol": "hls"}, "url": "https://api/media/stream/3", "snipped": False},
        ]})
        self.assertFalse(is_premium_track(state))

    def test_snipped_transcoding_only_is_premium(self):
        state = _allow_state(media={"transcodings": [
            {"format": {"protocol": "hls"}, "url": "https://api/media/preview/0/30/1", "snipped": True},
        ]})
        self.assertTrue(is_premium_track(state))

    def test_no_media_info_is_not_premium(self):
        self.assertFalse(is_premium_track(_allow_state(media=None)))


class TestDropPremium(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = MusicClient()
        self._orig_get_states = app_logic.get_track_states

    def tearDown(self):
        app_logic.get_track_states = self._orig_get_states

    async def test_drops_premium_and_keeps_others(self):
        entries = [
            {"id": "1", "title": "Free track", "duration": 200},
            {"id": "2", "title": "Go+ track", "duration": 30.0},
            {"id": "3", "title": "Another free", "duration": 180},
        ]

        async def fake_states(ids, use_cache=True):
            return {"1": _allow_state(), "2": _snip_state(), "3": _allow_state(full_duration=200000)}

        app_logic.get_track_states = fake_states
        kept = await self.client._drop_premium(entries)

        self.assertEqual([e["id"] for e in kept], ["1", "3"])
        self.assertEqual(kept[1]["duration"], 200.0)

    async def test_keeps_everything_when_api_is_unavailable(self):
        entries = [{"id": "1", "title": "Free track", "duration": 200}]

        async def no_states(ids, use_cache=True):
            return {}

        app_logic.get_track_states = no_states
        kept = await self.client._drop_premium(entries)
        self.assertEqual(len(kept), 1)

    async def test_empty_input(self):
        self.assertEqual(await self.client._drop_premium([]), [])


class TestSearchTrack(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = MusicClient()
        self._orig_ydl = app_logic.yt_dlp.YoutubeDL
        self._orig_get_states = app_logic.get_track_states
        app_logic._search_cache.clear()
        _FakeYDL.calls = []
        app_logic.yt_dlp.YoutubeDL = _FakeYDL

    def tearDown(self):
        app_logic.yt_dlp.YoutubeDL = self._orig_ydl
        app_logic.get_track_states = self._orig_get_states
        app_logic._search_cache.clear()

    async def test_premium_tracks_are_filtered_out(self):
        sc_entries = [
            {"id": "sc1", "title": "Free one", "uploader": "A", "duration": 200,
             "url": "https://soundcloud.com/a/1", "thumbnails": []},
            {"id": "sc2", "title": "Premium one", "uploader": "B", "duration": 30.0,
             "url": "https://soundcloud.com/b/2", "thumbnails": []},
            {"id": "sc3", "title": "Free two", "uploader": "C", "duration": 210,
             "url": "https://soundcloud.com/c/3", "thumbnails": []},
        ]
        _FakeYDL.handler = staticmethod(
            lambda url: {"entries": sc_entries} if url.startswith("scsearch") else {"entries": []})

        async def fake_states(ids, use_cache=True):
            return {"sc1": _allow_state(), "sc2": _snip_state(), "sc3": _allow_state()}

        app_logic.get_track_states = fake_states
        result = await self.client.search_track("query", page=1, limit=5)

        self.assertEqual([item["id"] for item in result["items"]], ["sc1", "sc3"])
        self.assertEqual(result["paginationInfo"]["lastPage"], 1)

    async def test_result_is_cached(self):
        _FakeYDL.handler = staticmethod(lambda url: {"entries": [
            {"id": "sc1", "title": "Free one", "uploader": "A", "duration": 200,
             "url": "https://soundcloud.com/a/1", "thumbnails": []},
        ]})

        async def fake_states(ids, use_cache=True):
            return {"sc1": _allow_state()}

        app_logic.get_track_states = fake_states
        first = await self.client.search_track("cached", page=1, limit=1)
        calls_after_first = len(_FakeYDL.calls)
        second = await self.client.search_track("cached", page=1, limit=1)

        self.assertIs(first, second)
        self.assertEqual(calls_after_first, len(_FakeYDL.calls))

    async def test_pagination_uses_cleaned_list(self):
        entries = [
            {"id": f"sc{i}", "title": f"Free {i}", "uploader": "A", "duration": 200,
             "url": f"https://soundcloud.com/a/{i}", "thumbnails": []}
            for i in range(1, 9)
        ]
        entries.append({"id": "sc9", "title": "Premium", "uploader": "B",
                        "duration": 30.0, "url": "https://soundcloud.com/b/9", "thumbnails": []})
        _FakeYDL.handler = staticmethod(lambda url: {"entries": entries})

        async def fake_states(ids, use_cache=True):
            return {tid: (_snip_state() if tid == "sc9" else _allow_state()) for tid in ids}

        app_logic.get_track_states = fake_states
        page2 = await self.client.search_track("page2", page=2, limit=3)

        self.assertEqual([item["id"] for item in page2["items"]], ["sc4", "sc5", "sc6"])


    async def test_youtube_fallback_fills_the_page(self):
        def handler(url):
            if url.startswith("scsearch"):
                return {"entries": [
                    {"id": "sc1", "title": "Free one", "uploader": "A", "duration": 200,
                     "url": "https://soundcloud.com/a/1", "thumbnails": []},
                ]}
            return {"entries": [
                {"id": "abc", "title": "YT track", "uploader": "Chan", "duration": 150,
                 "thumbnails": []},
            ]}

        _FakeYDL.handler = staticmethod(handler)

        async def fake_states(ids, use_cache=True):
            return {tid: _allow_state() for tid in ids}

        app_logic.get_track_states = fake_states
        result = await self.client.search_track("fallback", page=1, limit=2)

        self.assertEqual([item["id"] for item in result["items"]], ["sc1", "yt_abc"])

    async def test_search_request_has_no_question_marks(self):
        _FakeYDL.handler = staticmethod(lambda url: {"entries": []})

        async def fake_states(ids, use_cache=True):
            return {}

        app_logic.get_track_states = fake_states
        await self.client.search_track("nothing", page=1, limit=3)
        for call in _FakeYDL.calls:
            self.assertNotIn(TWO_QUESTIONS, call)


class TestReporterUpdate(unittest.IsolatedAsyncioTestCase):
    async def test_update_calls_edit(self):
        seen = []

        async def edit(text):
            seen.append(text)

        reporter = TelegramProgressReporter(edit, title="A — B")
        await reporter.update(50, 100, force=True)
        self.assertEqual(len(seen), 1)
        self.assertIn("50%", seen[0])

    async def test_update_throttles_by_interval(self):
        seen = []

        async def edit(text):
            seen.append(text)

        reporter = TelegramProgressReporter(edit)
        await reporter.update(10, 100)
        await reporter.update(20, 100)
        self.assertEqual(len(seen), 1)

    async def test_update_swallows_edit_errors(self):
        async def broken_edit(text):
            raise RuntimeError("telegram is down")

        reporter = TelegramProgressReporter(broken_edit)
        await reporter.update(10, 100, force=True)  # не должно упасть


class TestModuleSurface(unittest.TestCase):
    def test_downloader_is_singleton(self):
        self.assertIs(app_logic.downloader, app_logic.downloader)
        self.assertIs(app_logic.SoundCloudClient, MusicClient)

    def test_async_api(self):
        self.assertTrue(asyncio.iscoroutinefunction(app_logic.downloader.search_track))
        self.assertTrue(asyncio.iscoroutinefunction(app_logic.fetch_track_states))
        self.assertTrue(asyncio.iscoroutinefunction(app_logic.get_track_states))


if __name__ == "__main__":
    unittest.main()
