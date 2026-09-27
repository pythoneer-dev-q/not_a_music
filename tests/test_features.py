"""Тесты новых разделов: помощь, история, язык, статистика, шаринг, быстрые запросы."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from database import actions_db, nav_db
from database.nav_db import RECENT_QUERIES_MAX, clean_query
from handlers import handlers as h_handlers
from handlers import inline_handlers, service_handlers
from utils import kbs, messages, routes, utils
from utils.i18n import t


async def _fake_pack_many(payloads):
    """Замена батч-упаковки в БД: id вида 'n' + 10 символов (как в nav_db)."""
    return [f"n{i:010d}" for i in range(len(payloads))]


def _rows(markup) -> list:
    return markup.inline_keyboard


class TestModuleWiring(unittest.TestCase):
    """Импорт всех роутеров и наличие обработчиков новых callback-ов."""

    def test_all_routers_import(self):
        from handlers import admin
        self.assertTrue(service_handlers.zrouter)
        self.assertTrue(h_handlers.arouter)
        self.assertTrue(inline_handlers.xrouter)
        self.assertTrue(admin.crouter)

    def test_history_handlers_exist(self):
        for name in ('history_page', 'history_remove', 'history_clear',
                     'save_track_from_list', 'choose_playlist_from_list',
                     'top_playlists_page'):
            self.assertTrue(callable(getattr(service_handlers, name)), name)

    def test_routes_are_registered(self):
        registered = {prefix for prefix, _ in routes.ROUTES}
        for prefix in ('hist:', 'hist_rm|', 'fsvq|', 'plq|', 'tpls:', 'pl_up|'):
            self.assertIn(prefix, registered, prefix)

    def test_inline_track_handler_exists(self):
        self.assertTrue(callable(inline_handlers.inline_track_handler))

    def test_new_keyboards_exist(self):
        for name in ('hist_kb', 'help_kb', 'text_page_kb', 'search_start_kb',
                     'top_pls_kb'):
            self.assertTrue(asyncio.iscoroutinefunction(getattr(kbs, name)), name)


class TestSpawn(unittest.IsolatedAsyncioTestCase):
    async def test_spawn_keeps_reference_until_done(self):
        async def _noop():
            return 1
        task = utils.spawn(_noop())
        self.assertIn(task, utils._background)
        await task
        self.assertNotIn(task, utils._background)

    async def test_spawn_swallows_exceptions_without_warning(self):
        async def _boom():
            raise ValueError("expected")
        task = utils.spawn(_boom())
        with self.assertRaises(ValueError):
            await task
        self.assertNotIn(task, utils._background)


class TestCleanQuery(unittest.TestCase):
    def test_strips_payload_separators(self):
        self.assertEqual(clean_query("a|b:c"), "a b c")

    def test_truncates_to_50(self):
        self.assertEqual(len(clean_query("x" * 200)), 50)

    def test_empty(self):
        self.assertEqual(clean_query(None), "")
        self.assertEqual(clean_query("   "), "")


class TestOriginBackCb(unittest.TestCase):
    def test_search_origin(self):
        self.assertEqual(kbs.origin_back_cb('sq:numb:3'), 'srp|numb|3')

    def test_top_origin(self):
        self.assertEqual(kbs.origin_back_cb('top'), 'user_topTracks')

    def test_history_origin(self):
        self.assertEqual(kbs.origin_back_cb('hist:2'), 'hist:2')

    def test_favs_origin(self):
        self.assertEqual(kbs.origin_back_cb('fav:4'), 'favs:4')

    def test_unknown_origin(self):
        self.assertEqual(kbs.origin_back_cb('something'), 'user_menu')


def _callbacks(markup) -> list:
    return [b.callback_data for row in _rows(markup) for b in row if b.callback_data]


def _texts(markup) -> list:
    return [b.text for row in _rows(markup) for b in row]


def _switch_inline(markup) -> list:
    return [b.switch_inline_query for row in _rows(markup)
            for b in row if b.switch_inline_query]


class TestMainMenu(unittest.IsolatedAsyncioTestCase):
    async def test_menu_has_all_new_sections(self):
        with mock.patch.object(kbs.music_db, 'pl_count_user',
                               new=mock.AsyncMock(return_value=0)):
            markup = await kbs.main_menu(1)
        callbacks = _callbacks(markup)
        for expected in ('hist:1', 'user_random', 'user_toppls', 'user_help',
                         'prof', 'favs:1', 'user_search', 'user_pllists'):
            self.assertIn(expected, callbacks, expected)


class TestSearchStartKb(unittest.IsolatedAsyncioTestCase):
    async def test_recent_queries_become_buttons(self):
        queries = ['numb', 'linkin park', 'запрос']
        with mock.patch.object(kbs, '_pack_many', new=_fake_pack_many), \
             mock.patch.object(kbs.nav_db, 'get_queries',
                               new=mock.AsyncMock(return_value=queries)):
            markup = await kbs.search_start_kb(1)

        rows = _rows(markup)
        self.assertEqual(len(rows), len(queries) + 1)   # запросы + «В меню»
        self.assertIn('user_menu', _callbacks(markup))

    async def test_no_queries_still_has_menu_button(self):
        with mock.patch.object(kbs, '_pack_many', new=_fake_pack_many), \
             mock.patch.object(kbs.nav_db, 'get_queries',
                               new=mock.AsyncMock(return_value=[])):
            markup = await kbs.search_start_kb(1)
        self.assertEqual(_callbacks(markup), ['user_menu'])



class TestSearchKb(unittest.IsolatedAsyncioTestCase):
    async def test_every_result_has_play_fav_and_playlist_buttons(self):
        items = [
            {'id': 'a', 'fileId': 'a', 'artist': 'Artist', 'title': 'Title',
             'duration': 100, 'is_downloaded': False},
            {'id': 'b', 'fileId': 'b', 'artist': 'Other', 'title': 'Song',
             'duration': 200, 'is_downloaded': True},
        ]
        with mock.patch.object(kbs, '_pack_many', new=_fake_pack_many):
            markup = await kbs.get_search_kb(
                items, 'query', page=1, pages_all=1, origin='sq:query:1')

        rows = _rows(markup)
        self.assertEqual(len(rows), 2 + 1 + 1)   # треки + пагинация + «В меню»
        self.assertEqual(len(rows[0]), 3)         # ▶️ + ⭐️ + ➕
        texts = _texts(markup)
        self.assertIn(t('btn_add_fav_short'), texts)
        self.assertIn(t('btn_add_pl_short'), texts)

    async def test_top_origin_is_kept_in_payloads(self):
        items = [{'id': 'a', 'fileId': 'a', 'artist': 'A', 'title': 'T',
                  'duration': 100}]
        captured = []

        async def _spy(payloads):
            captured.extend(payloads)
            return await _fake_pack_many(payloads)

        with mock.patch.object(kbs, '_pack_many', new=_spy):
            await kbs.get_search_kb(items, 'query', page=1, pages_all=1, origin='top')

        self.assertIn('dl|top|a', captured)
        self.assertIn('fsvq|a|top', captured)
        self.assertIn('plq|a|top', captured)


class TestHistKb(unittest.IsolatedAsyncioTestCase):
    async def test_history_rows_use_labels(self):
        docs = [{'track_id': '1', 'user_id': 7}, {'track_id': '2', 'user_id': 7}]
        labels = {'1': 'Artist — One', '2': 'Artist — Two'}
        with mock.patch.object(kbs, '_pack_many', new=_fake_pack_many):
            markup = await kbs.hist_kb(page=1, pages_all=3, docs=docs, labels=labels)

        texts = _texts(markup)
        self.assertTrue(any('Artist — One' in x for x in texts))
        self.assertTrue(any('Artist — Two' in x for x in texts))
        callbacks = _callbacks(markup)
        # страница 1 из 3: кнопки «назад» нет, есть «вперёд» + номер страницы
        self.assertNotIn('hist:0', callbacks)
        self.assertIn('hist:2', callbacks)
        self.assertIn('nt', callbacks)
        self.assertIn('hist_clr', callbacks)   # очистить историю
        self.assertIn('user_menu', callbacks)

    async def test_unknown_label_falls_back_to_id(self):
        with mock.patch.object(kbs, '_pack_many', new=_fake_pack_many):
            markup = await kbs.hist_kb(page=1, pages_all=1,
                                       docs=[{'track_id': 'zzz'}], labels={})
        self.assertTrue(any('zzz' in x for x in _texts(markup)))


class TestProfileKb(unittest.IsolatedAsyncioTestCase):
    async def test_language_button_is_present(self):
        markup = await kbs.profile_kb({'lang': 'ru', 'is_hidden': False})
        self.assertIn('langsw', _callbacks(markup))
        self.assertIn(t('btn_lang', lang='RU'), _texts(markup))


class TestTrackKb(unittest.IsolatedAsyncioTestCase):
    async def test_share_button_uses_inline_query(self):
        noop = mock.AsyncMock(return_value=None)
        with mock.patch.object(kbs, '_pack_many', new=_fake_pack_many), \
             mock.patch.object(kbs.actions_db, 'count_likes',
                               new=mock.AsyncMock(return_value=3)), \
             mock.patch.object(kbs.actions_db, 'count_dislikes',
                               new=mock.AsyncMock(return_value=1)), \
             mock.patch.object(kbs.actions_db, 'count_views',
                               new=mock.AsyncMock(return_value=10)), \
             mock.patch.object(kbs.actions_db, 'get_like', new=noop), \
             mock.patch.object(kbs.actions_db, 'get_dislike', new=noop), \
             mock.patch.object(kbs.actions_db, 'get_saved_one', new=noop):
            markup = await kbs.track_kb('42', user_id=1, back_cb='user_menu')

        self.assertIn('track_42', _switch_inline(markup))
        self.assertIn('user_menu', _callbacks(markup))


class TestHelpAndTextPages(unittest.IsolatedAsyncioTestCase):
    async def test_help_links_to_rules_and_policy(self):
        callbacks = _callbacks(await kbs.help_kb())
        self.assertIn('user_rules', callbacks)
        self.assertIn('user_policy', callbacks)
        self.assertIn('user_menu', callbacks)

    async def test_text_page_back_button(self):
        self.assertEqual(_callbacks(await kbs.text_page_kb('reg_rg')), ['reg_rg'])

    async def test_greet_keyboards_have_no_placeholder_urls(self):
        for builder in (kbs.greet_kb, kbs.greet_kb_2):
            markup = await builder()
            for row in _rows(markup):
                for button in row:
                    self.assertIsNone(button.url, f'заглушка {button.url}')

    async def test_greet_kb_opens_rules_inline(self):
        callbacks = _callbacks(await kbs.greet_kb())
        self.assertIn('reg_rules', callbacks)


class TestTopPlsKb(unittest.IsolatedAsyncioTestCase):
    async def test_items_and_navigation(self):
        items = [
            {'_id': 'p1', 'pl_name': 'A', 'views': 10, 'likes': 2},
            {'_id': 'p2', 'pl_name': 'B', 'views': 5, 'likes': 1},
        ]
        markup = await kbs.top_pls_kb(page=2, pages_all=4, items=items)
        callbacks = _callbacks(markup)
        self.assertIn('plv:p1:1', callbacks)
        self.assertIn('plv:p2:1', callbacks)
        self.assertIn('tpls:1', callbacks)
        self.assertIn('tpls:3', callbacks)


class TestStatsBlock(unittest.IsolatedAsyncioTestCase):
    async def test_block_contains_all_counters(self):
        with mock.patch.object(messages.actions_db, 'count_user_likes',
                               new=mock.AsyncMock(return_value=4)), \
             mock.patch.object(messages.actions_db, 'count_user_dislikes',
                               new=mock.AsyncMock(return_value=1)), \
             mock.patch.object(messages.actions_db, 'saved_count',
                               new=mock.AsyncMock(return_value=7)), \
             mock.patch.object(messages.music_db, 'pl_count_user',
                               new=mock.AsyncMock(return_value=2)):
            block = await messages.stats_block(user_id=1, played=11)

        for key, value in (('st_played', 11), ('st_likes', 4),
                           ('st_dislikes', 1), ('st_favs', 7), ('st_pls', 2)):
            self.assertIn(t(key, n=value), block)
        self.assertIn(t('stats_title'), block)

    async def test_zero_played_is_rendered_as_zero(self):
        with mock.patch.object(messages.actions_db, 'count_user_likes',
                               new=mock.AsyncMock(return_value=0)), \
             mock.patch.object(messages.actions_db, 'count_user_dislikes',
                               new=mock.AsyncMock(return_value=0)), \
             mock.patch.object(messages.actions_db, 'saved_count',
                               new=mock.AsyncMock(return_value=0)), \
             mock.patch.object(messages.music_db, 'pl_count_user',
                               new=mock.AsyncMock(return_value=0)):
            block = await messages.stats_block(user_id=1, played=None)
        self.assertIn(t('st_played', n=0), block)


def _fake_call(user_id: int = 5):
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id), message=None)


class TestPlayedCounter(unittest.IsolatedAsyncioTestCase):
    async def test_counter_increments_even_when_ads_disabled(self):
        incr = mock.AsyncMock(return_value=1)
        with mock.patch.object(service_handlers.bot_db, 'incr_played', new=incr), \
             mock.patch.object(service_handlers.settings_db, 'get',
                               new=mock.AsyncMock(return_value=0)):
            shown = await service_handlers._maybe_show_ad(_fake_call())
        self.assertFalse(shown)
        incr.assert_awaited_once_with(5)

    async def test_ad_shown_on_every_nth_play(self):
        incr = mock.AsyncMock(return_value=10)
        ad_msg = SimpleNamespace(edit_text=mock.AsyncMock())
        with mock.patch.object(service_handlers.bot_db, 'incr_played', new=incr), \
             mock.patch.object(service_handlers.bot_db, 'get_ads_random',
                               new=mock.AsyncMock(return_value={'text': 'Ad'})), \
             mock.patch.object(service_handlers.settings_db, 'get',
                               new=mock.AsyncMock(return_value=5)), \
             mock.patch.object(service_handlers.nav_db, 'get_last_cb',
                               new=mock.AsyncMock(return_value='user_menu')), \
             mock.patch.object(service_handlers.kbs, 'ad_continue_kb',
                               new=mock.AsyncMock(return_value=None)), \
             mock.patch.object(service_handlers.utils, 'edit_or_resend',
                               new=mock.AsyncMock(return_value=ad_msg)), \
             mock.patch.object(service_handlers.asyncio, 'sleep',
                               new=mock.AsyncMock()):
            shown = await service_handlers._maybe_show_ad(_fake_call())
        self.assertTrue(shown)
        incr.assert_awaited_once()

    async def test_ad_hidden_between_multiples(self):
        incr = mock.AsyncMock(return_value=4)
        with mock.patch.object(service_handlers.bot_db, 'incr_played', new=incr), \
             mock.patch.object(service_handlers.bot_db, 'get_ads_random',
                               new=mock.AsyncMock(return_value={'text': 'Ad'})), \
             mock.patch.object(service_handlers.settings_db, 'get',
                               new=mock.AsyncMock(return_value=5)):
            shown = await service_handlers._maybe_show_ad(_fake_call())
        self.assertFalse(shown)
        incr.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
