"""
scraper.py — extracts subjects + video links + PDFs + notes strictly from the "Classroom" tab of sahukgs.com.
Uses Playwright headless Chromium + network interception with subject card navigation and lecture pairing.
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
    topic: str = ""             # Normalized topic/lecture name for pairing
    lecture_num: float = 9999.0 # Numeric order for natural sorting
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


# ── helpers ───────────────────────────────────────────────────────────────────

def extract_lecture_number(title: str) -> float:
    """Extracts numeric lecture order, e.g. 'Lecture - 03' -> 3.0, 'Class 12.1' -> 12.1"""
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
    t = re.sub(r"\b(pdf|notes|handwritten|class notes|dpp|solution|video|lecture notes)\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"[\s\-_|:]+", " ", t).strip()
    return t or title


def is_pdf_url(url: str) -> bool:
    return bool(re.search(r"\.pdf(\?|$)", url, re.I))


def is_video_url(url: str) -> bool:
    return bool(re.search(r"\.(mp4|m3u8|mkv|webm)(\?|$)", url, re.I)) or \
           any(x in url.lower() for x in ["youtube", "youtu.be", "vimeo", "stream", "video", "jwplayer"])


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
        if "json" in ct or any(x in url for x in ["firestore", "firebase", "api", "batch", "classroom", "subject"]):
            try:
                body = await response.text()
                if len(body) > 40:
                    self._api_responses.append({"url": url, "body": body})
            except Exception:
                pass

    async def scrape_batch(self, batch_url: Optional[str] = None) -> list[Subject]:
        target_url = batch_url or self.batch_url
        subjects_dict: dict[str, list[ContentItem]] = {}
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

                # ── Click Specifically on the "Classroom" Tab ────────────────
                print("[scraper] Locating and clicking the Classroom tab...")
                classroom_clicked = False
                for sel in [
                    "//div[contains(text(), 'Classroom')]",
                    "//button[contains(text(), 'Classroom')]",
                    "//span[contains(text(), 'Classroom')]",
                    "//a[contains(text(), 'Classroom')]",
                    ".classroom-tab",
                    "[data-tab='classroom']"
                ]:
                    try:
                        el = page.locator(sel).first
                        if await el.count() > 0 and await el.is_visible():
                            await el.click(timeout=3000)
                            classroom_clicked = True
                            print(f"[scraper] Clicked Classroom via selector: {sel}")
                            await asyncio.sleep(3)
                            break
                    except Exception:
                        continue

                # ── Iterate through Subject Cards inside Classroom ────────────
                subject_cards = await page.query_selector_all(".card, .subject-card, [class*='subject'], [class*='Subject'], div.cursor-pointer, [class*='grid'] > div")
                print(f"[scraper] Found {len(subject_cards)} possible subject card elements.")

                # Extract links directly visible in DOM under Classroom
                anchors = await page.query_selector_all("a[href]")
                for a in anchors:
                    href = await a.get_attribute("href")
                    title = (await a.inner_text() or "").strip()
                    if not href or not href.startswith("http"):
                        continue

                    kind = classify_url(href)
                    if kind != "other":
                        subject_name = "Classroom"
                        item = ContentItem(
                            id=make_id(subject_name, title, href),
                            title=title or href.split("/")[-1],
                            kind=kind,
                            url=href,
                            subject=subject_name,
                            topic=normalize_topic_name(title or href.split("/")[-1]),
                            lecture_num=extract_lecture_number(title)
                        )
                        subjects_dict.setdefault(subject_name, []).append(item)

                # Click each subject card to trigger sub-lesson network calls
                for idx, card in enumerate(subject_cards[:25]):
                    try:
                        card_text = (await card.inner_text() or "").strip().split("\n")[0]
                        if not card_text or any(k in card_text.lower() for k in ["today", "updates", "timetable", "logout"]):
                            continue

                        await card.click(timeout=2000)
                        await asyncio.sleep(1.5)

                        # Capture links inside opened card
                        sub_anchors = await page.query_selector_all("a[href]")
                        for sa in sub_anchors:
                            shref = await sa.get_attribute("href")
                            stitle = (await sa.inner_text() or "").strip()
                            if shref and shref.startswith("http"):
                                skind = classify_url(shref)
                                if skind != "other":
                                    s_item = ContentItem(
                                        id=make_id(card_text, stitle, shref),
                                        title=stitle or shref.split("/")[-1],
                                        kind=skind,
                                        url=shref,
                                        subject=card_text,
                                        topic=normalize_topic_name(stitle or shref.split("/")[-1]),
                                        lecture_num=extract_lecture_number(stitle)
                                    )
                                    subjects_dict.setdefault(card_text, []).append(s_item)

                    except Exception:
                        pass

                # ── Parse Intercepted JSON API Payloads for Classroom ─────────
                for entry in self._api_responses:
                    try:
                        data = json.loads(entry["body"])
                        self._extract_json_items(data, subjects_dict)
                    except Exception:
                        pass

            finally:
                await browser.close()

        # Final organization & sequential pairing per Subject
        final_subjects: list[Subject] = []
        for name, items in subjects_dict.items():
            seen = set()
            unique_items = []
            for it in items:
                if it.url not in seen:
                    seen.add(it.url)
                    unique_items.append(it)

            sorted_items = self._organize_sequential(unique_items)
            if sorted_items:
                final_subjects.append(Subject(name=name, items=sorted_items))

        return final_subjects

    def _organize_sequential(self, items: list[ContentItem]) -> list[ContentItem]:
        """
        Organizes content into sequential Lecture-wise pairs:
        For each lecture: Video first -> then its corresponding PDF/Notes right after.
        """
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

            result.extend(videos)
            result.extend(pdfs)
            result.extend(others)

        return result

    def _extract_json_items(self, data, target_dict: dict, current_subject: str = "Classroom"):
        if isinstance(data, dict):
            # Ignore tabs like today, updates, timetable in JSON
            tab_type = str(data.get("tab") or data.get("type") or "").lower()
            if tab_type in ["today", "updates", "timetable", "notice"]:
                return

            subj = data.get("subject") or data.get("subjectName") or data.get("folderName") or data.get("courseName") or current_subject
            title = data.get("title") or data.get("name") or data.get("topic") or data.get("lectureName") or "Untitled"
            lec_num = extract_lecture_number(title)
            topic = normalize_topic_name(title)

            video_url = data.get("videoUrl") or data.get("video_url") or data.get("streamUrl") or data.get("playbackUrl")
            pdf_url = data.get("pdfUrl") or data.get("pdf_url") or data.get("documentUrl") or data.get("notesUrl") or data.get("fileUrl")

            if video_url and isinstance(video_url, str) and video_url.startswith("http"):
                target_dict.setdefault(subj, []).append(
                    ContentItem(
                        id=make_id(subj, title, video_url),
                        title=title,
                        kind="video",
                        url=video_url,
                        subject=subj,
                        topic=topic,
                        lecture_num=lec_num
                    )
                )

            if pdf_url and isinstance(pdf_url, str) and pdf_url.startswith("http"):
                target_dict.setdefault(subj, []).append(
                    ContentItem(
                        id=make_id(subj, title, pdf_url),
                        title=f"{title} (Notes)",
                        kind="pdf",
                        url=pdf_url,
                        subject=subj,
                        topic=topic,
                        lecture_num=lec_num
                    )
                )

            url = data.get("url") or data.get("link")
            if url and isinstance(url, str) and url.startswith("http") and url != video_url and url != pdf_url:
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
