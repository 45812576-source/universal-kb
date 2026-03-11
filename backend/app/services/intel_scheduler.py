"""APScheduler-based scheduler for intel source collection."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_scheduler = None


def start_intel_scheduler():
    """Start the APScheduler background scheduler for intel collection."""
    global _scheduler
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("apscheduler not installed, intel scheduler disabled")
        return

    from app.database import SessionLocal
    from app.models.intel import IntelSource
    from app.services.intel_collector import intel_collector

    _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    async def _run_all_sources():
        """Scan intel_sources table and run due sources."""
        db = SessionLocal()
        try:
            sources = db.query(IntelSource).filter(IntelSource.is_active == True).all()
            for source in sources:
                if source.schedule:
                    # Each source has its own schedule — handled by individual jobs
                    pass
                else:
                    # Sources without schedule run every time (manual trigger only)
                    pass
        finally:
            db.close()

    async def _run_source_by_id(source_id: int):
        db = SessionLocal()
        try:
            source = db.get(IntelSource, source_id)
            if source:
                count = await intel_collector.run_source(db, source)
                logger.info(f"Scheduled run for source '{source.name}': {count} new entries")
        finally:
            db.close()

    # Add a global scan job every hour to pick up new sources
    _scheduler.add_job(
        _sync_source_jobs,
        "interval",
        hours=1,
        id="intel_sync_jobs",
        replace_existing=True,
        args=[_scheduler],
    )

    _scheduler.start()
    logger.info("Intel scheduler started")


async def _sync_source_jobs(scheduler):
    """Sync APScheduler jobs from intel_sources table."""
    from apscheduler.triggers.cron import CronTrigger
    from app.database import SessionLocal
    from app.models.intel import IntelSource, IntelSourceType
    from app.services.intel_collector import intel_collector

    db = SessionLocal()
    try:
        sources = db.query(IntelSource).filter(IntelSource.is_active == True).all()
        existing_job_ids = {job.id for job in scheduler.get_jobs()}

        for source in sources:
            job_id = f"intel_source_{source.id}"
            if source.schedule and job_id not in existing_job_ids:
                # 支持所有类型：rss, crawler, deep_crawl
                async def _run(src_id=source.id):
                    dbs = SessionLocal()
                    try:
                        src = dbs.get(IntelSource, src_id)
                        if src:
                            await intel_collector.run_source(dbs, src)
                    finally:
                        dbs.close()

                try:
                    trigger = CronTrigger.from_crontab(source.schedule, timezone="Asia/Shanghai")
                    scheduler.add_job(
                        _run,
                        trigger,
                        id=job_id,
                        replace_existing=True,
                    )
                    logger.info(f"Registered intel job for source '{source.name}' ({source.schedule}), type={source.source_type.value}")
                except Exception as e:
                    logger.warning(f"Invalid cron schedule for source '{source.name}': {e}")
    finally:
        db.close()


def stop_intel_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown()
        _scheduler = None
