"""Минимальный слой SQLite. Telegram-логики в этом модуле нет."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

# Сколько раз пытаемся отправить одну доставку, прежде чем сдаться
# (дальше её заново запускает только ручной /review).
MAX_DELIVERY_ATTEMPTS = 5


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """ISO-строка в UTC фиксированной ширины (всегда с микросекундами).

    Даты хранятся строками и сравниваются в SQL (due <= ?), поэтому формат
    должен быть одинаковым у всех значений.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def retry_delay(attempts: int) -> timedelta:
    """Пауза перед повторной отправкой: 1, 2, 4, 8 ... минут, не больше часа."""
    return timedelta(seconds=min(60 * 2 ** max(attempts - 1, 0), 3600))


class Database:
    def __init__(self, path: str):
        Path_dir = os.path.dirname(path)
        if Path_dir:
            os.makedirs(Path_dir, exist_ok=True)
        self.path = path
        self._init_schema()

    @contextmanager
    def connect(self):
        """Соединение с коммитом при успехе, откатом при ошибке и обязательным закрытием."""
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        with self.connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                front TEXT NOT NULL,
                back TEXT NOT NULL,
                created_at TEXT NOT NULL,
                due TEXT NOT NULL,
                fsrs_data TEXT NOT NULL,
                last_review TEXT,
                suspended INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_cards_due ON cards(user_id, due);

            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                due_snapshot TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                sent_at TEXT,
                telegram_message_id INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                next_attempt_at TEXT,
                UNIQUE(card_id, due_snapshot)
            );

            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                delivery_id INTEGER NOT NULL UNIQUE REFERENCES deliveries(id),
                user_id INTEGER NOT NULL,
                reviewed_at TEXT NOT NULL,
                rating TEXT NOT NULL,
                previous_due TEXT NOT NULL,
                new_due TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reviews_user_date ON reviews(user_id, reviewed_at);

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                paused INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            """)
            # Миграция для баз, созданных старой версией без next_attempt_at.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(deliveries)")}
            if "next_attempt_at" not in columns:
                conn.execute("ALTER TABLE deliveries ADD COLUMN next_attempt_at TEXT")

    # ---------- пользователи ----------

    def ensure_user(self, user_id: int):
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users(user_id, created_at) VALUES (?, ?)",
                (user_id, iso(utc_now())),
            )

    def is_paused(self, user_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT paused FROM users WHERE user_id=?", (user_id,)).fetchone()
        return bool(row["paused"]) if row else False

    def set_paused(self, user_id: int, paused: bool):
        self.ensure_user(user_id)
        with self.connect() as conn:
            conn.execute("UPDATE users SET paused=? WHERE user_id=?", (int(paused), user_id))

    # ---------- карточки ----------

    def add_card(self, user_id: int, front: str, back: str, fsrs_data: dict) -> int:
        now = iso(utc_now())
        self.ensure_user(user_id)
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO cards(user_id, front, back, created_at, due, fsrs_data)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, front, back, now, now, json.dumps(fsrs_data)),
            )
            return int(cur.lastrowid)

    def get_card(self, card_id: int, user_id: int):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM cards WHERE id=? AND user_id=?",
                (card_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def delete_card(self, card_id: int, user_id: int):
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM cards WHERE id=? AND user_id=?", (card_id, user_id))
            return cur.rowcount > 0

    def due_cards(self, now: datetime, limit: int = 50):
        now_s = iso(now)
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT c.* FROM cards c
                   WHERE c.suspended=0
                     AND c.due <= ?
                     AND NOT EXISTS (
                       SELECT 1 FROM deliveries d
                       WHERE d.card_id=c.id
                         AND (
                           d.status IN ('sent', 'pending')
                           OR (d.status = 'error' AND d.next_attempt_at > ?)
                         )
                     )
                   ORDER BY c.due ASC LIMIT ?""",
                (now_s, now_s, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- доставки ----------

    def create_delivery(self, card: dict) -> int | None:
        """Создаёт доставку для текущего срока карточки.

        Если для этого срока уже была неудачная попытка (status='error'),
        возвращает её в очередь только в том случае, если истекло время задержки.
        """
        now_s = iso(utc_now())
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id, status, attempts, next_attempt_at FROM deliveries WHERE card_id=? AND due_snapshot=?",
                (card["id"], card["due"]),
            ).fetchone()
            
            if existing is None:
                cur = conn.execute(
                    """INSERT INTO deliveries(card_id, user_id, due_snapshot, status, created_at)
                       VALUES (?, ?, ?, 'pending', ?)""",
                    (card["id"], card["user_id"], card["due"], now_s),
                )
                return int(cur.lastrowid)
                
            if existing["status"] in ("pending", "sent", "reviewed"):
                return None
                
            if existing["status"] == "error":
                if existing["attempts"] >= MAX_DELIVERY_ATTEMPTS:
                    return None  # Лимит исчерпан, сдаемся
                if existing["next_attempt_at"] and existing["next_attempt_at"] > now_s:
                    return None  # Время паузы ещё не вышло, ждём

            conn.execute("UPDATE deliveries SET status='pending' WHERE id=?", (existing["id"],))
            return int(existing["id"])

    def pending_deliveries(self, limit: int = 50, user_id: int | None = None,
                           include_paused: bool = False):
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT d.*, c.front, c.back, c.due AS current_due
                   FROM deliveries d
                   JOIN cards c ON c.id = d.card_id
                   JOIN users u ON u.user_id = d.user_id
                   WHERE d.status = 'pending' AND c.suspended = 0
                     AND (:include_paused = 1 OR u.paused = 0)
                     AND (:user_id IS NULL OR d.user_id = :user_id)
                   ORDER BY d.created_at ASC LIMIT :limit""",
                {"limit": limit, "user_id": user_id, "include_paused": int(include_paused)},
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_delivery_sent(self, delivery_id: int, message_id: int):
        with self.connect() as conn:
            conn.execute(
                """UPDATE deliveries SET status='sent', sent_at=?, telegram_message_id=?,
                   attempts=attempts+1, last_error=NULL, next_attempt_at=NULL
                   WHERE id=? AND status='pending'""",
                (iso(utc_now()), message_id, delivery_id),
            )

    def mark_delivery_error(self, delivery_id: int, error: str):
        """Фиксирует неудачную отправку и назначает следующую попытку с нарастающей паузой."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT attempts FROM deliveries WHERE id=? AND status='pending'",
                (delivery_id,),
            ).fetchone()
            if not row:
                return
            attempts = row["attempts"] + 1
            conn.execute(
                """UPDATE deliveries SET status='error', attempts=?, last_error=?,
                   next_attempt_at=? WHERE id=?""",
                (attempts, error[:500], iso(utc_now() + retry_delay(attempts)), delivery_id),
            )

    def reset_failed_deliveries(self, user_id: int) -> int:
        """Снимает паузу/лимит попыток с неудачных доставок пользователя (ручной /review)."""
        with self.connect() as conn:
            cur = conn.execute(
                """UPDATE deliveries SET attempts=0, next_attempt_at=NULL
                   WHERE user_id=? AND status='error'""",
                (user_id,),
            )
            return cur.rowcount

    def get_delivery(self, delivery_id: int):
        with self.connect() as conn:
            row = conn.execute(
                """SELECT d.*, c.front, c.back, c.fsrs_data, c.due AS current_due
                   FROM deliveries d JOIN cards c ON c.id=d.card_id WHERE d.id=?""",
                (delivery_id,),
            ).fetchone()
        return dict(row) if row else None

    def save_review(self, delivery: dict, rating_name: str, new_fsrs_data: dict,
                    new_due: datetime, reviewed_at: datetime):
        """Атомарно обновляет карточку, историю и статус доставки."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            fresh = conn.execute(
                "SELECT status FROM deliveries WHERE id=?",
                (delivery["id"],),
            ).fetchone()
            if not fresh or fresh["status"] != "sent":
                conn.rollback()
                return False

            conn.execute(
                """UPDATE cards SET due=?, fsrs_data=?, last_review=?
                   WHERE id=? AND user_id=?""",
                (iso(new_due), json.dumps(new_fsrs_data), iso(reviewed_at),
                 delivery["card_id"], delivery["user_id"]),
            )
            conn.execute(
                """INSERT INTO reviews(card_id, delivery_id, user_id, reviewed_at,
                   rating, previous_due, new_due) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (delivery["card_id"], delivery["id"], delivery["user_id"],
                 iso(reviewed_at), rating_name, delivery["current_due"], iso(new_due)),
            )
            conn.execute(
                "UPDATE deliveries SET status='reviewed' WHERE id=? AND status='sent'",
                (delivery["id"],),
            )
            return True  # коммит выполнит контекстный менеджер connect()

    def stats(self, user_id: int):
        now_s = iso(utc_now())
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) n FROM cards WHERE user_id=?", (user_id,)).fetchone()["n"]
            due = conn.execute(
                "SELECT COUNT(*) n FROM cards WHERE user_id=? AND due<=? AND suspended=0",
                (user_id, now_s),
            ).fetchone()["n"]
            reviews = conn.execute("SELECT COUNT(*) n FROM reviews WHERE user_id=?", (user_id,)).fetchone()["n"]
            waiting = conn.execute(
                """SELECT COUNT(*) n FROM deliveries WHERE user_id=? AND status='sent'""",
                (user_id,),
            ).fetchone()["n"]
        return {"total": total, "due": due, "reviews": reviews, "waiting": waiting}