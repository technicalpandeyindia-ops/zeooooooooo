"""
scraper.py — Deep Classroom Extractor for sahukgs.com.
Navigates every subject card in the Classroom tab, explores all chapters/sub-tabs,
scrolls lazy-loaded lists, intercepts all backend API payloads, and pairs videos with notes.
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Optional

from playwright.async_api import async_playwright, Page, Response

import config


# ── Data Models ───────────────────────────────────────────────────────────────

@dataclass
class ContentItem:
    """A single piece of content — video, PDF, or note."""
    id: str
    title: str
    kind: str                   # "video" | "pdf" | "notes"
    url: str                    # playable / downloadable URL
    subject: str = ""
    topic: str = ""
    lecture_num: float = 9999.0
    thumbnail: str = ""
    duration: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = re.sub(r"[^a-zA-Z0-9_-]", "", self.url)[-64:]
        if not self.topic:
            self.topic = self.title
        if self.lecture_num == 9999.0:
            self.lecture_num = extract_lecture_number(self.title)


@dataclass
class Subject:
    name: str
    items: list[ContentItem] = field(default_factory=list)


# ── Helper Functions ──────────────────────────────────────────────────────────

def extract_lecture_number(title: str) -> float:
    """Extracts numerical lecture order (e.g. 'Lecture - 03' -> 3.0, 'Class 12.1' -> 12.1)."""
    m = re.search(r"(?:lecture|class|part|ep|session|lec|no|ch|chapter)[\s._#-]*([0-9]+(?:\.[0-9]+)?)", title, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    m2 = re.search(r"\b([0-9]{1,4})\b", title)
    if m2:
        try:
            return float(m2.group(1))
        except ValueError:
            pass
    return 9999.0


def normalize_topic_name(title: str) -> str:
    """Normalizes title to group matching Video and PDF for the same lecture."""
    t = re.sub(r"\.(pdf|mp4|mkv|zip|docx?)", "", title, flags=re.IGNORECASE)
    t = re.sub(r"\b(pdf|notes|handwritten|class notes|dpp|solution|video|lecture notes|part\s*\d+)\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"[\s\-_|:]+", " ", t).strip()
    return t or title


def is_pdf_url(url: str) -> bool:
    return bool(re.search(r"\.pdf(\?|$)", url, re.I)) or "drive.google.com" in url or "pdf" in url.lower()


def is_video_url(url: str) -> bool:
    return bool(re.search(r"\.(mp4|m3u8|mkv|webm|ts)(\?|$)", url, re.I)) or \
           any(x in url.lower() for x in ["youtube", "youtu.be", "vimeo", "stream", "video", "jwplayer", "cloudfront", "playlist"])


def is_notes_url(url: str) -> bool:
    return bool(re.search(r"\.(doc|docx|ppt|pptx|txt|zip|rar)(\?|$)", url, re.I))


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


# ── Deep Scraper Engine ───────────────────────────────────────────────────────

class SahukgsScraper:
    def __init__(self, batch_url: str = None):
        self.batch_url = batch_url or config.BATCH_URL
        self._api_responses: list[dict] = []

    async def _intercept(self, response: Response):
        url = response.url
        ct = response.headers.get("content-type", "")
        # Intercept any potential JSON API or course metadata responses
        if "json" in ct or any(x in url for x in ["firestore", "firebase", "api", "batch", "classroom", "subject", "courses", "lessons"]):
            try:
                body = await response.text()
                if len(body) > 30:
                    self._api_responses.append({"url": url, "body": body})
            except Exception:
                pass

    async def scrape_batch(self, batch_url: Optional[str] = None) -> list[Subject]:
        target_url = batch_url or self.batch_url
        subjects_dict: dict[str, list[ContentItem]] = {}
        self._api_responses.clear()

        print(f"[scraper] Launching Chromium to scrape: {target_url}")
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-accelerated-2d-canvas",
                    "--no-first-run",
                    "--no-zygote",
                    "--disable-gpu"
                ]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900}
            )
            page = await context.new_page()
            page.on("response", self._intercept)

            try:
                await page.goto(target_url, wait_until="networkidle", timeout=60000)
                await asyncio.sleep(4)

                # 1. Switch to Classroom Tab
                print("[scraper] Activating Classroom tab...")
                await self._click_classroom_tab(page)
                await asyncio.sleep(3)

                # 2. Extract subjects list
                subject_names = await self._get_subject_names(page)
                print(f"[scraper] Identified {len(subject_names)} subjects: {subject_names}")

                # 3. Explore each subject by clicking into it
                for subj_idx, sname in enumerate(subject_names):
                    try:
                        print(f"[scraper] [{subj_idx+1}/{len(subject_names)}] Exploring subject: {sname}")
                        
                        # Re-ensure on Classroom tab
                        await self._click_classroom_tab(page)
                        await asyncio.sleep(1.5)

                        # Find matching subject card and click
                        card = page.locator(f"xpath=//*[contains(text(), '{sname}')]").first
                        if await card.count() > 0:
                            await card.click(timeout=4000)
                            await asyncio.sleep(2.5)

                            # Expand sub-folders / accordions / chapters if any
                            await self._expand_and_scroll(page, sname, subjects_dict)

                            # Check for sub-tabs like 'Videos' and 'Notes' inside subject
                            for sub_tab in ["Videos", "Notes", "Study Material", "All"]:
                                try:
                                    tab_el = page.locator(f"xpath=//*[text()='{sub_tab}' or contains(text(), '{sub_tab}')]").first
                                    if await tab_el.count() > 0 and await tab_el.is_visible():
                                        await tab_el.click(timeout=1500)
                                        await asyncio.sleep(1.5)
                                        await self._expand_and_scroll(page, sname, subjects_dict)
                                except Exception:
                                    pass

                    except Exception as e:
                        print(f"[scraper] Note exploring {sname}: {e}")
                        continue

                # 4. Parse all intercepted JSON API responses for deep extraction
                print(f"[scraper] Parsing {len(self._api_responses)} intercepted network API responses...")
                for entry in self._api_responses:
                    try:
                        data = json.loads(entry["body"])
                        self._extract_json_items(data, subjects_dict)
                    except Exception:
                        pass

            finally:
                await browser.close()

        # 5. Deduplicate and sequentially pair (Video -> Matching PDF) for every subject
        final_subjects: list[Subject] = []
        for name, items in subjects_dict.items():
            if not items:
                continue
            seen = set()
            unique_items = []
            for it in items:
                if it.url not in seen:
                    seen.add(it.url)
                    unique_items.append(it)

            sorted_items = self._organize_sequential(unique_items)
            if sorted_items:
                final_subjects.append(Subject(name=name, items=sorted_items))

        total_extracted = sum(len(s.items) for s in final_subjects)
        print(f"[scraper] Extraction finished: Total {len(final_subjects)} subjects with {total_extracted} total items.")
        return final_subjects

    async def _click_classroom_tab(self, page: Page):
        for sel in [
            "//div[normalize-space()='Classroom']",
            "//button[contains(., 'Classroom')]",
            "//span[contains(., 'Classroom')]",
            "//a[contains(., 'Classroom')]",
            "[data-tab='classroom']",
            ".classroom-tab"
        ]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click(timeout=2000)
                    return True
            except Exception:
                continue
        return False

    async def _get_subject_names(self, page: Page) -> list[str]:
        # Collect text of subject cards
        cards = await page.query_selector_all(".card, .subject-card, [class*='subject'], [class*='grid'] > div")
        names = []
        for c in cards:
            text = (await c.inner_text() or "").strip().split("\n")[0]
            if text and len(text) > 2 and not any(k in text.lower() for k in ["today", "updates", "timetable", "logout", "notification"]):
                if text not in names:
                    names.append(text)
        return names

    async def _expand_and_scroll(self, page: Page, subject_name: str, target_dict: dict):
        # Click all accordion headers or chapter expansion buttons
        for btn in await page.query_selector_all("button, [role='button'], .accordion-header, [class*='chapter'], [class*='folder']"):
            try:
                await btn.click(timeout=800)
                await asyncio.sleep(0.2)
            except Exception:
                pass

        # Scroll multiple times to trigger lazy loading of lectures
        for _ in range(4):
            await page.evaluate("window.scrollBy(0, 1000);")
            await asyncio.sleep(0.5)

        # Harvest all clickable and download links currently in DOM
        anchors = await page.query_selector_all("a[href], iframe[src], video source[src]")
        for a in anchors:
            href = await a.get_attribute("href") or await a.get_attribute("src")
            title = (await a.inner_text() or "").strip()
            if not href or not href.startswith("http"):
                continue

            kind = classify_url(href)
            if kind != "other":
                item_title = title or href.split("/")[-1].split("?")[0]
                target_dict.setdefault(subject_name, []).append(
                    ContentItem(
                        id=make_id(subject_name, item_title, href),
                        title=item_title,
                        kind=kind,
                        url=href,
                        subject=subject_name,
                        topic=normalize_topic_name(item_title),
                        lecture_num=extract_lecture_number(item_title)
                    )
                )

    def _organize_sequential(self, items: list[ContentItem]) -> list[ContentItem]:
        """Pairs every lecture's Video directly with its corresponding PDF Notes."""
        groups: dict[str, list[ContentItem]] = {}
        for item in items:
            key = f"{item.lecture_num:06.2f}_{item.topic.lower()}"
            groups.setdefault(key, []).append(item)

        ordered_keys = sorted(groups.keys())
        result: list[ContentItem] = []

        for k in ordered_keys:
            group_items = groups[k]
            videos = [i for i in group_items if i.kind == "video"]
            pdfs = [i for i in group_items if i.kind in ("pdf", "notes")]
            others = [i for i in group_items if i not in videos and i not in pdfs]

            # Sequence: Video -> Matching PDF -> Others
            result.extend(videos)
            result.extend(pdfs)
            result.extend(others)

        return result

    def _extract_json_items(self, data, target_dict: dict, current_subject: str = "Classroom"):
        if isinstance(data, dict):
            tab_type = str(data.get("tab") or data.get("type") or "").lower()
            if tab_type in ["today", "updates", "timetable", "notice"]:
                return

            subj = data.get("subject") or data.get("subjectName") or data.get("folderName") or data.get("courseName") or data.get("category") or current_subject
            title = data.get("title") or data.get("name") or data.get("topic") or data.get("lectureName") or data.get("chapterName") or "Untitled"
            lec_num = extract_lecture_number(title)
            topic = normalize_topic_name(title)

            # Search for video URLs
            for v_key in ["videoUrl", "video_url", "streamUrl", "playbackUrl", "video", "hlsUrl", "m3u8Url"]:
                v_url = data.get(v_key)
                if v_url and isinstance(v_url, str) and v_url.startswith("http"):
                    target_dict.setdefault(subj, []).append(
                        ContentItem(
                            id=make_id(subj, title, v_url),
                            title=title,
                            kind="video",
                            url=v_url,
                            subject=subj,
                            topic=topic,
                            lecture_num=lec_num
                        )
                    )
                    break

            # Search for PDF / Notes URLs
            for p_key in ["pdfUrl", "pdf_url", "documentUrl", "notesUrl", "fileUrl", "attachment", "docUrl"]:
                p_url = data.get(p_key)
                if p_url and isinstance(p_url, str) and p_url.startswith("http"):
                    target_dict.setdefault(subj, []).append(
                        ContentItem(
                            id=make_id(subj, title, p_url),
                            title=f"{title} (Notes)",
                            kind="pdf",
                            url=p_url,
                            subject=subj,
                            topic=topic,
                            lecture_num=lec_num
                        )
                    )
                    break

            # Generic URL search
            url = data.get("url") or data.get("link") or data.get("file")
            if url and isinstance(url, str) and url.startswith("http"):
                kind = classify_url(url)
                if kind != "other":
                    target_dict.setdefault(subj, []).append(
                        ContentItem(
                            id=make_id(subj, title, url),
                            title=title,
                            kind=kind,
                            url=url,
                            subject=subj,
                            topic=topic,
                            lecture_num=lec_num
                        )
                    )

            for v in data.values():
                self._extract_json_items(v, target_dict, subj)

        elif isinstance(data, list):
            for elem in data:
                self._extract_json_items(elem, target_dict, current_subject)
