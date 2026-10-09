"""
downloader.py — downloads videos, PDFs, and notes using yt-dlp / requests.
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


async def download_video(url: str, title: str) -> Optional[Path]:
    filename = sanitize(title)
    out_dir  = Path(config.DOWNLOAD_DIR)
    out_tmpl = str(out_dir / f"{filename}.%(ext)s")

    print(f"[dl:video] {title[:55]}")
    cmd = [
        "yt-dlp",
        "--no-playlist", "--no-part",
        "--merge-output-format", "mp4",
        "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "--max-filesize", f"{config.MAX_FILE_SIZE_MB}m",
        "-o", out_tmpl,
        url,
    ]
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
        if p.exists():
            size_mb = p.stat().st_size / 1_048_576
            if size_mb > config.MAX_FILE_SIZE_MB:
                p.unlink(missing_ok=True)
                return None
            print(f"[dl:video] ok  {p.name}  ({size_mb:.1f} MB)")
            return p

    # glob fallback
    for p in out_dir.glob(f"{filename}.*"):
        return p
    return None


async def download_file(url: str, title: str, ext: str = "pdf") -> Optional[Path]:
    """Download a PDF / notes file via HTTP."""
    filename = sanitize(title)
    dest     = Path(config.DOWNLOAD_DIR) / f"{filename}.{ext}"

    print(f"[dl:{ext}] {title[:55]}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as r:
                if r.status != 200:
                    print(f"[dl:{ext}] HTTP {r.status}")
                    return None
                size = 0
                with open(dest, "wb") as f:
                    async for chunk in r.content.iter_chunked(65536):
                        f.write(chunk)
                        size += len(chunk)
                        if size > config.MAX_FILE_SIZE_MB * 1_048_576:
                            f.close()
                            dest.unlink(missing_ok=True)
                            print(f"[dl:{ext}] too large, skipped")
                            return None
        size_mb = dest.stat().st_size / 1_048_576
        print(f"[dl:{ext}] ok  {dest.name}  ({size_mb:.1f} MB)")
        return dest
    except Exception as e:
        print(f"[dl:{ext}] error: {e}")
        dest.unlink(missing_ok=True)
        return None


def guess_ext(url: str) -> str:
    m = re.search(r"\.(pdf|docx|doc|pptx|ppt|zip|rar|txt)(\?|$)", url, re.I)
    return m.group(1).lower() if m else "pdf"
