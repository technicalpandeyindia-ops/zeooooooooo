"""
downloader.py — downloads videos, PDFs, and notes using yt-dlp / aiohttp.
"""

import asyncio
import os
import re
import aiohttp
from pathlib import Path
from typing import Optional

import config

os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)


def sanitize(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip()[:80]


def guess_ext(content_type: str, url: str) -> str:
    if "pdf" in content_type or url.lower().endswith(".pdf"):
        return ".pdf"
    if "zip" in content_type or url.lower().endswith(".zip"):
        return ".zip"
    if "mp4" in content_type or url.lower().endswith(".mp4"):
        return ".mp4"
    if "document" in content_type or url.lower().endswith(".docx"):
        return ".docx"
    return ".bin"


async def download_video(url: str, title: str) -> Optional[Path]:
    filename = sanitize(title)
    out_dir = Path(config.DOWNLOAD_DIR)
    out_tmpl = str(out_dir / f"{filename}.%(ext)s")

    print(f"[dl:video] Downloading: {title[:55]}")
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-part",
        "--merge-output-format", "mp4",
        "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "--max-filesize", f"{config.MAX_FILE_SIZE_MB}m",
        "-o", out_tmpl,
        url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            print(f"[dl:video] fail: {stderr.decode(errors='replace')[-200:]}")
            return None

        for ext in ("mp4", "mkv", "webm"):
            p = out_dir / f"{filename}.{ext}"
            if p.exists() and p.stat().st_size > 0:
                return p
    except Exception as e:
        print(f"[dl:video] Exception during download: {e}")
        return None

    return None


async def download_file(url: str, title: str) -> Optional[Path]:
    filename = sanitize(title)
    out_dir = Path(config.DOWNLOAD_DIR)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    print(f"[dl:file] Failed HTTP {resp.status} for {url}")
                    return None

                ct = resp.headers.get("Content-Type", "")
                ext = guess_ext(ct, url)
                target_path = out_dir / f"{filename}{ext}"

                with open(target_path, "wb") as f:
                    while True:
                        chunk = await resp.content.read(64 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)

                if target_path.exists() and target_path.stat().st_size > 0:
                    return target_path
    except Exception as e:
        print(f"[dl:file] Error downloading {url}: {e}")
        return None

    return None
