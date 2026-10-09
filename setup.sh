#!/usr/bin/env bash
# setup.sh — one-shot install
set -e

pip install -r requirements.txt
playwright install chromium

echo ""
echo "✅ Setup done."
echo "Now edit config.py — set BOT_TOKEN and GROUP_ID — then:"
echo "  python bot.py"
