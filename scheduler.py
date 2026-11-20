"""Периодический поиск и отправка карточек, срок которых наступил."""
import asyncio
import html
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TelegramError

logger = logging.getLogger(__name__)

# Периодический опрос и ручной /review не должны работать одновременно:
# иначе обе корутины могут взять одну и ту же 'pending' доставку и отправить её дважды.

_poll_lock = asyncio.Lock()


async def poll_due_cards(context):
    async with _poll_lock:
        await _poll_due_cards(context)


def now_utc():
    return datetime.now(timezone.utc)


async def poll_due_cards(context, user_id: int | None = None) -> int:
    async with _poll_lock:
        await _poll_due_cards(context, user_id)
    return 0


async def _poll_due_cards(context, user_id: int | None = None) -> int:
    """Создаёт доставки для наступивших карточек и отправляет их.

    Без user_id — обычный фоновый проход по всем пользователям (пауза учитывается).
    С user_id — ручной /review одного пользователя (пауза игнорируется: она
    отключает только автоматические уведомления).

    Возвращает число отправленных карточек.
    """
    app = context.application
    db = app.bot_data["db"]
    manual = user_id is not None
    sent_count = 0

    # Сначала создаём записи доставок для карточек, срок которых наступил.
    for card in db.due_cards(now_utc(), limit=50):
        db.create_delivery(card)

    # Затем отправляем записанные задания. Последовательно, по одному.
    for delivery in db.pending_deliveries(limit=50, user_id=user_id, include_paused=manual):
        front = html.escape(delivery["front"])
        back = html.escape(delivery["back"])
        message = f"<b>Repeat?</b>\n{front}\n\n<b>Answer:</b>\n<tg-spoiler>{back}</tg-spoiler>"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Again", callback_data=f"rate:{delivery['id']}:again"),
                InlineKeyboardButton("Hard", callback_data=f"rate:{delivery['id']}:hard"),
            ],
            [
                InlineKeyboardButton("Good", callback_data=f"rate:{delivery['id']}:good"),
                InlineKeyboardButton("Easy", callback_data=f"rate:{delivery['id']}:easy"),
            ],
        ])
        try:
            sent = await context.bot.send_message(
                chat_id=delivery["user_id"],
                text=message,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except RetryAfter as exc:
            # Telegram просит притормозить: не считаем это ошибкой доставки,
            # оставляем остальные задания в очереди до следующего прохода.
            logger.warning("Лимит Telegram, пауза %s с; остальные доставки подождут", exc.retry_after)
            break
        except TelegramError as exc:
            db.mark_delivery_error(delivery["id"], str(exc))
            logger.warning("Не удалось отправить доставку %s: %s", delivery["id"], exc)
            continue

        db.mark_delivery_sent(delivery["id"], sent.message_id)
        sent_count += 1
        await asyncio.sleep(0.15)

    return sent_count
