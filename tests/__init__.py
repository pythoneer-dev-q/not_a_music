"""Тесты проекта not_a_music (запуск: python -m unittest discover -s tests -t . -v)."""

import logging

# IsolatedAsyncioTestCase включает debug-режим asyncio, из-за чего в вывод летят
# предупреждения вида «Executing <Task ...> took N seconds». Для тестов они
# бесполезны — глушим логгер asyncio.
logging.getLogger("asyncio").setLevel(logging.ERROR)

