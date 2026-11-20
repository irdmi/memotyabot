"""Загрузка и проверка конфигурации из переменных окружения."""
import os
from pathlib import Path


def load_config() -> dict:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or token == "put_token_here":
        raise RuntimeError("Задайте TELEGRAM_BOT_TOKEN в переменных окружения.")

    db_path = Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3"))
    interval = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
    if interval < 5:
        raise RuntimeError("POLL_INTERVAL_SECONDS не должен быть меньше 5.")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    return {
        "token": token,
        "database_path": str(db_path),
        "poll_interval_seconds": interval,
    }
