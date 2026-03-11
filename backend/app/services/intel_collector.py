"""External intelligence collection engine: RSS / Crawler (crawl4ai) / Deep Crawl / Manual."""
from __future__ import annotations

import datetime
import hashlib
import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.intel import (
    IntelEntry,
    IntelEntryStatus,
    IntelSource,
    IntelSourceType,
    IntelTask,
    IntelTaskStatus,
)

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

    # ── RSS ────────────────────────────────────────────────────────────────────

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

    # ── Crawler (crawl4ai) ─────────────────────────────────────────────────────

    async def collect_crawler(self, db: Session, source: IntelSource, task: Optional[IntelTask] = None) -> int:
        """Crawl a single URL using crawl4ai with JS rendering support."""
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

        config = source.config or {}
        url = config.get("url", "")
        if not url:
            return 0

        browser_config = BrowserConfig(headless=True, verbose=False)
        run_config = CrawlerRunConfig(
            wait_until="networkidle",
            word_count_threshold=50,
        )

        # 如果有自定义 wait_selector，使用 CSS 等待
        wait_selector = config.get("wait_selector")
        if wait_selector:
            run_config = CrawlerRunConfig(
                wait_until="networkidle",
                wait_for=f"css:{wait_selector}",
                word_count_threshold=50,
            )

        if task:
            task.total_urls = 1
            db.commit()

        count = 0
        try:
            async with AsyncWebCrawler(config=browser_config) as crawler:
                result = await crawler.arun(url=url, config=run_config)
                if result.success:
                    title = result.metadata.get("title", "") if result.metadata else ""
                    if not title:
                        title = url

                    if not self._is_duplicate(db, url, title):
                        # 取 markdown 内容，截取前 5000 字作为 content
                        markdown_content = result.markdown or ""
                        intel = IntelEntry(
                            source_id=source.id,
                            title=title[:500],
                            content=markdown_content[:5000] if markdown_content else None,
                            raw_markdown=markdown_content if markdown_content else None,
                            url=url,
                            depth=0,
                            status=IntelEntryStatus.PENDING,
                            auto_collected=True,
                        )
                        db.add(intel)
                        count += 1
                        db.commit()
                else:
                    logger.warning(f"Crawl failed for {url}: {result.error_message}")
        except Exception as e:
            logger.error(f"Crawler error for {url}: {e}")
            if task:
                task.error_message = str(e)

        if task:
            task.crawled_urls = 1
            task.new_entries = count
            db.commit()

        return count

    # ── Deep Crawl (crawl4ai BFS) ─────────────────────────────────────────────

    async def collect_deep_crawl(self, db: Session, source: IntelSource, task: Optional[IntelTask] = None) -> int:
        """Deep crawl using crawl4ai BFS strategy — recursive multi-page crawl."""
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
        from crawl4ai.deep_crawling import BFSDeepCrawlStrategy
        from crawl4ai.deep_crawling.filters import FilterChain, URLPatternFilter, DomainFilter

        config = source.config or {}
        url = config.get("url", "")
        if not url:
            return 0

        max_depth = config.get("max_depth", 2)
        max_pages = config.get("max_pages", 20)
        include_external = config.get("include_external", False)
        url_patterns = config.get("url_patterns", [])

        # 构建过滤链
        filters = []
        if not include_external:
            from urllib.parse import urlparse
            domain = urlparse(url).netloc
            filters.append(DomainFilter(allowed_domains=[domain]))
        if url_patterns:
            filters.append(URLPatternFilter(patterns=url_patterns))

        deep_strategy = BFSDeepCrawlStrategy(
            max_depth=max_depth,
            max_pages=max_pages,
            filter_chain=FilterChain(filters) if filters else None,
            include_external=include_external,
        )

        browser_config = BrowserConfig(headless=True, verbose=False)
        run_config = CrawlerRunConfig(
            deep_crawl_strategy=deep_strategy,
            wait_until="networkidle",
            word_count_threshold=50,
        )

        if task:
            task.total_urls = max_pages
            db.commit()

        count = 0
        try:
            async with AsyncWebCrawler(config=browser_config) as crawler:
                results = await crawler.arun(url=url, config=run_config)
                # deep crawl 返回结果列表
                if not isinstance(results, list):
                    results = [results]

                if task:
                    task.total_urls = len(results)
                    db.commit()

                for i, result in enumerate(results):
                    if not result.success:
                        continue

                    page_url = result.url or url
                    title = ""
                    if result.metadata:
                        title = result.metadata.get("title", "")
                    if not title:
                        title = page_url

                    if self._is_duplicate(db, page_url, title):
                        if task:
                            task.crawled_urls = i + 1
                            db.commit()
                        continue

                    markdown_content = result.markdown or ""
                    # 推算深度：根据 result 的 depth 属性或默认 0
                    depth = getattr(result, "depth", 0)

                    intel = IntelEntry(
                        source_id=source.id,
                        title=title[:500],
                        content=markdown_content[:5000] if markdown_content else None,
                        raw_markdown=markdown_content if markdown_content else None,
                        url=page_url,
                        depth=depth,
                        status=IntelEntryStatus.PENDING,
                        auto_collected=True,
                    )
                    db.add(intel)
                    count += 1

                    if task:
                        task.crawled_urls = i + 1
                        task.new_entries = count
                        db.commit()

                db.commit()
        except Exception as e:
            logger.error(f"Deep crawl error for {url}: {e}")
            if task:
                task.error_message = str(e)
                db.commit()

        return count

    # ── Batch Crawl ────────────────────────────────────────────────────────────

    async def collect_batch(self, db: Session, source: IntelSource, task: Optional[IntelTask] = None) -> int:
        """Batch crawl multiple URLs concurrently using crawl4ai arun_many."""
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

        config = source.config or {}
        urls = config.get("urls", [])
        if not urls:
            return 0

        browser_config = BrowserConfig(headless=True, verbose=False)
        run_config = CrawlerRunConfig(
            wait_until="networkidle",
            word_count_threshold=50,
        )

        if task:
            task.total_urls = len(urls)
            db.commit()

        count = 0
        try:
            async with AsyncWebCrawler(config=browser_config) as crawler:
                results = await crawler.arun_many(urls=urls, config=run_config)
                for i, result in enumerate(results):
                    if not result.success:
                        continue

                    page_url = result.url or urls[i] if i < len(urls) else ""
                    title = ""
                    if result.metadata:
                        title = result.metadata.get("title", "")
                    if not title:
                        title = page_url

                    if self._is_duplicate(db, page_url, title):
                        if task:
                            task.crawled_urls = i + 1
                            db.commit()
                        continue

                    markdown_content = result.markdown or ""
                    intel = IntelEntry(
                        source_id=source.id,
                        title=title[:500],
                        content=markdown_content[:5000] if markdown_content else None,
                        raw_markdown=markdown_content if markdown_content else None,
                        url=page_url,
                        depth=0,
                        status=IntelEntryStatus.PENDING,
                        auto_collected=True,
                    )
                    db.add(intel)
                    count += 1

                    if task:
                        task.crawled_urls = i + 1
                        task.new_entries = count
                        db.commit()

                db.commit()
        except Exception as e:
            logger.error(f"Batch crawl error: {e}")
            if task:
                task.error_message = str(e)
                db.commit()

        return count

    # ── LLM Processing ─────────────────────────────────────────────────────────

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
            result, _ = await llm_gateway.chat(
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

    # ── Orchestrator ───────────────────────────────────────────────────────────

    async def run_source(self, db: Session, source: IntelSource, task: Optional[IntelTask] = None) -> int:
        """Run collection for a single source."""
        if not source.is_active:
            return 0

        if task:
            task.status = IntelTaskStatus.RUNNING
            task.started_at = datetime.datetime.utcnow()
            db.commit()

        try:
            if source.source_type == IntelSourceType.RSS:
                count = await self.collect_rss(db, source)
            elif source.source_type == IntelSourceType.CRAWLER:
                count = await self.collect_crawler(db, source, task=task)
            elif source.source_type == IntelSourceType.DEEP_CRAWL:
                count = await self.collect_deep_crawl(db, source, task=task)
            else:
                count = 0

            source.last_run_at = datetime.datetime.utcnow()
            db.commit()

            # Process new entries with LLM
            if count > 0:
                try:
                    from app.services.llm_gateway import llm_gateway
                    model_config = llm_gateway.get_config(db)
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

            if task:
                task.status = IntelTaskStatus.COMPLETED
                task.new_entries = count
                task.finished_at = datetime.datetime.utcnow()
                db.commit()

            return count

        except Exception as e:
            logger.error(f"Source run failed for '{source.name}': {e}")
            if task:
                task.status = IntelTaskStatus.FAILED
                task.error_message = str(e)
                task.finished_at = datetime.datetime.utcnow()
                db.commit()
            return 0


intel_collector = IntelCollector()
