"""Telegram-бот интервальных повторений на SQLite и FSRS."""
import json
import logging
from datetime import timedelta, timezone

from dotenv import load_dotenv

load_dotenv("env.memotyabot")

from fsrs import Card, Rating, Scheduler
from telegram import Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

from config import load_config
from database import Database, utc_now
from scheduler import poll_due_cards

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

RATINGS = {
    "again": Rating.Again,
    "hard": Rating.Hard,
    "good": Rating.Good,
    "easy": Rating.Easy,
}

# Одно сообщение с карточкой должно уместиться в лимит Telegram (4096 символов).
MAX_FRONT_LEN = 1000
MAX_BACK_LEN = 2500

FSRS_SCHEDULER = Scheduler()


def serialize_fsrs_card(card: Card) -> dict:
    """Card.to_dict() доступен в fsrs 6.x (с классом Scheduler)."""
    return card.to_dict()


def restore_fsrs_card(raw: str) -> Card:
    """Card.from_dict() доступен в fsrs 6.x (с классом Scheduler)."""
    return Card.from_dict(json.loads(raw))


def humanize_delta(delta: timedelta) -> str:
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 90:
        return "in about a minute"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"in {minutes} min"
    hours = round(seconds / 3600)
    if hours < 48:
        return f"in {hours} h"
    return f"in {round(seconds / 86400)} days"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    context.application.bot_data["db"].ensure_user(user.id)
    await update.effective_message.reply_text(
        "Ready for a spaced repetition session?\n\n"
        "Create a card in a single message: the first line is the question, and the rest is the answer.\n"
        "The answer is hidden under a spoiler. Try to recall it, then select a rating.\n\n"
        "Commands: /help, /review, /stats, /pause, /resume, /delete"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["db"].ensure_user(update.effective_user.id)
    await update.effective_message.reply_text(
        "Card format: the first line is the question, and all subsequent lines are the answer.\n"
        "/review — Review due cards\n"
        "/stats — Statistics\n"
        "/pause — Disable auto-notifications\n"
        "/resume — Resume notifications \n"
        "/delete <id> — delete the card"
    )


async def add_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    db = context.application.bot_data["db"]
    db.ensure_user(user.id)

    lines = (message.text or "").splitlines()
    if len(lines) < 2 or not lines[0].strip() or not "\n".join(lines[1:]).strip():
        await message.reply_text(
            "Card recognition failed. Put the question on the first line and the answer on the second."
        )
        return

    front = lines[0].strip()
    back = "\n".join(lines[1:]).strip()
    if len(front) + len(back) > 3500:
        await message.reply_text("The card is too long (max ~3500 characters).")
        return
    if len(front) > MAX_FRONT_LEN or len(back) > MAX_BACK_LEN:
        await message.reply_text(
            f"The card is too long: the question can have up to {MAX_FRONT_LEN} characters "
            f"and the answer up to {MAX_BACK_LEN}."
        )
        return

    try:
        card_id = db.add_card(user.id, front, back, serialize_fsrs_card(Card()))
    except Exception:
        logger.exception("Failed to create card")
        await message.reply_text("Failed to save the card. Details have been logged.")
        return

    await message.reply_text(f"The card #{card_id} is saved ;-)")


async def review_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db = context.application.bot_data["db"]
    db.ensure_user(user_id)
    db.reset_failed_deliveries(user_id)  # ручной запуск = ещё одна попытка для неудачных отправок

    sent = await poll_due_cards(context, user_id=user_id)
    if sent:
        return  # сами карточки и есть ответ

    waiting = db.stats(user_id)["waiting"]
    if waiting:
        await update.effective_message.reply_text(
            f"Nothing new is due. {waiting} card(s) are already waiting for your rating above."
        )
    else:
        await update.effective_message.reply_text("Nothing is due right now.")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["db"].ensure_user(update.effective_user.id)
    values = context.application.bot_data["db"].stats(update.effective_user.id)
    await update.effective_message.reply_text(
        f"Cards in total: {values['total']}\n"
        f"Due: {values['due']}\n"
        f"Waiting for rating: {values['waiting']}\n"
        f"Reviews completed: {values['reviews']}"
    )


async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["db"].ensure_user(update.effective_user.id)
    context.application.bot_data["db"].set_paused(update.effective_user.id, True)
    await update.effective_message.reply_text("Automatic notifications are suspended. All cards and due dates have been preserved.")


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["db"].ensure_user(update.effective_user.id)
    context.application.bot_data["db"].set_paused(update.effective_user.id, False)
    await update.effective_message.reply_text("Automatic notifications have been resumed.")


async def delete_card_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["db"].ensure_user(update.effective_user.id)
    if not context.args:
        await update.effective_message.reply_text("Type the ID of the card for deleting: /delete <id>")
        return
    try:
        card_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("The ID must be a number. Example: /delete 1")
        return
    db = context.application.bot_data["db"]
    if db.delete_card(card_id, update.effective_user.id):
        await update.effective_message.reply_text(f"The card #{card_id} is no longer in the same world as us")
    else:
        await update.effective_message.reply_text(f"The card #{card_id} is not found")


async def rate_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not user:
        await query.answer("Access denied.", show_alert=True)
        return

    try:
        _, delivery_id_text, rating_key = query.data.split(":")
        delivery_id = int(delivery_id_text)
        rating = RATINGS[rating_key]
    except (ValueError, KeyError, AttributeError):
        await query.answer("Invalid button action.", show_alert=True)
        return

    db = context.application.bot_data["db"]
    delivery = db.get_delivery(delivery_id)
    if not delivery or delivery["user_id"] != user.id:
        await query.answer("The card is not found", show_alert=True)
        return
    if delivery["status"] != "sent":
        await query.answer("This card has already been rated or is unavailable.", show_alert=True)
        return

    try:
        card = restore_fsrs_card(delivery["fsrs_data"])
        reviewed_at = utc_now()
        updated_card, _review_log = FSRS_SCHEDULER.review_card(card, rating, review_datetime=reviewed_at)
        new_due = updated_card.due
        if new_due.tzinfo is None:
            new_due = new_due.replace(tzinfo=timezone.utc)
        ok = db.save_review(
            delivery=delivery,
            rating_name=rating_key,
            new_fsrs_data=serialize_fsrs_card(updated_card),
            new_due=new_due,
            reviewed_at=reviewed_at,
        )
        if not ok:
            await query.answer("This card has already been processed.", show_alert=True)
            return
    except Exception:
        logger.exception("Error processing the rating")
        await query.answer("Could not save your rating. Try again later.", show_alert=True)
        return

    await query.answer("Rating saved.")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.warning("Не удалось убрать кнопки у сообщения доставки %s", delivery_id)
    await query.message.reply_text(
        f"Rating saved. Next review {humanize_delta(new_due - reviewed_at)}."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Необработанная ошибка при обработке обновления", exc_info=context.error)


def main():
    cfg = load_config()
    db = Database(cfg["database_path"])
    app = Application.builder().token(cfg["token"]).build()
    app.bot_data["db"] = db

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("review", review_now))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("pause", pause))
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(CommandHandler("delete", delete_card_cmd))
    app.add_handler(CallbackQueryHandler(rate_card, pattern=r"^rate:"))
    # Только новые личные сообщения: правка сообщения не должна создавать дубликат карточки,
    # а карточки доставляются в личку, так что группы тут не нужны.
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE,
        add_card,
    ))
    app.add_error_handler(error_handler)

    app.job_queue.run_repeating(
        poll_due_cards,
        interval=cfg["poll_interval_seconds"],
        first=3,
        name="due-card-poller",
    )
    logger.info("Бот запускается.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
