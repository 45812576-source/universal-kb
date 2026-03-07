"""External intelligence collection engine: RSS / Crawler / Manual."""
from __future__ import annotations

import datetime
import hashlib
import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.intel import IntelEntry, IntelEntryStatus, IntelSource, IntelSourceType

logger = logging.getLogger(__name__)

_PROCESS_PROMPT = """你是行业情报分析师。对以下文章进行分析，提取关键信息。
只返回JSON，不要任何其他内容。

文章标题：{title}
文章内容（前2000字）：{content}

返回格式：
{{
  "industry": "所属行业（如：电商、金融、广告等，若不确定返回null）",
  "platform": "所属平台（如：淘宝、抖音、微信等，若不确定返回null）",
  "tags": ["标签1", "标签2", "标签3"],
  "summary": "100字以内的摘要"
}}"""


class IntelCollector:

    def _url_hash(self, url: str) -> str:
        return hashlib.md5(url.encode()).hexdigest()

    def _is_duplicate(self, db: Session, url: Optional[str], title: str) -> bool:
        if url:
            exists = db.query(IntelEntry).filter(IntelEntry.url == url).first()
            if exists:
                return True
        # Title-based dedup (exact match)
        exists = db.query(IntelEntry).filter(IntelEntry.title == title).first()
        return exists is not None

    async def collect_rss(self, db: Session, source: IntelSource) -> int:
        """Collect entries from an RSS feed."""
        try:
            import feedparser
        except ImportError:
            logger.error("feedparser not installed. Run: pip install feedparser")
            return 0

        config = source.config or {}
        url = config.get("url", "")
        if not url:
            logger.warning(f"RSS source {source.name} has no URL")
            return 0

        feed = feedparser.parse(url)
        count = 0
        for entry in feed.entries[:20]:  # Limit to 20 latest
            title = entry.get("title", "").strip()
            link = entry.get("link", "")
            content = entry.get("summary", "") or entry.get("content", [{}])[0].get("value", "")

            if not title:
                continue
            if self._is_duplicate(db, link, title):
                continue

            intel = IntelEntry(
                source_id=source.id,
                title=title,
                content=content[:5000] if content else None,
                url=link,
                status=IntelEntryStatus.PENDING,
                auto_collected=True,
            )
            db.add(intel)
            count += 1

        db.commit()
        logger.info(f"RSS source '{source.name}' collected {count} new entries")
        return count

    async def collect_crawler(self, db: Session, source: IntelSource) -> int:
        """Crawl a webpage and extract articles."""
        try:
            import httpx
            from bs4 import BeautifulSoup
        except ImportError:
            logger.error("httpx or beautifulsoup4 not installed")
            return 0

        config = source.config or {}
        url = config.get("url", "")
        article_selector = config.get("article_selector", "article")
        title_selector = config.get("title_selector", "h1, h2")
        content_selector = config.get("content_selector", "p")

        if not url:
            return 0

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
            except Exception as e:
                logger.error(f"Crawler failed for {url}: {e}")
                return 0

        soup = BeautifulSoup(resp.text, "html.parser")
        articles = soup.select(article_selector)
        count = 0

        for article in articles[:10]:
            title_el = article.select_one(title_selector)
            title = title_el.get_text(strip=True) if title_el else ""
            if not title:
                continue

            content_els = article.select(content_selector)
            content = " ".join(el.get_text(strip=True) for el in content_els)

            link_el = article.find("a", href=True)
            link = link_el["href"] if link_el else url

            if self._is_duplicate(db, link, title):
                continue

            intel = IntelEntry(
                source_id=source.id,
                title=title,
                content=content[:5000] if content else None,
                url=link,
                status=IntelEntryStatus.PENDING,
                auto_collected=True,
            )
            db.add(intel)
            count += 1

        db.commit()
        return count

    async def process_entry(self, db: Session, entry: IntelEntry, model_config: dict) -> None:
        """Use LLM to clean and tag an intel entry."""
        from app.services.llm_gateway import llm_gateway
        import json

        if not entry.content and not entry.title:
            return

        content_preview = (entry.content or "")[:2000]
        prompt = _PROCESS_PROMPT.format(
            title=entry.title,
            content=content_preview,
        )

        try:
            result = await llm_gateway.chat(
                model_config=model_config,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=500,
            )
            import re
            cleaned = result.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```[a-z]*\n?", "", cleaned)
                cleaned = re.sub(r"\n?```$", "", cleaned)
            data = json.loads(cleaned)

            entry.industry = data.get("industry")
            entry.platform = data.get("platform")
            entry.tags = data.get("tags", [])
            # Prepend summary to content if available
            summary = data.get("summary", "")
            if summary and entry.content:
                entry.content = f"[摘要] {summary}\n\n{entry.content}"
            db.commit()
        except Exception as e:
            logger.warning(f"Entry processing failed for id={entry.id}: {e}")

    async def run_source(self, db: Session, source: IntelSource) -> int:
        """Run collection for a single source."""
        if not source.is_active:
            return 0

        if source.source_type == IntelSourceType.RSS:
            count = await self.collect_rss(db, source)
        elif source.source_type == IntelSourceType.CRAWLER:
            count = await self.collect_crawler(db, source)
        else:
            count = 0

        source.last_run_at = datetime.datetime.utcnow()
        db.commit()

        # Process new entries with LLM
        if count > 0:
            try:
                from app.services.llm_gateway import llm_gateway
                model_config = llm_gateway.get_config(db)
                # Get the entries we just added (most recent)
                new_entries = (
                    db.query(IntelEntry)
                    .filter(
                        IntelEntry.source_id == source.id,
                        IntelEntry.industry == None,  # noqa: E711
                    )
                    .order_by(IntelEntry.created_at.desc())
                    .limit(count)
                    .all()
                )
                for entry in new_entries:
                    await self.process_entry(db, entry, model_config)
            except Exception as e:
                logger.warning(f"Batch processing failed: {e}")

        return count


intel_collector = IntelCollector()
