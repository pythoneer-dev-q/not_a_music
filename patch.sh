#!/bin/bash
set -e

echo "=========================================================="
echo "🔧 Запуск автоматического патча и обновления бота not_a_music"
echo "=========================================================="

if [ "$EUID" -ne 0 ]; then
    echo "⛔ Ошибка: запустите от имени root: sudo bash patch.sh"
    exit 1
fi

APP_DIR="/opt/not_a_music"
CURRENT_DIR=$(pwd)

echo "1️⃣  Останавливаю старые/зависшие процессы бота..."
systemctl stop not_a_music 2>/dev/null || true
pkill -9 -f "python3 -m main" 2>/dev/null || true
pkill -9 -f "python3 main.py" 2>/dev/null || true

echo "2️⃣  Устанавливаю системные зависимости (Node.js, FFmpeg, Python)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv ffmpeg curl git nodejs || true

echo "3️⃣  Обновляю репозиторий через Git..."
git fetch origin main || true
git reset --hard origin/main || true

echo "4️⃣  Синхронизирую файлы в $APP_DIR..."
mkdir -p "$APP_DIR"
if [ "$CURRENT_DIR" != "$APP_DIR" ]; then
    # Копируем всё кроме venv и .env (чтобы сохранить токен и базу)
    cp -rf "$CURRENT_DIR"/* "$APP_DIR"/ 2>/dev/null || true
    if [ -f "$CURRENT_DIR/.env" ] && [ ! -f "$APP_DIR/.env" ]; then
        cp -f "$CURRENT_DIR/.env" "$APP_DIR/.env"
    fi
fi

cd "$APP_DIR"

echo "5️⃣  Обновляю виртуальное окружение и пакеты..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

./venv/bin/pip install --upgrade pip setuptools wheel
./venv/bin/pip install --upgrade yt-dlp aiohttp
./venv/bin/pip install -r requirements.txt

# Очищаем pycache
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

echo "6️⃣  Проверяю статус сервиса not_a_music..."
if [ -f "/etc/systemd/system/not_a_music.service" ]; then
    systemctl daemon-reload
    systemctl restart not_a_music
    sleep 2
    echo "Статус службы:"
    systemctl status not_a_music --no-pager -l | head -n 20 || true
    echo ""
    echo "Последние логи (journalctl):"
    journalctl -u not_a_music -n 15 --no-pager || true
else
    echo "ℹ️  Служба systemd не найдена. Если хотите запустить бота вручную:"
    echo "   cd $APP_DIR && ./venv/bin/python3 -m main"
fi

echo "=========================================================="
echo "✅ Патч успешно применён! Бот обновлён и запущен."
echo "=========================================================="
