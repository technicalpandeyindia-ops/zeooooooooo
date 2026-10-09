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
    def __init__(self, batch_url: str = config.BATCH_URL):
        self.batch_url = batch_url
        self._api_responses: list[dict] = []

    async def _intercept(self, response: Response):
        url = response.url
        ct  = response.headers.get("content-type", "")
        if "json" in ct or any(x in url for x in ["firestore", "firebase", "api"]):
            try:
                body = await response.text()
                if len(body) > 40:
                    self._api_responses.append({"url": url, "body": body})
            except Exception:
                pass

    async def scrape(self) -> list[Subject]:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=config.HEADLESS)
            ctx     = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            )
            page: Page = await ctx.new_page()
            page.on("response", self._intercept)

            print(f"[scraper] → {self.batch_url}")
            await page.goto(self.batch_url,
                            timeout=config.PAGE_TIMEOUT,
                            wait_until="networkidle")
            await page.wait_for_timeout(3000)

            subjects = await self._extract_subjects(page)

            for subj in subjects:
                await self._drill_subject(page, subj)

            await browser.close()

        # fallback: parse raw network traffic
        if not any(s.items for s in subjects):
            subjects = self._parse_network(subjects)

        return subjects

    # ── page-level extraction ─────────────────────────────────────────────────

    async def _extract_subjects(self, page: Page) -> list[Subject]:
        subjects: list[Subject] = []

        # try clicking Classroom tab
        for selector in ["text=Classroom", "text=classroom",
                          "[class*='classroom']", "[class*='subject']"]:
            try:
                await page.click(selector, timeout=3000)
                await page.wait_for_timeout(1500)
                break
            except Exception:
                pass

        # collect subject cards
        candidates = await page.evaluate("""() => {
            const selectors = [
                '[class*="subject"]', '[class*="chapter"]',
                '[class*="card"]',    '.MuiListItem-root',
                '.v-list-item',       'li'
            ];
            const seen = new Set();
            const out  = [];
            for (const sel of selectors) {
                document.querySelectorAll(sel).forEach(el => {
                    const t = el.innerText.trim().split('\\n')[0];
                    if (t && t.length > 2 && t.length < 100 && !seen.has(t)) {
                        seen.add(t);
                        out.push(t);
                    }
                });
                if (out.length) break;
            }
            // last resort: headings
            if (!out.length) {
                document.querySelectorAll('h3,h4,h5,[class*="title"],[class*="name"]')
                    .forEach(el => {
                        const t = el.innerText.trim();
                        if (t && t.length > 2 && t.length < 80 && !seen.has(t)) {
                            seen.add(t); out.push(t);
                        }
                    });
            }
            return out;
        }""")

        for name in candidates:
            subjects.append(Subject(name=name))

        if not subjects:
            subjects = [Subject(name="Batch 1159")]

        return subjects

    async def _drill_subject(self, page: Page, subject: Subject):
        try:
            await page.click(f"text='{subject.name}'", timeout=4000)
            await page.wait_for_timeout(2000)
            subject.items = await self._extract_items(page, subject.name)
            await page.go_back(timeout=8000, wait_until="networkidle")
            await page.wait_for_timeout(1000)
        except Exception as e:
            print(f"[scraper] skip '{subject.name}': {e}")

    async def _extract_items(self, page: Page, subject_name: str) -> list[ContentItem]:
        items: list[ContentItem] = []
        seen: set[str] = set()

        # ── iframes (YouTube embeds) ───────────────────────────────────────────
        iframes = await page.evaluate(
            "() => Array.from(document.querySelectorAll('iframe')).map(f => f.src)"
        )
        for src in iframes:
            vid = extract_youtube_id(src)
            if vid and vid not in seen:
                seen.add(vid)
                url = clean_youtube_url(vid)
                items.append(ContentItem(
                    id=make_id(subject_name, f"yt_{vid}", url),
                    title=f"Lecture {len(items)+1}",
                    kind="video",
                    url=url,
                    subject=subject_name,
                ))

        # ── all links / data-attrs ─────────────────────────────────────────────
        links = await page.evaluate("""() => {
            const out = [];
            const attrs = ['href','data-url','data-src','data-video','data-file','data-link'];
            document.querySelectorAll('*').forEach(el => {
                for (const a of attrs) {
                    const v = el.getAttribute(a);
                    if (v && v.startsWith('http')) {
                        out.push({
                            text: el.innerText?.trim().split('\\n')[0] || '',
                            url: v
                        });
                        break;
                    }
                }
            });
            return out;
        }""")

        for item in links:
            url: str   = item.get("url", "").strip()
            title: str = item.get("text", "").strip() or f"Item {len(items)+1}"
            if not url or url in seen:
                continue
            kind = classify_url(url)
            if kind == "other":
                continue
            seen.add(url)
            vid = extract_youtube_id(url) if kind == "video" else None
            final_url = clean_youtube_url(vid) if vid else url
            items.append(ContentItem(
                id=make_id(subject_name, title, final_url),
                title=title[:120],
                kind=kind,
                url=final_url,
                subject=subject_name,
            ))

        return items

    # ── network fallback ──────────────────────────────────────────────────────

    def _parse_network(self, subjects: list[Subject]) -> list[Subject]:
        all_items: list[ContentItem] = []

        for resp in self._api_responses:
            try:
                data = json.loads(resp["body"])
                self._walk(data, all_items)
            except json.JSONDecodeError:
                for frag in re.findall(r'\{[^{}]{20,}\}', resp["body"]):
                    try:
                        self._walk(json.loads(frag), all_items)
                    except Exception:
                        pass

        if all_items:
            subjects[0].items = all_items

        return subjects

    def _walk(self, obj, out: list[ContentItem], depth: int = 0):
        if depth > 10:
            return
        if isinstance(obj, list):
            for x in obj:
                self._walk(x, out, depth+1)
            return
        if not isinstance(obj, dict):
            return

        url_keys   = {"url","videoUrl","video_url","link","youtubeUrl",
                      "embedUrl","src","source","streamUrl","contentUrl",
                      "pdfUrl","pdf_url","fileUrl","file_url","notesUrl"}
        title_keys = {"title","name","subject","topic","heading","label"}

        url   = ""
        title = ""
        for k, v in obj.items():
            if isinstance(v, str):
                if k in url_keys or any(x in v for x in
                        ["youtube","youtu.be",".mp4",".m3u8",".pdf",
                         "drive.google",".docx",".pptx"]):
                    url = v
                if k in title_keys:
                    title = v

        if url:
            kind = classify_url(url)
            if kind != "other":
                vid = extract_youtube_id(url) if kind == "video" else None
                final = clean_youtube_url(vid) if vid else url
                out.append(ContentItem(
                    id=make_id("", title, final),
                    title=title or f"Item {len(out)+1}",
                    kind=kind,
                    url=final,
                ))

        for v in obj.values():
            if isinstance(v, (dict, list)):
                self._walk(v, out, depth+1)


# ── standalone run ────────────────────────────────────────────────────────────

async def main():
    s = SahukgsScraper()
    subjects = await s.scrape()
    for subj in subjects:
        print(f"\n📚 {subj.name}")
        for item in subj.items[:5]:
            print(f"  [{item.kind.upper()}] {item.title[:60]}")
            print(f"         → {item.url[:80]}")

if __name__ == "__main__":
    asyncio.run(main())
