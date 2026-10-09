"""
bot.py — Sahukgs Batch Extractor Telegram Bot (Interactive Flow)
Features:
  • extracts videos + PDFs + notes from sahukgs.com
  • skips already-sent content (per group tracker)
  • first run → sends everything; re-run → only new content
  • interactive conversation asking for Group ID, Batch URL, and SEND_VIDEO mode
  • /extract  /download  /status  /reset  /help
"""

import asyncio
import html
import os
import time
from pathlib import Path
from typing import Optional

from telegram import Bot, Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, ConversationHandler, filters
)
from telegram.error import TelegramError, RetryAfter

import config
from scraper import SahukgsScraper, Subject, ContentItem
from downloader import download_video, download_file
from db import is_sent, mark_sent, get_all_sent, clear_group

# ── Conversation States ───────────────────────────────────────────────────────
ASK_GROUP, ASK_BATCH, ASK_SEND_VIDEO = range(3)

# ── Per-User Session State ────────────────────────────────────────────────────
_sessions: dict[int, dict] = {}

# ── Helpers ───────────────────────────────────────────────────────────────────
EMOJI = {"video": "🎬", "pdf": "📄", "notes": "📝"}


async def safe_send(bot: Bot, chat_id, text: str, **kwargs):
    while True:
        try:
            return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramError as e:
            print(f"[bot] send error: {e}")
            return None


async def safe_send_doc(bot: Bot, chat_id, file_path: Path, caption: str):
    while True:
        try:
            with open(file_path, "rb") as f:
                return await bot.send_document(
                    chat_id=chat_id, document=f,
                    caption=caption[:1024], parse_mode=ParseMode.HTML
                )
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramError as e:
            print(f"[bot] doc error: {e}")
            return None


async def safe_send_video(bot: Bot, chat_id, file_path: Path, caption: str):
    while True:
        try:
            with open(file_path, "rb") as f:
                return await bot.send_video(
                    chat_id=chat_id, video=f,
                    caption=caption[:1024], parse_mode=ParseMode.HTML
                )
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramError as e:
            print(f"[bot] video error: {e}")
            return None


def fmt_item_link(idx: int, item: ContentItem) -> str:
    em = EMOJI.get(item.kind, "🔗")
    title = html.escape(item.title or f"Item {idx}")
    return f'{idx}. {em} <a href="{item.url}">{title}</a>'


# ── Interactive Conversation Handlers ─────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 1: Ask user for Telegram Group ID / Channel ID."""
    user_id = update.effective_user.id
    _sessions[user_id] = {"mode": "extract"}

    default_group = getattr(config, "GROUP_ID", "")
    keyboard = [[default_group]] if default_group and not default_group.startswith("-100XXX") else None
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True) if keyboard else ReplyKeyboardRemove()

    await update.message.reply_text(
        "👋 <b>Sahukgs Batch Extractor Bot</b>\n\n"
        "<b>Step 1/3:</b> Enter or choose the <b>Telegram Group / Channel ID</b> (e.g. <code>-1001234567890</code>):\n\n"
        "<i>(Make sure this bot is an Admin in the target group with post permissions!)</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )
    return ASK_GROUP


async def cmd_download_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 1 (Download Mode): Ask user for Telegram Group ID."""
    user_id = update.effective_user.id
    _sessions[user_id] = {"mode": "download"}

    default_group = getattr(config, "GROUP_ID", "")
    keyboard = [[default_group]] if default_group and not default_group.startswith("-100XXX") else None
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True) if keyboard else ReplyKeyboardRemove()

    await update.message.reply_text(
        "📥 <b>Download & Upload Pipeline</b>\n\n"
        "<b>Step 1/3:</b> Enter the target <b>Telegram Group / Channel ID</b>:",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )
    return ASK_GROUP


async def handle_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 2: Save Group ID & Ask for Batch URL."""
    user_id = update.effective_user.id
    group_text = update.message.text.strip()

    if user_id not in _sessions:
        _sessions[user_id] = {"mode": "extract"}
    _sessions[user_id]["group_id"] = group_text

    default_batch = getattr(config, "BATCH_URL", "")
    keyboard = [[default_batch]] if default_batch else None
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True) if keyboard else ReplyKeyboardRemove()

    await update.message.reply_text(
        f"✅ Group set to: <code>{group_text}</code>\n\n"
        "<b>Step 2/3:</b> Send the <b>Batch URL</b> (e.g. <code>https://www.sahukgs.com/batch/1159</code>):",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )
    return ASK_BATCH


async def handle_batch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 3: Save Batch URL & Ask for SEND_VIDEO option."""
    user_id = update.effective_user.id
    batch_text = update.message.text.strip()

    if user_id not in _sessions:
        _sessions[user_id] = {"mode": "extract"}
    _sessions[user_id]["batch_url"] = batch_text

    keyboard = [["Yes (Send Videos)", "No (Links/Docs Only)"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)

    await update.message.reply_text(
        f"✅ Batch URL set to: <code>{batch_text}</code>\n\n"
        "<b>Step 3/3:</b> Do you want to download and upload <b>Video files</b> directly to the group?",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )
    return ASK_SEND_VIDEO


async def handle_send_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Final Step: Save SEND_VIDEO choice and start pipeline."""
    user_id = update.effective_user.id
    choice = update.message.text.strip().lower()

    send_video = choice.startswith("yes") or choice == "true" or "send video" in choice
    session = _sessions.get(user_id, {})
    session["send_video"] = send_video

    group_id = session.get("group_id")
    batch_url = session.get("batch_url")
    mode = session.get("mode", "extract")

    await update.message.reply_text(
        "🚀 <b>Starting Pipeline...</b>\n\n"
        f"• <b>Target Group:</b> <code>{group_id}</code>\n"
        f"• <b>Batch URL:</b> <code>{batch_url}</code>\n"
        f"• <b>Send Videos:</b> <code>{'Yes' if send_video else 'No'}</code>\n"
        f"• <b>Mode:</b> <code>{mode}</code>\n\n"
        "<i>Scraping portal with Chromium engine, please wait...</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove()
    )

    # Launch worker in background
    asyncio.create_task(run_process(context.bot, update.effective_chat.id, session))
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel flow."""
    user_id = update.effective_user.id
    _sessions.pop(user_id, None)
    await update.message.reply_text("❌ Action cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset tracker database for a specific group."""
    user_id = update.effective_user.id
    session = _sessions.get(user_id, {})
    group_id = session.get("group_id") or getattr(config, "GROUP_ID", "")
    if group_id:
        clear_group(group_id)
        await update.message.reply_text(f"🧹 Cleared tracking history for <code>{group_id}</code>.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("⚠️ No group configured to reset. Run /start first.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View bot status and sent counts."""
    all_sent = get_all_sent()
    await update.message.reply_text(
        f"📊 <b>Bot Status</b>\n\n"
        f"• Total items sent recorded: <b>{len(all_sent)}</b>\n"
        f"• Default Batch URL: <code>{config.BATCH_URL}</code>",
        parse_mode=ParseMode.HTML
    )


# ── Execution Pipeline ────────────────────────────────────────────────────────

async def run_process(bot: Bot, notification_chat_id: int, session: dict):
    group_id = session["group_id"]
    batch_url = session["batch_url"]
    send_video = session["send_video"]

    try:
        scraper = SahukgsScraper(batch_url=batch_url)
        subjects = await scraper.scrape_batch(batch_url)

        if not subjects:
            await safe_send(bot, notification_chat_id, "⚠️ No subjects or content found for this batch.")
            return

        total_items = sum(len(s.items) for s in subjects)
        await safe_send(bot, notification_chat_id, f"🔍 Extracted <b>{len(subjects)}</b> subjects ({total_items} items). Dispatching to group...", parse_mode=ParseMode.HTML)

        sent_count = 0
        skipped_count = 0

        for subj in subjects:
            for idx, item in enumerate(subj.items, 1):
                item_key = f"{group_id}:{item.url}"
                if is_sent(item_key):
                    skipped_count += 1
                    continue

                if item.kind == "video":
                    if send_video:
                        local_file = await download_video(item.url, title=item.title)
                        if local_file and Path(local_file).exists():
                            await safe_send_video(bot, group_id, Path(local_file), caption=f"🎬 <b>{item.title}</b>\n📚 {subj.name}")
                            os.remove(local_file)
                        else:
                            await safe_send(bot, group_id, fmt_item_link(idx, item), parse_mode=ParseMode.HTML)
                    else:
                        await safe_send(bot, group_id, fmt_item_link(idx, item), parse_mode=ParseMode.HTML)

                elif item.kind in ["pdf", "notes"]:
                    local_file = await download_file(item.url, title=item.title)
                    if local_file and Path(local_file).exists():
                        await safe_send_doc(bot, group_id, Path(local_file), caption=f"📄 <b>{item.title}</b>\n📚 {subj.name}")
                        os.remove(local_file)
                    else:
                        await safe_send(bot, group_id, fmt_item_link(idx, item), parse_mode=ParseMode.HTML)

                mark_sent(item_key)
                sent_count += 1
                await asyncio.sleep(1.5)

        await safe_send(
            bot,
            notification_chat_id,
            f"✅ <b>Job Complete!</b>\n\n• Sent: <b>{sent_count}</b> new items\n• Skipped (already sent): <b>{skipped_count}</b>",
            parse_mode=ParseMode.HTML
        )

    except Exception as e:
        await safe_send(bot, notification_chat_id, f"❌ <b>Execution Error:</b> <code>{html.escape(str(e))}</code>", parse_mode=ParseMode.HTML)


# ── Main Polling Routine ──────────────────────────────────────────────────────

def main():
    token = getattr(config, "BOT_TOKEN", None) or os.environ.get("BOT_TOKEN")
    if not token or token == "YOUR_BOT_TOKEN":
        raise ValueError("BOT_TOKEN must be set in config.py or BOT_TOKEN env variable.")

    app = Application.builder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("extract", cmd_start),
            CommandHandler("download", cmd_download_entry)
        ],
        states={
            ASK_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group)],
            ASK_BATCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_batch)],
            ASK_SEND_VIDEO: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_send_video)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("status", cmd_status))

    print("[bot] Starting Telegram polling loop...")
    app.run_polling()


if __name__ == "__main__":
    main()
