"""
bot.py — Sahukgs Batch Extractor Telegram Bot
Features:
  • extracts videos + PDFs + notes from sahukgs.com
  • skips already-sent content (per group tracker)
  • first run → sends everything; re-run → only new content
  • bot asks user for group ID / batch URL via conversation
  • /extract  /download  /status  /reset  /setgroup  /help
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
from downloader import download_video, download_file, guess_ext
from db import is_sent, mark_sent, get_all_sent, clear_group

# ── conversation states ───────────────────────────────────────────────────────
ASK_GROUP, ASK_BATCH = range(2)

# ── per-user session state ────────────────────────────────────────────────────
# { user_id: { "group_id": ..., "batch_url": ..., "mode": "extract"|"download" } }
_sessions: dict[int, dict] = {}

# ── helpers ───────────────────────────────────────────────────────────────────

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
    em    = EMOJI.get(item.kind, "🔗")
    title = html.escape(item.title or f"Item {idx}")
    return f'{idx}. {em} <a href="{item.url}">{title}</a>'


def fmt_subject_header(subj: Subject, new_count: int) -> str:
    return (
        f"📚 <b>{html.escape(subj.name)}</b>\n"
        f"<i>{new_count} new item(s)</i>\n"
        "─────────────────"
    )


# ── /help ─────────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 <b>Sahukgs Extractor Bot</b>\n\n"
        "/extract   — send new lecture links (videos + PDFs + notes)\n"
        "/download  — download files and upload to group\n"
        "/setgroup  — change target group or batch URL\n"
        "/status    — show sent count and pending\n"
        "/reset     — clear sent history (re-send everything next run)\n"
        "/help      — this message\n\n"
        "<i>First run: sends everything.\n"
        "Next runs: only sends content not yet in the group.</i>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ── /setgroup conversation ────────────────────────────────────────────────────

async def cmd_setgroup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        "📌 Send me the <b>Group ID</b> (e.g. <code>-1001234567890</code>)\n\n"
        "Tip: add @userinfobot to your group to get its ID.",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_GROUP


async def got_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    gid  = update.message.text.strip()
    _sessions.setdefault(uid, {})["group_id"] = gid
    await update.message.reply_text(
        f"✅ Group set: <code>{html.escape(gid)}</code>\n\n"
        "Now send the <b>Batch URL</b> (e.g. <code>https://www.sahukgs.com/batch/1159</code>)\n"
        "or type <code>skip</code> to keep the default.",
        parse_mode=ParseMode.HTML,
    )
    return ASK_BATCH


async def got_batch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text.strip()
    if text.lower() != "skip":
        _sessions.setdefault(uid, {})["batch_url"] = text
    batch = _sessions.get(uid, {}).get("batch_url", config.BATCH_URL)
    group = _sessions.get(uid, {}).get("group_id", config.GROUP_ID)
    await update.message.reply_text(
        f"✅ All set!\n"
        f"Group: <code>{html.escape(group)}</code>\n"
        f"Batch: <code>{html.escape(batch)}</code>\n\n"
        "Use /extract or /download to start.",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ── resolve group + batch for user ───────────────────────────────────────────

def resolve(uid: int) -> tuple[str, str]:
    """Returns (group_id, batch_url). Falls back to config defaults."""
    sess = _sessions.get(uid, {})
    return (
        sess.get("group_id", config.GROUP_ID),
        sess.get("batch_url", config.BATCH_URL),
    )


# ── core extract logic ────────────────────────────────────────────────────────

async def run_extract(update: Update, ctx: ContextTypes.DEFAULT_TYPE, download: bool):
    uid         = update.effective_user.id
    group, batch = resolve(uid)

    # if group still unset, prompt
    if group == config.GROUP_ID and group == "-100XXXXXXXXXX":
        await update.message.reply_text(
            "⚠️ No group set yet. Use /setgroup first."
        )
        return

    mode_label = "download + upload" if download else "link-only"
    msg = await update.message.reply_text(
        f"🔍 Scraping batch...\nGroup: <code>{html.escape(group)}</code>\n"
        f"Mode: {mode_label}",
        parse_mode=ParseMode.HTML,
    )

    scraper  = SahukgsScraper(batch)
    subjects = await scraper.scrape()
    total    = sum(len(s.items) for s in subjects)

    already_sent = get_all_sent(group)
    new_subjects = []
    for subj in subjects:
        new_items = [i for i in subj.items if i.id not in already_sent]
        if new_items:
            new_subjects.append((subj, new_items))

    new_total = sum(len(items) for _, items in new_subjects)

    if new_total == 0:
        await msg.edit_text(
            f"✅ Scraped {total} items — all already sent to the group.\n"
            f"Nothing new to send."
        )
        return

    await msg.edit_text(
        f"✅ Found {total} total items.\n"
        f"🆕 New (not yet in group): <b>{new_total}</b>\n"
        f"Sending...",
        parse_mode=ParseMode.HTML,
    )

    bot   = ctx.bot
    sent  = 0
    skipped = 0

    for subj, new_items in new_subjects:
        await safe_send(
            bot, group,
            fmt_subject_header(subj, len(new_items)),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await asyncio.sleep(config.DELAY_BETWEEN_MSGS)

        for idx, item in enumerate(new_items, 1):
            success = False

            if download:
                success = await _send_as_file(bot, group, item, subj.name)

            if not success:
                # send as link
                await safe_send(
                    bot, group,
                    fmt_item_link(idx, item),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
                success = True

            if success:
                mark_sent(group, item.id, {"title": item.title, "kind": item.kind})
                sent += 1
            else:
                skipped += 1

            await asyncio.sleep(config.DELAY_BETWEEN_MSGS)

    await update.message.reply_text(
        f"✅ Done!\n"
        f"Sent: {sent} new items\n"
        f"Skipped/failed: {skipped}\n"
        f"Group: <code>{html.escape(group)}</code>",
        parse_mode=ParseMode.HTML,
    )


async def _send_as_file(bot: Bot, group: str,
                        item: ContentItem, subject_name: str) -> bool:
    caption = (
        f"{EMOJI.get(item.kind,'🔗')} <b>{html.escape(item.title[:80])}</b>\n"
        f"Subject: {html.escape(subject_name)}"
    )

    if item.kind == "video":
        path = await download_video(item.url, item.title)
        if path and path.exists():
            r = await safe_send_video(bot, group, path, caption)
            path.unlink(missing_ok=True)
            return r is not None

    elif item.kind in ("pdf", "notes"):
        ext  = guess_ext(item.url)
        path = await download_file(item.url, item.title, ext)
        if path and path.exists():
            r = await safe_send_doc(bot, group, path, caption)
            path.unlink(missing_ok=True)
            return r is not None

    return False  # download failed → caller will send link


# ── commands ──────────────────────────────────────────────────────────────────

async def cmd_extract(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await run_extract(update, ctx, download=False)


async def cmd_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await run_extract(update, ctx, download=True)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid          = update.effective_user.id
    group, batch = resolve(uid)
    sent_ids     = get_all_sent(group)
    await update.message.reply_text(
        f"📊 <b>Status</b>\n"
        f"Group: <code>{html.escape(group)}</code>\n"
        f"Batch: <code>{html.escape(batch)}</code>\n"
        f"Items sent so far: <b>{len(sent_ids)}</b>\n\n"
        f"Use /extract to send new items only.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid          = update.effective_user.id
    group, _     = resolve(uid)
    clear_group(group)
    await update.message.reply_text(
        f"🔄 History cleared for group <code>{html.escape(group)}</code>.\n"
        f"Next /extract will re-send everything.",
        parse_mode=ParseMode.HTML,
    )


# ── app entry ─────────────────────────────────────────────────────────────────

def main():
    print(f"[bot] starting — default batch: {config.BATCH_URL}")

    app = Application.builder().token(config.BOT_TOKEN).build()

    # setgroup conversation
    conv = ConversationHandler(
        entry_points=[CommandHandler("setgroup", cmd_setgroup)],
        states={
            ASK_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_group)],
            ASK_BATCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_batch)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("start",    cmd_help))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("extract",  cmd_extract))
    app.add_handler(CommandHandler("download", cmd_download))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("reset",    cmd_reset))

    print("[bot] polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
