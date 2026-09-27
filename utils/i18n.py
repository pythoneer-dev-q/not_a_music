from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware

from utils.app_config import Config
from database import bot_db, settings_db

SUPPORTED_LANGS = ('ru', 'en')

current_lang: ContextVar[str] = ContextVar('current_lang', default=Config.DEFAULT_LANG)

_lang_cache: Dict[int, str] = {}

TRANSLATIONS: Dict[str, Dict[str, str]] = {
    'ru': {
        'greet': "🎉 Привет, <b>{name}</b>!\n🔮 Давай наслаждаться бесплатной и безлимитной музыкой!\n\n🧩 <b>PS:</b> Ты пришел от {source}",
        'main_menu': "<b>💡 Главное меню:</b>\n\n<i>💬 Чтобы поискать трек — нажмите «Поиск»</i>",
        'welcome_menu': "✅ Добро пожаловать в меню!",
        'rules_read': "❓ Внимательно ознакомьтесь с правилами платформы",
        'policy_read': "❗️ Прочтите внимательно политику конфиденциальности",
        'sub_prompt': "⚡️ Подпишитесь на каналы",
        'sub_needed': "🧩 Подпишитесь на все каналы!",
        'banned': "Вы заблокированы. Обратитесь в поддержку.",
        'error_generic': "Возникла проблема при обработке вашего запроса. Попробуйте выполнить все условия, указанные в сообщении выше.",

        'search_prompt': "🔎 Введите запрос для поиска",
        'searching': "⚡️ Ищу: <code>{query}</code>...",
        'no_results': "❌ Ничего не нашли...\n\n<i>Попробуйте изменить запрос?</i>",
        'results_for': "🔎 Результаты по запросу: <b>{query}</b>",
        'too_long': "⚠️ Слишком длинный запрос (макс. 50 симв.)",
        'no_more': "Больше ничего не найдено",
        'page_of': "📑 {p} / {all}",

        'uploading': "⏳ Загрузка трека в облако...",
        'already_open': "ℹ️ Трек уже открыт",
        'liked': "👍 Лайк!",
        'unliked': "👍 Лайк убран",
        'disliked': "👎 Дизлайк",
        'undisliked': "👎 Дизлайк убран",

        'fav_title': "⭐️ <b>Избранное</b> ({n} / {max})\nСтраница {p} / {all}",
        'fav_empty': "🎧 В избранном пока пусто.\n\n<i>⭐️ Добавляйте треки кнопкой под треком.</i>",
        'fav_added': "✅ Трек добавлен в избранное",
        'fav_full': "⚠️ В избранном максимум {max} треков",
        'fav_removed': "🗑 Трек удален из избранного",
        'pl_not_found': "❌ Плейлист не найден",
        'pl_name_prompt': "<b>Введите название вашего плейлиста</b>\n\n<i>Оно будет видно другим пользователям</i>",
        'pl_name_long': "⚠️ Слишком длинное название! Лимит: 30 символов.",
        'pl_created': "✅ Плейлист создан!",
        'pl_limit': "⚠️ Достигнут лимит плейлистов (лимит: {max})",
        'pl_deleted': "🗑 Плейлист: <code>{id}</code> удален...",
        'pl_cleared': "🗑 Все треки плейлиста <code>{id}</code> <b>удалены</b>",
        'pl_track_added': "✅ Трек добавлен в плейлист",
        'pl_track_full': "⚠️ В плейлисте нет свободных слотов (лимит: {max})",
        'pl_track_removed': "🗑 Трек удален из плейлиста",
        'pl_choose': "<b>Выберите плейлист</b>:",
        'pl_list_empty': "🎧 У вас нет плейлистов :(\nПопробуйте создать новый!",
        'pl_list_ok': "🎵 Ваши плейлисты:",
        'pl_end': "ℹ️ Это последний трек плейлиста",
        'pl_share_text': "🎧 <b>Послушайте мой плейлист: {name}</b>\n\nВсего треков: {n}",
        'pl_tracks_count': "В плейлисте {n} треков",

        'ad_msg': "📢 <b>Реклама:</b> \n\n<i>{text}</i>\n<i>(показывается каждые {n} треков)</i>",

        # --- профиль ---
        'about_prompt': "✏️ <b>Отправьте текст «О себе»</b>\n\n<i>Максимум 300 символов. Он будет виден в вашем профиле.</i>",
        'about_saved': "✅ Информация «О себе» сохранена!",
        'profile_hidden_self': "🙈 Ваш профиль скрыт — другие пользователи не увидят информацию о вас.",
        'profile_hidden': "🙈 Пользователь скрыл свой профиль.",
        'visibility_off': "🙈 Профиль скрыт",
        'visibility_on': "👁 Профиль открыт",
        'lang_switched': "✅ Язык изменен!",

        # --- инлайн ---
        'inline_press_to_load': "🎵 <b>{query}</b>\n\n<i>Нажмите «Загрузить», чтобы получить трек</i>",
        'inline_listen_bot': "🔺 Перейдите в бот, чтобы послушать все треки! Нажмите кнопку: «Слушать в боте»",
        'btn_continue': "▶️ Продолжить",
        'cb_expired': "⌛️ Ссылка устарела — откройте раздел заново",
        'top_chart': "🏆 Топ-чарт: <b>TOP 1000</b>",
        'user_not_found': "❌ Пользователь не найден",

        # --- помощь / правила / статистика ---
        'btn_help': "ℹ️ Помощь",
        'help_title': "ℹ️ <b>Как пользоваться ботом</b>",
        'help_text': ("🔎 <b>Поиск</b> — просто напишите название трека или исполнителя.\n"
                      "⚡️ <b>ТОП</b> — самые прослушиваемые треки.\n"
                      "⭐️ <b>Избранное</b> — кнопкой ⭐️ под любым списком треков.\n"
                      "🎵 <b>Плейлисты</b> — собирайте свои подборки.\n"
                      "🕘 <b>История</b> — что вы слушали раньше.\n"
                      "🎲 <b>Случайный трек</b> — когда не знаете, что послушать.\n"
                      "✈️ <b>Поделиться</b> — отправляйте треки в другие чаты кнопкой под треком."),
        'rules_title': "🛡 <b>Правила использования</b>",
        'rules_text': ("1️⃣ Бот нужен для личного прослушивания музыки.\n"
                       "2️⃣ Запрещено использовать бот для рассылок и спама.\n"
                       "3️⃣ Запрещен оскорбительный контент в названиях плейлистов.\n"
                       "4️⃣ За нарушения аккаунт может быть заблокирован.\n"
                       "5️⃣ Данные хранятся только для работы бота."),
        'policy_title': "❗️ <b>Политика конфиденциальности</b>",
        'policy_text': ("Бот хранит только необходимое для работы:\n"
                        "• ваш Telegram ID, имя и источник перехода;\n"
                        "• язык и описание профиля (если заполнили);\n"
                        "• плейлисты, избранное и историю прослушиваний.\n\n"
                        "Данные не передаются третьим лицам. Удалить их можно по запросу администратору."),

        # --- статистика профиля ---
        'stats_title': "📊 <b>Моя статистика</b>",
        'st_played': "▶️ Прослушано треков: <b>{n}</b>",
        'st_likes': "👍 Лайков: <b>{n}</b>",
        'st_dislikes': "👎 Дизлайков: <b>{n}</b>",
        'st_favs': "⭐️ В избранном: <b>{n}</b>",
        'st_pls': "🎵 Плейлистов: <b>{n}</b>",

        # --- история ---
        'btn_hist': "🕘 История",
        'hist_title': "🕘 <b>История прослушиваний</b>\nСтраница {p} / {all} · всего {n}",
        'hist_empty': "🕘 История пуста.\n\n<i>Сыграйте трек — он появится здесь.</i>",
        'hist_cleared': "🧹 История очищена",
        'btn_clear_hist': "🧹 Очистить историю",

        # --- топ плейлистов / случайный трек ---
        'btn_top_pls': "🏆 ТОП плейлистов",
        'pl_top_title': "🏆 <b>Популярные плейлисты</b>\nСтраница {p} / {all}",
        'pl_top_empty': "🏆 Пока нет публичных плейлистов с просмотрами.",
        'btn_random': "🎲 Случайный трек",
        'random_empty': "🎲 Сейчас нет скачанных треков. Попробуйте поиск.",

        # --- шаринг трека ---
        'btn_share_tr': "✈️ Поделиться",
        'btn_add_fav_short': "⭐️",
        'btn_add_pl_short': "➕",
        'share_tr_text': "🎧 <b>Послушайте этот трек</b>\n\n{title}",

        # --- кнопки ---
        'btn_go': "🎉 Поехали!",
        'btn_next': "➡️ Дальше",
        'btn_done': "✅ Готово",
        'btn_back': "⬅️ Назад",
        'btn_cancel': "↖️ Отменить",
        'btn_to_menu': "↖️ В меню",
        'btn_to_playlists': "↖️ К списку плейлистов",
        'btn_to_pl': "↖️ К плейлисту",
        'btn_search': "🔎 Поиск",
        'btn_top': "⚡️ ТОП",
        'btn_favs': "⭐️ Избранное",
        'btn_playlists': "🎵 Плейлисты [{n}]",
        'btn_profile': "👤 Мой профиль",
        'btn_lang': "🌍 Язык: {lang}",
        'btn_visibility_on': "🙈 Скрыть профиль",
        'btn_visibility_off': "👁 Открыть профиль",
        'btn_about': "✏️ О себе",
        'btn_add_pl': "➕ В плейлист",
        'btn_add_fav': "⭐️ В избранное",
        'btn_in_fav': "⭐️ В избранном",
        'btn_create_pl': "➕ Создать плейлист",
        'btn_share_pl': "✈️ Поделиться плейлистом",
        'btn_listen_bot': "▶️ Слушать в боте",
        'btn_listen_all': "▶️ Слушать с начала",
        'btn_del_pl': "❌ Удалить плейлист",
        'btn_clear_pl': "🧹 Очистить все треки",
        'btn_next_track': "⏭ Следующий трек",
        'btn_dl': "⬇️ Загрузить трек",
        'btn_rules': "🛡 Правила использования",
        'btn_policy': "❗️ Политика конфиденциальности",
    },
    'en': {
        # --- greeting / menu ---
        'greet': "🎉 Hi, <b>{name}</b>!\n🔮 Let's enjoy free and unlimited music!\n\n🧩 <b>PS:</b> You came from {source}",
        'main_menu': "<b>💡 Main menu:</b>\n\n<i>💬 To search for a track press «Search».\nPlaylist tracks are easy to listen to with ▶ and switch with ⏭.</i>",
        'welcome_menu': "✅ Welcome to the menu!",
        'rules_read': "❓ Please read the platform rules carefully",
        'policy_read': "❗️ Please read the privacy policy carefully",
        'sub_prompt': "⚡️ Please subscribe to the channels",
        'sub_needed': "🧩 Please subscribe to all channels!",
        'banned': "You are banned. Please contact support.",
        'error_generic': "There was a problem processing your request. Please try again.",

        # --- search ---
        'search_prompt': "🔎 Enter a search query\n<i>Just type your query in any format</i>",
        'searching': "⚡️ Searching: <code>{query}</code>...",
        'no_results': "❌ Nothing found...\n<i>Try changing the query?</i>",
        'results_for': "🔎 Results for: <b>{query}</b>",
        'too_long': "⚠️ Query is too long (max 50 chars)",
        'no_more': "Nothing more found",
        'page_of': "{p} / {all}",

        # --- playback ---
        'uploading': "⏳ Uploading track to the cloud...",
        'already_open': "ℹ️ Track is already open",
        'liked': "👍 Liked!",
        'unliked': "👍 Like removed",
        'disliked': "👎 Disliked",
        'undisliked': "👎 Dislike removed",

        # --- favorites ---
        'fav_title': "⭐️ <b>Favorites</b> ({n} / {max})\nPage {p} / {all}",
        'fav_empty': "🎧 Your favorites are empty.\n<i>Add tracks with the ⭐️ button under a track.</i>",
        'fav_added': "✅ Track added to favorites",
        'fav_full': "⚠️ Favorites are limited to {max} tracks",
        'fav_removed': "🗑 Track removed from favorites",

        # --- playlists ---
        'pl_not_found': "❌ Playlist not found",
        'pl_name_prompt': "<b>Enter your playlist name</b>\n\n<i>It will be visible to other users</i>",
        'pl_name_long': "⚠️ Name is too long! Limit: 30 characters.",
        'pl_created': "✅ Playlist created!",
        'pl_limit': "⚠️ Playlist limit reached (limit: {max})",
        'pl_deleted': "🗑 Playlist: <code>{id}</code> deleted...",
        'pl_cleared': "🗑 All tracks of playlist <code>{id}</code> <b>deleted</b>",
        'pl_track_added': "✅ Track added to the playlist",
        'pl_track_full': "⚠️ No free slots in the playlist (limit: {max})",
        'pl_track_removed': "🗑 Track removed from the playlist",
        'pl_choose': "<b>Choose a playlist</b>:",
        'pl_list_empty': "🎧 You have no playlists :(\nTry creating a new one!",
        'pl_list_ok': "🎵 Your playlists:",
        'pl_end': "ℹ️ This is the last track of the playlist",
        'pl_share_text': "🎧 <b>Listen to my playlist: {name}</b>\n\nTotal tracks: {n}",
        'pl_tracks_count': "{n} tracks in the playlist",

        # --- ads ---
        'ad_msg': "📢 <b>Ad:</b> {text}\n<i>(shown every {n} tracks)</i>",

        # --- profile ---
        'about_prompt': "✏️ <b>Send your «About me» text</b>\n\n<i>Max 300 characters. It will be shown in your profile.</i>",
        'about_saved': "✅ «About me» saved!",
        'profile_hidden_self': "🙈 Your profile is hidden — other users can't see your info.",
        'profile_hidden': "🙈 This user has hidden their profile.",
        'visibility_off': "🙈 Profile hidden",
        'visibility_on': "👁 Profile visible",
        'lang_switched': "✅ Language changed!",

        # --- inline ---
        'inline_press_to_load': "🎵 <b>{query}</b>\n\n<i>Press «Download» to get the track</i>",
        'inline_listen_bot': "🔺 Go to the bot to listen to all tracks! Press the button: «Listen in bot»",
        'btn_continue': "▶️ Continue",
        'cb_expired': "⌛️ Link expired — please reopen the section",
        'top_chart': "🏆 Top chart: <b>TOP 1000</b>",
        'user_not_found': "❌ User not found",

        # --- help / rules / stats ---
        'btn_help': "ℹ️ Help",
        'help_title': "ℹ️ <b>How to use the bot</b>",
        'help_text': ("🔎 <b>Search</b> — just type a track or artist name.\n"
                      "⚡️ <b>TOP</b> — the most listened tracks.\n"
                      "⭐️ <b>Favorites</b> — tap ⭐️ under any track list.\n"
                      "🎵 <b>Playlists</b> — build your own collections.\n"
                      "🕘 <b>History</b> — what you listened to before.\n"
                      "🎲 <b>Random track</b> — when you don't know what to play.\n"
                      "✈️ <b>Share</b> — send tracks to other chats with the button under the track."),
        'rules_title': "🛡 <b>Terms of use</b>",
        'rules_text': ("1️⃣ The bot is for personal listening only.\n"
                       "2️⃣ Mass messaging and spam are forbidden.\n"
                       "3️⃣ Offensive content in playlist names is forbidden.\n"
                       "4️⃣ Violations may lead to an account ban.\n"
                       "5️⃣ Data is stored only to keep the bot working."),
        'policy_title': "❗️ <b>Privacy policy</b>",
        'policy_text': ("The bot stores only what it needs:\n"
                        "• your Telegram ID, name and referral source;\n"
                        "• interface language and your «about» text (if set);\n"
                        "• playlists, favorites and listening history.\n\n"
                        "We never share this data with third parties. You can request its deletion from the admin."),

        # --- profile stats ---
        'stats_title': "📊 <b>My stats</b>",
        'st_played': "▶️ Tracks played: <b>{n}</b>",
        'st_likes': "👍 Likes: <b>{n}</b>",
        'st_dislikes': "👎 Dislikes: <b>{n}</b>",
        'st_favs': "⭐️ In favorites: <b>{n}</b>",
        'st_pls': "🎵 Playlists: <b>{n}</b>",

        # --- history ---
        'btn_hist': "🕘 History",
        'hist_title': "🕘 <b>Listening history</b>\nPage {p} / {all} · total {n}",
        'hist_empty': "🕘 History is empty.\n\n<i>Play a track and it will show up here.</i>",
        'hist_cleared': "🧹 History cleared",
        'btn_clear_hist': "🧹 Clear history",

        # --- popular playlists / random track ---
        'btn_top_pls': "🏆 Top playlists",
        'pl_top_title': "🏆 <b>Popular playlists</b>\nPage {p} / {all}",
        'pl_top_empty': "🏆 No public playlists with views yet.",
        'btn_random': "🎲 Random track",
        'random_empty': "🎲 No downloaded tracks right now. Try search.",

        # --- track sharing ---
        'btn_share_tr': "✈️ Share",
        'btn_add_fav_short': "⭐️",
        'btn_add_pl_short': "➕",
        'share_tr_text': "🎧 <b>Listen to this track</b>\n\n{title}",

        # --- buttons ---
        'btn_go': "🎉 Let's go!",
        'btn_next': "➡️ Next",
        'btn_done': "✅ Done",
        'btn_back': "⬅️ Back",
        'btn_cancel': "↖️ Cancel",
        'btn_to_menu': "↖️ To menu",
        'btn_to_playlists': "↖️ To playlists",
        'btn_to_pl': "↖️ To playlist",
        'btn_search': "🔎 Search",
        'btn_top': "⚡️ TOP",
        'btn_favs': "⭐️ Favorites",
        'btn_playlists': "🎵 Playlists [{n}]",
        'btn_profile': "👤 My profile",
        'btn_lang': "🌍 Language: {lang}",
        'btn_visibility_on': "🙈 Hide profile",
        'btn_visibility_off': "👁 Show profile",
        'btn_about': "✏️ About me",
        'btn_add_pl': "➕ To playlist",
        'btn_add_fav': "⭐️ To favorites",
        'btn_in_fav': "⭐️ In favorites",
        'btn_create_pl': "➕ Create playlist",
        'btn_share_pl': "✈️ Share playlist",
        'btn_listen_bot': "▶️ Listen in bot",
        'btn_listen_all': "▶️ Play from start",
        'btn_del_pl': "❌ Delete playlist",
        'btn_clear_pl': "🧹 Clear all tracks",
        'btn_next_track': "⏭ Next track",
        'btn_dl': "⬇️ Download track",
        'btn_rules': "🛡 Terms of use",
        'btn_policy': "❗️ Privacy policy",
    },
}


def t(key: str, **kwargs) -> str:
    """Перевод строки для текущего пользователя (язык задан middleware)."""
    lang = current_lang.get()
    text = TRANSLATIONS.get(lang, {}).get(key) or TRANSLATIONS[Config.DEFAULT_LANG].get(key) or key
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            pass
    return text


async def detect_lang(user_id: int) -> str:
    """Язык пользователя: кэш -> БД -> настройки -> дефолт."""
    if user_id in _lang_cache:
        return _lang_cache[user_id]
    user = await bot_db.search_user(user_id)
    default_lang = await settings_db.get('DEFAULT_LANG')
    lang = (user or {}).get('lang') or default_lang
    if lang not in SUPPORTED_LANGS:
        lang = default_lang
    _lang_cache[user_id] = lang
    return lang


async def set_user_lang(user_id: int, lang: str) -> str:
    """Сохранить язык пользователя (БД + кэш)."""
    if lang not in SUPPORTED_LANGS:
        lang = await settings_db.get('DEFAULT_LANG')
    await bot_db.set_lang(user_id, lang)
    _lang_cache[user_id] = lang
    return lang


class I18nMiddleware(BaseMiddleware):
    """Устанавливает язык пользователя в contextvar на время обработки апдейта."""

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        user = getattr(event, 'from_user', None)
        token = None
        if user is not None:
            lang = await detect_lang(user.id)
            token = current_lang.set(lang)
        try:
            return await handler(event, data)
        finally:
            if token is not None:
                current_lang.reset(token)