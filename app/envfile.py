"""Минимальный загрузчик .env (без python-dotenv).

Приоритет: переменные окружения сильнее файла. Всё, что уже есть в os.environ
(в Docker это то, что выставил compose), файл не перезаписывает. Если ключ повторён
внутри самого файла, действует последняя строка (как в compose).
"""
import os


def parse_env(path: str) -> dict:
    values = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    return values


def load_env(path: str = ".env"):
    if not os.path.exists(path):
        return
    for key, value in parse_env(path).items():
        os.environ.setdefault(key, value)
