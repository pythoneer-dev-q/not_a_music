# not_a_music

Телеграм-бот для бесплатного поиска и прослушивания музыки. Источник — SoundCloud,
резерв — YouTube (если трек защищён SoundCloud Go+/DRM).

## Возможности

- Поиск по SoundCloud с постраничной выдачей (премиум-треки SoundCloud Go+ отсеиваются).
- Резервный поиск/скачивание с YouTube, если трек недоступен целиком.
- Избранное, плейлисты, топ прослушиваний, инлайн-режим.
- Прогресс-бар загрузки трека прямо в сообщении.

## Быстрый старт

```bash
cp .env.example .env        # или создайте .env вручную
# укажите BOT_TOKEN, полученный у @BotFather
pip install -r requirements.txt
python main.py
```

## Развёртывание на сервере

```bash
sudo bash deploy.sh
```

Скрипт ставит зависимости, создаёт venv, обновляет `.env`, перезаписывает
systemd-юнит `not_a_music` и перезапускает сервис.

Токен можно передать явно:

```bash
sudo BOT_TOKEN='123456:AA...' bash deploy.sh
```

Иначе он берётся из `.env` рядом с проектом. Скрипт сразу проверяет токен
через `getMe` и предупредит, если Telegram отвечает «Unauthorized».

### Если в логах `TelegramUnauthorizedError: Telegram server says - Unauthorized`

Токен бота недействителен (отозван или указан неверно).

1. Получите новый токен у [@BotFather](https://t.me/BotFather) (`/mybots` → API Token).
2. Обновите `.env` (или запустите `deploy.sh` с `BOT_TOKEN=...`).
3. Перезапустите сервис: `systemctl restart not_a_music`.

Полезные команды:

```bash
systemctl status not_a_music
journalctl -u not_a_music -f
```

## Тесты

```bash
python -m unittest discover -s tests -t . -v
```

Тесты не требуют интернета и дополнительных зависимостей: проверяются прогресс-бар,
сборка названия трека, фильтр премиум-треков, пагинация поиска, а также что в
исходниках нет «вопросиков» вместо русских букв и иконок (защита от битой кодировки).

## Требования

См. `requirements.txt` (aiogram 3, aiohttp, motor, yt-dlp).
