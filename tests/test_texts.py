"""Тесты текстов интерфейса и защиты от «вопросиков» вместо символов."""

import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", ".kilo", "__pycache__", "venv", ".venv", "node_modules"}
SOURCE_EXTS = {".py", ".sh", ".txt", ".md", ".yml", ".yaml", ".cfg", ".toml", ".json"}
# Ищем два и более вопросительных знака подряд — признак битой кодировки.
MOJIBAKE_RUN = re.compile(r"\?" + "{2,}")
REPLACEMENT_CHAR = "\ufffd"
# Токен бота не должен быть зашит в коде/скриптах.
HARDCODED_TOKEN = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")


def _source_files():
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if os.path.splitext(name)[1].lower() in SOURCE_EXTS or name == ".env":
                yield os.path.join(root, name)


def _read(rel_path: str) -> str:
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


class TestNoMojibake(unittest.TestCase):
    def test_all_sources_are_valid_utf8(self):
        broken = []
        for path in _source_files():
            with open(path, "rb") as fh:
                raw = fh.read()
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError as e:
                broken.append(f"{os.path.relpath(path, REPO_ROOT)}: {e}")
        self.assertEqual(broken, [], f"Файлы не в UTF-8: {broken}")

    def test_no_replacement_characters(self):
        broken = []
        for path in _source_files():
            text = _read(os.path.relpath(path, REPO_ROOT))
            if REPLACEMENT_CHAR in text:
                broken.append(os.path.relpath(path, REPO_ROOT))
        self.assertEqual(broken, [], f"Найден символ-замена U+FFFD: {broken}")

    def test_no_question_mark_runs(self):
        """Ни в одном файле не должно быть вопросиков вместо русских букв/иконок."""
        broken = []
        for path in _source_files():
            rel = os.path.relpath(path, REPO_ROOT)
            for number, line in enumerate(_read(rel).splitlines(), 1):
                if MOJIBAKE_RUN.search(line):
                    broken.append(f"{rel}:{number}: {line.strip()[:70]}")
        self.assertEqual(broken, [], "Найдены «вопросики» вместо символов:\n" + "\n".join(broken))


class TestDeployScript(unittest.TestCase):
    def test_script_has_no_mojibake(self):
        self.assertIsNone(MOJIBAKE_RUN.search(_read("deploy.sh")))

    def test_script_starts_with_shebang_and_strict_mode(self):
        text = _read("deploy.sh")
        self.assertTrue(text.startswith("#!/bin/bash"))
        self.assertIn("set -e", text)

    def test_token_is_synced_from_env_or_project_env(self):
        text = _read("deploy.sh")
        self.assertIn("BOT_TOKEN", text)
        self.assertIn("getMe", text)          # токен проверяется у Telegram
        self.assertIn("sed -i", text)         # BOT_TOKEN обновляется в /opt/.../.env

    def test_no_hardcoded_bot_token(self):
        self.assertIsNone(HARDCODED_TOKEN.search(_read("deploy.sh")),
                          "В deploy.sh не должно быть настоящего токена бота")

    def test_systemd_limits_restarts(self):
        text = _read("deploy.sh")
        self.assertIn("StartLimitBurst", text)
        self.assertIn("RestartSec", text)
        self.assertIn("PYTHONIOENCODING=utf-8", text)


class TestBotStartup(unittest.TestCase):
    def test_main_handles_unauthorized(self):
        text = _read("main.py")
        self.assertIn("TelegramUnauthorizedError", text)
        self.assertIn("delete_webhook", text)
        self.assertIn("bot.session.close()", text)

    def test_main_validates_token(self):
        text = _read("main.py")
        self.assertIn("_validate_token", text)
        self.assertIn("BOT_TOKEN", text)


class TestUiTexts(unittest.TestCase):
    def test_kbs_duration_placeholder(self):
        text = _read("utils/kbs.py")
        self.assertIn('if duration else "--:--"', text)

    def test_progress_reporter_texts(self):
        text = _read("core/app_logic.py")
        self.assertIn("Загружаю трек", text)
        self.assertIn("Загрузка…", text)

    def test_service_handler_drm_text(self):
        text = _read("handlers/service_handlers.py")
        self.assertIn("SoundCloud Go+", text)
        self.assertIn("track_display_name", text)

    def test_i18n_has_no_question_mark_runs(self):
        for line in _read("utils/i18n.py").splitlines():
            self.assertIsNone(MOJIBAKE_RUN.search(line), line)


if __name__ == "__main__":
    unittest.main()
