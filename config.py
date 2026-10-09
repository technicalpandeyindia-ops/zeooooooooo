import os

# ─── CONFIG — reads from env vars (Render) or falls back to defaults ──────────

BOT_TOKEN   = os.environ.get("BOT_TOKEN",   "YOUR_BOT_TOKEN")
GROUP_ID    = os.environ.get("GROUP_ID",    "-100XXXXXXXXXX")
BATCH_URL   = os.environ.get("BATCH_URL",   "https://www.sahukgs.com/batch/1159")

DOWNLOAD_DIR      = os.environ.get("DOWNLOAD_DIR",     "./downloads")
MAX_FILE_SIZE_MB  = int(os.environ.get("MAX_FILE_SIZE_MB", "50"))
SEND_VIDEOS       = os.environ.get("SEND_VIDEOS", "true").lower() == "true"
DELAY_BETWEEN_MSGS = float(os.environ.get("DELAY_BETWEEN_MSGS", "2"))

HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"
PAGE_TIMEOUT = int(os.environ.get("PAGE_TIMEOUT", "60000"))
