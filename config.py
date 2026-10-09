import os

# ─── CONFIG — reads from env vars (Render) or falls back to defaults ──────────

BOT_TOKEN   = os.environ.get("BOT_TOKEN",   "")
GROUP_ID    = os.environ.get("GROUP_ID",    "")
BATCH_URL   = os.environ.get("BATCH_URL",   "https://www.sahukgs.com/batch/1159")

DOWNLOAD_DIR      = os.environ.get("DOWNLOAD_DIR",      "/tmp/downloads")
MAX_FILE_SIZE_MB  = int(os.environ.get("MAX_FILE_SIZE_MB", "2000"))  # TG bot limit is 2GB
SEND_VIDEO        = os.environ.get("SEND_VIDEO", "true").lower() in ("true", "1", "yes")
POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "300"))
