"""
bot.py — Sahukgs Batch Extractor Telegram Bot (Interactive Flow + Keep-Alive + Resume & Anti-Duplicate Engine)
"""

import asyncio
import html
import logging
import os
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

import aiohttp
from telegram import Bot, Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, ConversationHandler, filters
)
from telegram.error import TelegramError, RetryAfter, Conflict

import config
from scraper import SahukgsScraper, Subject, ContentItem
from downloader import download_video, download_file
from db import is_already_sent, mark_as_sent, get_all_sent_count, clear_group

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ── Instant HTTP Health Check Server (Render Port Binding) ────────────────────

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - Sahukgs Telegram Bot is Alive\n")

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        print(f"[http] Health check server successfully bound to 0.0.0.0:{port}", flush=True)
        server.serve_forever()
    except Exception as e:
        print(f"[http] Failed to bind health server on port {port}: {e}", flush=True)


# Launch HTTP daemon thread immediately
threading.Thread(target=start_health_server, daemon=True).start()


# ── Render Free-Tier Anti-Sleep Self Pinger ───────────────────────────────────

def start_self_pinger():
    """Pings the local HTTP server every 5 minutes to keep Render alive during long batch runs."""
    port = int(os.environ.get("PORT", "10000"))
    external_url = os.environ.get("RENDER_EXTERNAL_URL")
    
    while True:
        time.sleep(280) # 4.6 minutes
        try:
            import urllib.request
            target = external_url if external_url else f"http://127.0.0.1:{port}/"
            req = urllib.request.Request(target, headers={"User-Agent": "Render-KeepAlive/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    logger.info("[keepalive] Self-ping successful — container kept awake.")
        except Exception as e:
            logger.debug(f"[keepalive] Ping notice: {e}")


threading.Thread(target=start_self_pinger, daemon=True).start()

# ── Conversation States & Strong Task References ──────────────────────────────
ASK_GROUP, ASK_BATCH, ASK_SEND_VIDEO = range(3)
_sessions: dict[int, dict] = {}
_background_tasks: set = set()

# ── Helpers ───────────────────────────────────────────────────────────────────
EMOJI = {"video": "🎬", "pdf": "📄", "notes": "📝"}


async def safe_send(bot: Bot, chat_id, text: str, **kwargs):
    while True:
        try:
            return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramError as e:
            logger.error(f"[bot] send error: {e}")
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
            logger.error(f"[bot] doc error: {e}")
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
            logger.error(f"[bot] video error: {e}")
            return None


def fmt_item_link(idx: int, item: ContentItem) -> str:
    em = EMOJI.get(item.kind, "🔗")
    title = html.escape(item.title or f"Item {idx}")
    return f'{em} <a href="{item.url}"><b>{title}</b></a>\n📚 <i>Subject: {html.escape(item.subject)}</i>'


# ── Global Error Handler ──────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        logger.warning("[bot] Conflict error: Another instance is polling. Telegram will reconnect automatically.")
    else:
        logger.error(f"[bot] Handler exception: {context.error}")


# ── Interactive Conversation Handlers ─────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 1: Ask user for Telegram Group ID / Channel ID."""
    user = update.effective_user
    user_id = user.id if user else update.effective_chat.id
    _sessions[user_id] = {"mode": "extract"}

    default_group = getattr(config, "GROUP_ID", "")
    keyboard = [[default_group]] if default_group and not default_group.startswith("-100XXX") else None
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True) if keyboard else ReplyKeyboardRemove()

    await update.effective_message.reply_text(
        "👋 <b>Sahukgs Batch Extractor Bot</b>\n\n"
        "<b>Step 1/3:</b> Enter or choose the <b>Telegram Group / Channel ID</b> (e.g. <code>-1001234567890</code>):\n\n"
        "<i>(Make sure this bot is added as Admin in the target group with post permissions!)</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )
    return ASK_GROUP


async def cmd_download_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 1 (Download Mode): Ask user for Telegram Group ID."""
    user = update.effective_user
    user_id = user.id if user else update.effective_chat.id
    _sessions[user_id] = {"mode": "download"}

    default_group = getattr(config, "GROUP_ID", "")
    keyboard = [[default_group]] if default_group and not default_group.startswith("-100XXX") else None
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True) if keyboard else ReplyKeyboardRemove()

    await update.effective_message.reply_text(
        "📥 <b>Download & Upload Pipeline</b>\n\n"
        "<b>Step 1/3:</b> Enter the target <b>Telegram Group / Channel ID</b>:",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )
    return ASK_GROUP


async def handle_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 2: Save Group ID & Ask for Batch URL."""
    user = update.effective_user
    user_id = user.id if user else update.effective_chat.id
    group_text = update.effective_message.text.strip()

    if user_id not in _sessions:
        _sessions[user_id] = {"mode": "extract"}
    _sessions[user_id]["group_id"] = group_text

    default_batch = getattr(config, "BATCH_URL", "")
    keyboard = [[default_batch]] if default_batch else None
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True) if keyboard else ReplyKeyboardRemove()

    await update.effective_message.reply_text(
        f"✅ Group set to: <code>{group_text}</code>\n\n"
        "<b>Step 2/3:</b> Send the <b>Batch URL</b> (e.g. <code>https://www.sahukgs.com/batch/1159</code> or <code>lesson_id=14392</code>):",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )
    return ASK_BATCH


async def handle_batch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 3: Save Batch URL & Ask for SEND_VIDEO option."""
    user = update.effective_user
    user_id = user.id if user else update.effective_chat.id
    batch_text = update.effective_message.text.strip()

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
    user = update.effective_user
    user_id = user.id if user else update.effective_chat.id
    choice = update.effective_message.text.strip().lower()

    send_video = choice.startswith("yes") or choice == "true" or "send video" in choice
    session = _sessions.get(user_id, {})
    session["send_video"] = send_video

    group_id = session.get("group_id")
    batch_url = session.get("batch_url")
    mode = session.get("mode", "extract")

    await update.effective_message.reply_text(
        "🚀 <b>Starting Sequential Pipeline (Skipping Previously Sent Items)...</b>\n\n"
        f"• <b>Target Group:</b> <code>{group_id}</code>\n"
        f"• <b>Batch URL:</b> <code>{batch_url}</code>\n"
        f"• <b>Send Videos:</b> <code>{'Yes' if send_video else 'No'}</code>\n"
        f"• <b>Mode:</b> <code>{mode}</code>\n\n"
        "<i>Checking previously sent items & resuming where you left off...</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove()
    )

    task = asyncio.create_task(run_process(context.bot, update.effective_chat.id, session))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel flow."""
    user = update.effective_user
    user_id = user.id if user else update.effective_chat.id
    _sessions.pop(user_id, None)
    await update.message.reply_text("❌ Action cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset tracker database for a specific group."""
    user = update.effective_user
    user_id = user.id if user else update.effective_chat.id
    session = _sessions.get(user_id, {})
    group_id = session.get("group_id") or getattr(config, "GROUP_ID", "")
    if group_id:
        clear_group(group_id)
        await update.effective_message.reply_text(f"🧹 Cleared tracking history for <code>{group_id}</code>. All batch items will now send from scratch.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("⚠️ No group configured to reset. Run /start first.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View bot status and sent counts."""
    count = get_all_sent_count()
    await update.effective_message.reply_text(
        f"📊 <b>Bot Status</b>\n\n"
        f"• Total tracked items already sent: <b>{count}</b>\n"
        f"• Default Batch URL: <code>{config.BATCH_URL}</code>",
        parse_mode=ParseMode.HTML
    )


# ── Sequential Execution Pipeline ─────────────────────────────────────────────

async def run_process(bot: Bot, notification_chat_id: int, session: dict):
    group_id = session.get("group_id")
    batch_url = session.get("batch_url")
    send_video = session.get("send_video", True)

    try:
        scraper = SahukgsScraper(batch_url=batch_url)
        subjects = await scraper.scrape_batch(batch_url)

        if not subjects:
            await safe_send(bot, notification_chat_id, "⚠️ No subjects or content found for this batch.")
            return

        total_items = sum(len(s.items or []) for s in (subjects or []))
        await safe_send(
            bot,
            notification_chat_id,
            f"🔍 <b>Found {len(subjects)} Folders ({total_items} Total Items)</b>\n"
            "Checking duplicates and sending only remaining new items...",
            parse_mode=ParseMode.HTML
        )

        sent_count = 0
        skipped_count = 0

        for subj in (subjects or []):
            if not subj or not subj.items:
                continue

            for idx, item in enumerate((subj.items or []), 1):
                if not item:
                    continue

                # Multi-key duplicate check (matches by URL, Title, and Subject)
                if is_already_sent(group_id, subj.name, item.title, item.url):
                    skipped_count += 1
                    continue

                caption_text = f"<b>{html.escape(item.title or '')}</b>\n📚 <i>Subject: {html.escape(subj.name or '')}</i>"

                if item.kind == "video":
                    if send_video:
                        local_file = await download_video(item.url, title=item.title)
                        if local_file and Path(local_file).exists():
                            await safe_send_video(bot, group_id, Path(local_file), caption=f"🎬 {caption_text}")
                            try:
                                os.remove(local_file)
                            except Exception:
                                pass
                        else:
                            await safe_send(bot, group_id, fmt_item_link(idx, item), parse_mode=ParseMode.HTML)
                    else:
                        await safe_send(bot, group_id, fmt_item_link(idx, item), parse_mode=ParseMode.HTML)

                elif item.kind in ["pdf", "notes"]:
                    local_file = await download_file(item.url, title=item.title)
                    if local_file and Path(local_file).exists():
                        await safe_send_doc(bot, group_id, Path(local_file), caption=f"📄 {caption_text}")
                        try:
                            os.remove(local_file)
                        except Exception:
                            pass
                    else:
                        await safe_send(bot, group_id, fmt_item_link(idx, item), parse_mode=ParseMode.HTML)

                # Mark item as sent under all matching keys
                mark_as_sent(group_id, subj.name, item.title, item.url)
                sent_count += 1

                # Send progress update every 20 newly sent items
                if sent_count > 0 and sent_count % 20 == 0:
                    await safe_send(bot, notification_chat_id, f"📦 <b>Live Progress:</b> Sent {sent_count} new items (Skipped {skipped_count} existing)...")

                await asyncio.sleep(2.0)

        await safe_send(
            bot,
            notification_chat_id,
            f"✅ <b>Job Complete!</b>\n\n"
            f"• Newly Sent: <b>{sent_count}</b> items\n"
            f"• Skipped (Already in group): <b>{skipped_count}</b> items",
            parse_mode=ParseMode.HTML
        )

    except Exception as e:
        logger.exception("Error in run_process")
        await safe_send(bot, notification_chat_id, f"❌ <b>Execution Error:</b> <code>{html.escape(str(e))}</code>", parse_mode=ParseMode.HTML)


# ── Main Entrypoint ───────────────────────────────────────────────────────────

def main():
    token = getattr(config, "BOT_TOKEN", None) or os.environ.get("BOT_TOKEN")
    if not token or token == "YOUR_BOT_TOKEN":
        logger.error("[bot] ❌ CRITICAL: BOT_TOKEN is missing! Set BOT_TOKEN in Render Environment Variables.")
        while True:
            time.sleep(60)

    app = Application.builder().token(token).build()
    app.add_error_handler(error_handler)

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
        allow_reentry=True
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("status", cmd_status))

    logger.info("[bot] Starting standard Telegram polling loop...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
