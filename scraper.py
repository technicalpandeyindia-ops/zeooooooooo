"""
scraper.py — extracts subjects + video links + PDFs + notes from sahukgs.com SPA.
Uses Playwright headless Chromium + network interception.
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Optional

from playwright.async_api import async_playwright, Page, Response

import config


# ── data models ───────────────────────────────────────────────────────────────

@dataclass
class ContentItem:
    """A single piece of content — video, PDF, or note."""
    id: str                     # unique stable ID for dedup tracking
    title: str
    kind: str                   # "video" | "pdf" | "notes"
    url: str                    # playable / downloadable URL
    subject: str = ""
    thumbnail: str = ""
    duration: str = ""

    def __post_init__(self):
        if not self.id:
            # derive a stable ID from URL
            self.id = re.sub(r"[^a-zA-Z0-9_-]", "", self.url)[-64:]


@dataclass
class Subject:
    name: str
    items: list[ContentItem] = field(default_factory=list)

    @property
    def videos(self):
        return [i for i in self.items if i.kind == "video"]

    @property
    def pdfs(self):
        return [i for i in self.items if i.kind == "pdf"]

    @property
    def notes(self):
        return [i for i in self.items if i.kind == "notes"]


# ── helpers ───────────────────────────────────────────────────────────────────

def extract_youtube_id(url: str) -> Optional[str]:
    m = re.search(r"(?:v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def clean_youtube_url(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


def is_pdf_url(url: str) -> bool:
    return bool(re.search(r"\.pdf(\?|$)", url, re.I))


def is_video_url(url: str) -> bool:
    return bool(re.search(r"\.(mp4|m3u8|mkv|webm)(\?|$)", url, re.I)) or \
           any(x in url for x in ["youtube", "youtu.be", "vimeo"])


def is_notes_url(url: str) -> bool:
    return bool(re.search(r"\.(doc|docx|ppt|pptx|txt|zip|rar)(\?|$)", url, re.I)) or \
           "drive.google.com" in url


def classify_url(url: str) -> str:
    if is_pdf_url(url):
        return "pdf"
    if is_video_url(url):
        return "video"
    if is_notes_url(url):
        return "notes"
    return "other"


def make_id(subject: str, title: str, url: str) -> str:
    raw = f"{subject}|{title}|{url}"
    return re.sub(r"[^a-zA-Z0-9]", "", raw)[:80]


# ── scraper ───────────────────────────────────────────────────────────────────

class SahukgsScraper:
    def __init__(self, batch_url: str = None):
        self.batch_url = batch_url or config.BATCH_URL
        self._api_responses: list[dict] = []

    async def _intercept(self, response: Response):
        url = response.url
        ct = response.headers.get("content-type", "")
        if "json" in ct or any(x in url for x in ["firestore", "firebase", "api"]):
            try:
                body = await response.text()
                if len(body) > 40:
                    self._api_responses.append({"url": url, "body": body})
            except Exception:
                pass

    async def scrape_batch(self, batch_url: Optional[str] = None) -> list[Subject]:
        target_url = batch_url or self.batch_url
        subjects: list[Subject] = []
        self._api_responses.clear()

        print(f"[scraper] Navigating to: {target_url}")
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            page = await context.new_page()
            page.on("response", self._intercept)

            try:
                await page.goto(target_url, wait_until="networkidle", timeout=60000)
                await asyncio.sleep(4)

                # Expand subjects if clickable
                for btn in await page.query_selector_all("button, [role='button'], .accordion, .subject-tab"):
                    try:
                        await btn.click(timeout=1000)
                        await asyncio.sleep(0.3)
                    except Exception:
                        pass

                # Parse DOM elements
                anchors = await page.query_selector_all("a[href]")
                items_by_subject: dict[str, list[ContentItem]] = {}

                for a in anchors:
                    href = await a.get_attribute("href")
                    title = (await a.inner_text() or "").strip()
                    if not href or not href.startswith("http"):
                        continue

                    kind = classify_url(href)
                    if kind != "other":
                        subject_name = "General"
                        item = ContentItem(
                            id=make_id(subject_name, title, href),
                            title=title or href.split("/")[-1],
                            kind=kind,
                            url=href,
                            subject=subject_name
                        )
                        items_by_subject.setdefault(subject_name, []).append(item)

                # Parse intercepted API payloads if any
                for entry in self._api_responses:
                    try:
                        data = json.loads(entry["body"])
                        # Recursive search for video/pdf links in JSON
                        self._extract_json_items(data, items_by_subject)
                    except Exception:
                        pass

                for name, items in items_by_subject.items():
                    # Dedup by URL
                    seen = set()
                    unique_items = []
                    for it in items:
                        if it.url not in seen:
                            seen.add(it.url)
                            unique_items.append(it)
                    subjects.append(Subject(name=name, items=unique_items))

            finally:
                await browser.close()

        return subjects

    def _extract_json_items(self, data, target_dict: dict, current_subject: str = "Batch Content"):
        if isinstance(data, dict):
            url = data.get("url") or data.get("videoUrl") or data.get("pdfUrl") or data.get("fileUrl") or data.get("link")
            title = data.get("title") or data.get("name") or data.get("topic") or "Untitled"
            subj = data.get("subject") or data.get("subjectName") or current_subject

            if url and isinstance(url, str) and url.startswith("http"):
                kind = classify_url(url)
                if kind != "other":
                    item = ContentItem(
                        id=make_id(subj, title, url),
                        title=title,
                        kind=kind,
                        url=url,
                        subject=subj
                    )
                    target_dict.setdefault(subj, []).append(item)

            for v in data.values():
                self._extract_json_items(v, target_dict, subj)
        elif isinstance(data, list):
            for elem in data:
                self._extract_json_items(elem, target_dict, current_subject)
