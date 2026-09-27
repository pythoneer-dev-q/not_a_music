#!/bin/bash
set -e

echo "=========================================="
echo "🚀 Запускаю развёртывание бота not_a_music..."
echo "=========================================="

if [ "$EUID" -ne 0 ]; then
    echo "⛔ Ошибка: запустите скрипт от имени root (sudo bash deploy.sh)"
    exit 1
fi

APP_DIR="/opt/not_a_music"
CURRENT_DIR=$(pwd)

echo "📦 Обновляю список пакетов и ставлю зависимости..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv ffmpeg curl git nodejs

if ! command -v mongod &> /dev/null && ! command -v mongodb &> /dev/null; then
    echo "🗄 Ставлю MongoDB..."
    apt-get install -y --no-install-recommends mongodb || true
fi

systemctl daemon-reload || true
systemctl enable mongod 2>/dev/null || systemctl enable mongodb 2>/dev/null || true
systemctl restart mongod 2>/dev/null || systemctl restart mongodb 2>/dev/null || true

echo "📁 Готовлю каталог приложения: $APP_DIR"
mkdir -p "$APP_DIR"

if [ "$CURRENT_DIR" != "$APP_DIR" ]; then
    echo "📂 Копирую файлы проекта..."
    cp -rf "$CURRENT_DIR"/* "$APP_DIR"/ 2>/dev/null || true
    rm -rf "$APP_DIR/venv" 2>/dev/null || true
fi

cd "$APP_DIR"

echo "🐍 Настраиваю виртуальное окружение Python 3..."
python3 -m venv venv
./venv/bin/pip install --upgrade pip setuptools wheel
./venv/bin/pip install -r requirements.txt

# ─────────── .env и токен бота ───────────
ENV_FILE="$APP_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "📝 Создаю файл настроек .env..."
    cat << 'ENVEOF' > "$ENV_FILE"
MONGO_URI='mongodb://127.0.0.1:27017'
MONGO_DB_NAME='nt_music'
BOT_TOKEN='YOUR_BOT_TOKEN_HERE'

USER_SAVED_LIMIT=50
FAV_PAGE_SIZE=10
TRACKS_MAX=50
PLAYLISTS_MAX=25
SEARCH_LIMIT=7
AD_EVERY_N=5
DEFAULT_LANG=ru
IS_DW_ALL=False
SUDO_ADMIN=7194401988
ENVEOF
fi

# Источник токена: переменная окружения BOT_TOKEN или .env рядом с проектом.
TOKEN_SOURCE="${BOT_TOKEN:-}"
if [ -z "$TOKEN_SOURCE" ] && [ -f "$CURRENT_DIR/.env" ]; then
    TOKEN_SOURCE=$(grep -E "^BOT_TOKEN=" "$CURRENT_DIR/.env" | head -n 1 | cut -d '=' -f 2- | tr -d "'\" ")
fi

if [ -n "$TOKEN_SOURCE" ]; then
    TOKEN_ESCAPED=$(printf '%s' "$TOKEN_SOURCE" | sed -e 's/[\/&|]/\\&/g')
    if grep -qE "^BOT_TOKEN=" "$ENV_FILE"; then
        sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN='${TOKEN_ESCAPED}'|" "$ENV_FILE"
    else
        printf "\nBOT_TOKEN='%s'\n" "$TOKEN_SOURCE" >> "$ENV_FILE"
    fi
    echo "🔑 BOT_TOKEN обновлён в $ENV_FILE"
fi

# Проверяем, что в .env лежит настоящий токен, а не заглушка.
CURRENT_TOKEN=$(grep -E "^BOT_TOKEN=" "$ENV_FILE" | head -n 1 | cut -d '=' -f 2- | tr -d "'\" ")
if [ -z "$CURRENT_TOKEN" ] || [ "$CURRENT_TOKEN" = "YOUR_BOT_TOKEN_HERE" ]; then
    echo "⚠  В $ENV_FILE не указан токен бота."
    echo "   Получите его у @BotFather и запустите:  BOT_TOKEN='123456:AA...' bash deploy.sh"
elif ! [[ "$CURRENT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; then
    echo "⚠  BOT_TOKEN выглядит некорректно (ожидается формат 123456:AA...)."
    echo "   Проверьте значение в $ENV_FILE — иначе Telegram ответит «Unauthorized»."
fi

# Проверяем токен у Telegram, чтобы не гонять systemd в бесконечных перезапусках.
if [ -n "$CURRENT_TOKEN" ] && [ "$CURRENT_TOKEN" != "YOUR_BOT_TOKEN_HERE" ]; then
    echo "🔍 Проверяю токен у Telegram..."
    if curl -fsS --max-time 15 "https://api.telegram.org/bot${CURRENT_TOKEN}/getMe" > /dev/null 2>&1; then
        echo "✅ Токен принят Telegram."
    else
        echo "⚠  Telegram отклонил токен (Unauthorized)."
        echo "   Получите новый токен у @BotFather и запустите deploy.sh заново."
    fi
fi

SERVICE_FILE="/etc/systemd/system/not_a_music.service"
echo "⚙  Обновляю сервис systemd ($SERVICE_FILE)..."

cat << 'SVCEOF' > "$SERVICE_FILE"
[Unit]
Description=Not A Music Telegram Bot
After=network.target mongod.service mongodb.service
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
User=root
WorkingDirectory=/opt/not_a_music
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONUTF8=1
Environment=PYTHONIOENCODING=utf-8
ExecStart=/opt/not_a_music/venv/bin/python main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=not_a_music

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable not_a_music
systemctl restart not_a_music || true

echo "=========================================="
echo "✅ Развёртывание завершено!"
echo "Статус сервиса: systemctl status not_a_music"
echo "Живые логи:     journalctl -u not_a_music -f"
echo "=========================================="
