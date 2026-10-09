"""
scraper.py — High-Speed Direct API Extractor for sahukgs.com Classroom.
Defensively handles null/empty fields and extracts all videos and matching lecture PDFs across all subjects.
"""

import asyncio
import html
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

import config

logger = logging.getLogger(__name__)


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


# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_target_id(target_url: str) -> tuple[str, str]:
    """
    Determines if input URL is a batch ID or a single lesson ID.
    Returns: ('batch', '1159') or ('lesson', '14392')
    """
    m_lesson = re.search(r"lesson_id=([0-9]+)", target_url)
    if m_lesson:
        return ("lesson", m_lesson.group(1))

    m_batch = re.search(r"batch[=/]([0-9]+)", target_url)
    if m_batch:
        return ("batch", m_batch.group(1))

    parts = target_url.rstrip("/").split("/")
    if parts[-1].isdigit():
        return ("batch", parts[-1])
    return ("batch", "1159")


def extract_lecture_number(title: str) -> float:
    """Extracts numeric lecture number (e.g. 'Lecture - 03' -> 3.0, 'Class 12.1' -> 12.1)."""
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


def make_id(subject: str, title: str, url: str) -> str:
    raw = f"{subject}|{title}|{url}"
    return re.sub(r"[^a-zA-Z0-9]", "", raw)[:80]


# ── High-Speed API Scraper ────────────────────────────────────────────────────

class SahukgsScraper:
    def __init__(self, batch_url: str = None):
        self.batch_url = batch_url or config.BATCH_URL
        self.base_url = "https://www.sahukgs.com"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": "https://www.sahukgs.com/"
        }

    async def scrape_batch(self, batch_url: Optional[str] = None) -> list[Subject]:
        target_url = batch_url or self.batch_url
        target_type, target_id = extract_target_id(target_url)
        logger.info(f"[scraper] Initiating extraction: Type={target_type}, ID={target_id}")

        subjects_list: list[Subject] = []
        timeout = aiohttp.ClientTimeout(total=60)

        async with aiohttp.ClientSession(headers=self.headers, timeout=timeout) as session:
            topics_to_fetch: list[dict] = []

            if target_type == "lesson":
                topics_to_fetch.append({"id": int(target_id), "name": f"Lesson {target_id}"})
            else:
                classroom_api = f"{self.base_url}/api/classroom/{target_id}"
                logger.info(f"[scraper] Fetching all classroom folders from: {classroom_api}")
                try:
                    async with session.get(classroom_api) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if isinstance(data, dict):
                                topics_to_fetch = data.get("classroom") or []
                            elif isinstance(data, list):
                                topics_to_fetch = data
                        else:
                            logger.error(f"[scraper] HTTP {resp.status} fetching classroom")
                except Exception as e:
                    logger.error(f"[scraper] Error calling classroom API: {e}")
                    return []

            if not topics_to_fetch:
                logger.warning("[scraper] No topics returned from classroom API.")
                return []

            logger.info(f"[scraper] Processing {len(topics_to_fetch)} subject folders...")

            for topic_idx, topic in enumerate(topics_to_fetch, 1):
                if not isinstance(topic, dict):
                    continue

                lesson_id = topic.get("id")
                fallback_name = topic.get("name") or f"Subject {topic_idx}"
                if not lesson_id:
                    continue

                lesson_api = f"{self.base_url}/api/lesson/{lesson_id}"
                logger.info(f"[scraper] [{topic_idx}/{len(topics_to_fetch)}] Loading folder: ID {lesson_id}")

                try:
                    async with session.get(lesson_api) as l_resp:
                        if l_resp.status != 200:
                            logger.warning(f"[scraper] Failed lesson {lesson_id}: HTTP {l_resp.status}")
                            continue
                        lesson_data = await l_resp.json()
                except Exception as e:
                    logger.warning(f"[scraper] Exception fetching lesson {lesson_id}: {e}")
                    continue

                if not isinstance(lesson_data, dict):
                    continue

                subject_name = lesson_data.get("name") or fallback_name
                videos_meta = lesson_data.get("videos") or []
                notes_meta = lesson_data.get("notes") or []
                subject_items: list[ContentItem] = []

                logger.info(f"[scraper] '{subject_name}' contains {len(videos_meta)} videos and {len(notes_meta)} notes.")

                # Worker to resolve video streams & attached PDFs
                async def resolve_video(v_info):
                    if not isinstance(v_info, dict):
                        return []

                    v_id = v_info.get("id")
                    v_name = v_info.get("name") or "Untitled Lecture"
                    lec_num = extract_lecture_number(v_name)
                    topic_norm = normalize_topic_name(v_name)
                    v_thumb = v_info.get("thumb") or ""
                    items = []

                    # Direct video_url check
                    direct_video = v_info.get("video_url")
                    if direct_video:
                        items.append(
                            ContentItem(
                                id=f"vid_{v_id}",
                                title=v_name,
                                kind="video",
                                url=direct_video,
                                subject=subject_name,
                                topic=topic_norm,
                                lecture_num=lec_num,
                                thumbnail=v_thumb
                            )
                        )
                    elif v_id:
                        try:
                            async with session.get(f"{self.base_url}/api/video/{v_id}") as v_resp:
                                if v_resp.status == 200:
                                    v_data = await v_resp.json()
                                    if isinstance(v_data, dict):
                                        final_url = v_data.get("hd_video_url") or v_data.get("video_url")
                                        if final_url:
                                            items.append(
                                                ContentItem(
                                                    id=f"vid_{v_id}",
                                                    title=v_name,
                                                    kind="video",
                                                    url=final_url,
                                                    subject=subject_name,
                                                    topic=topic_norm,
                                                    lecture_num=lec_num,
                                                    thumbnail=v_thumb
                                                )
                                            )
                                        # Also check attached PDFs in /api/video response
                                        for p in (v_data.get("pdfs") or []):
                                            if isinstance(p, dict) and p.get("url"):
                                                items.append(
                                                    ContentItem(
                                                        id=f"vpdf_{v_id}_{make_id('', '', p.get('url'))[:10]}",
                                                        title=f"{v_name} (Notes)",
                                                        kind="pdf",
                                                        url=p.get("url"),
                                                        subject=subject_name,
                                                        topic=topic_norm,
                                                        lecture_num=lec_num
                                                    )
                                                )
                        except Exception:
                            pass

                    # Extract matching PDFs attached directly to this video metadata
                    for p in (v_info.get("pdfs") or []):
                        if isinstance(p, dict) and p.get("url"):
                            items.append(
                                ContentItem(
                                    id=f"vpdf_{v_id}_{make_id('', '', p.get('url'))[:10]}",
                                    title=f"{v_name} (Notes)",
                                    kind="pdf",
                                    url=p.get("url"),
                                    subject=subject_name,
                                    topic=topic_norm,
                                    lecture_num=lec_num
                                )
                            )

                    return items

                # Worker to resolve standalone notes
                async def resolve_note(n_info):
                    if not isinstance(n_info, dict):
                        return []

                    n_id = n_info.get("id")
                    n_name = n_info.get("name") or "Untitled Note"
                    lec_num = extract_lecture_number(n_name)
                    topic_norm = normalize_topic_name(n_name)
                    items = []

                    direct_pdf = n_info.get("url") or ((n_info.get("pdfs") or [{}])[0].get("url") if n_info.get("pdfs") else None)
                    if direct_pdf:
                        items.append(
                            ContentItem(
                                id=f"note_{n_id}",
                                title=n_name,
                                kind="pdf",
                                url=direct_pdf,
                                subject=subject_name,
                                topic=topic_norm,
                                lecture_num=lec_num
                            )
                        )
                    elif n_id:
                        try:
                            async with session.get(f"{self.base_url}/api/video/{n_id}") as n_resp:
                                if n_resp.status == 200:
                                    n_data = await n_resp.json()
                                    if isinstance(n_data, dict):
                                        pdf_url = n_data.get("video_url") or ((n_data.get("pdfs") or [{}])[0].get("url") if n_data.get("pdfs") else None)
                                        if pdf_url:
                                            items.append(
                                                ContentItem(
                                                    id=f"note_{n_id}",
                                                    title=n_name,
                                                    kind="pdf",
                                                    url=pdf_url,
                                                    subject=subject_name,
                                                    topic=topic_norm,
                                                    lecture_num=lec_num
                                                )
                                            )
                        except Exception:
                            pass

                    return items

                # Execute resolution tasks concurrently
                video_tasks = [resolve_video(v) for v in (videos_meta or [])]
                note_tasks = [resolve_note(n) for n in (notes_meta or [])]

                results = await asyncio.gather(*video_tasks, *note_tasks)
                for res in results:
                    if res:
                        subject_items.extend(res)

                # Organize sequentially
                organized_items = self._organize_sequential(subject_items)
                if organized_items:
                    subjects_list.append(Subject(name=subject_name, items=organized_items))

        total_extracted = sum(len(s.items or []) for s in subjects_list)
        logger.info(f"[scraper] ✅ Extracted {len(subjects_list)} folders with {total_extracted} total items.")
        return subjects_list

    def _organize_sequential(self, items: list[ContentItem]) -> list[ContentItem]:
        """
        Pairs each lecture's Video directly with its corresponding PDF notes,
        sorted in ascending numerical order (Lecture 01, Lecture 02, etc.).
        """
        if not items:
            return []

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
