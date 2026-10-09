# Sahukgs Telegram Extractor Bot

Extracts videos, PDFs, and notes from sahukgs.com and dispatches them to specified Telegram groups / channels.

## Features
- **Interactive Prompts**: Prompts dynamically in chat for `Group ID`, `Batch URL`, and `SEND_VIDEO` mode.
- **De-duplication**: Tracks sent items in `sent_tracker.json` to prevent reposting existing content.
- **Playwright Scraping**: Handles SPA frontend rendering & network interception.
- **Direct Video / Doc Upload**: Supports both link previews and native video/PDF file uploads.

## Local Run
```bash
pip install -r requirements.txt
playwright install chromium
python bot.py
```

## Render Deployment
1. Connect repository to [Render](https://dashboard.render.com).
2. Use the provided `render.yaml` Blueprint or create a Docker Background Worker.
3. Supply `BOT_TOKEN`.
