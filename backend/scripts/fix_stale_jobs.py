"""一次性数据修复脚本：清理垃圾 job + 修复存量条目状态。

用法: cd backend && python scripts/fix_stale_jobs.py [--dry-run]
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func
from app.database import SessionLocal
from app.models.knowledge import KnowledgeEntry
from app.models.knowledge_job import KnowledgeJob

DRY_RUN = "--dry-run" in sys.argv


def main():
    db = SessionLocal()
    try:
        # ── 1. 清理累计失败 > 5 次的 failed job（保留最近 1 条作为记录）────
        print("=== 清理累计失败 job ===")
        # 找出每个 (knowledge_id, job_type) 组合中 failed 数量 > 5 的
        heavy_failures = (
            db.query(
                KnowledgeJob.knowledge_id,
                KnowledgeJob.job_type,
                func.count(KnowledgeJob.id).label("cnt"),
            )
            .filter(KnowledgeJob.status == "failed")
            .group_by(KnowledgeJob.knowledge_id, KnowledgeJob.job_type)
            .having(func.count(KnowledgeJob.id) > 5)
            .all()
        )

        total_deleted = 0
        for kid, jtype, cnt in heavy_failures:
            # 保留最近 1 条 failed job，删除其余
            keep_job = (
                db.query(KnowledgeJob)
                .filter(
                    KnowledgeJob.knowledge_id == kid,
                    KnowledgeJob.job_type == jtype,
                    KnowledgeJob.status == "failed",
                )
                .order_by(KnowledgeJob.id.desc())
                .first()
            )
            to_delete = (
                db.query(KnowledgeJob)
                .filter(
                    KnowledgeJob.knowledge_id == kid,
                    KnowledgeJob.job_type == jtype,
                    KnowledgeJob.status == "failed",
                    KnowledgeJob.id != keep_job.id,
                )
                .all()
            )
            total_deleted += len(to_delete)
            print(f"  entry={kid} type={jtype}: {cnt} failed → 删除 {len(to_delete)} 条，保留 job #{keep_job.id}")
            if not DRY_RUN:
                for j in to_delete:
                    db.delete(j)

        # ── 2. 清理 stuck running job（started_at 超过 1 小时的 running job）────
        print("\n=== 清理 stuck running job ===")
        import datetime
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=1)
        stuck_jobs = (
            db.query(KnowledgeJob)
            .filter(
                KnowledgeJob.status == "running",
                KnowledgeJob.started_at < cutoff,
            )
            .all()
        )
        for j in stuck_jobs:
            print(f"  job #{j.id} type={j.job_type} entry={j.knowledge_id} — 标记为 failed")
            if not DRY_RUN:
                j.status = "failed"
                j.error_message = "stuck running > 1h, auto-recovered"

        # ── 3. 修复 file_ext=None 的条目 ────────────────────────────────
        print("\n=== 修复缺失 file_ext 的条目 ===")
        no_ext_entries = (
            db.query(KnowledgeEntry)
            .filter(
                KnowledgeEntry.file_ext.is_(None),
                KnowledgeEntry.source_file.isnot(None),
            )
            .all()
        )
        for e in no_ext_entries:
            ext = os.path.splitext(e.source_file or "")[1].lower()
            if ext:
                print(f"  entry #{e.id} '{e.title}': source_file='{e.source_file}' → file_ext='{ext}'")
                if not DRY_RUN:
                    e.file_ext = ext

        # ── 4. 为无 content_html 但有 content 的条目生成 fallback HTML ──
        print("\n=== 为无 HTML 的条目生成 fallback ===")
        no_html = (
            db.query(KnowledgeEntry)
            .filter(
                KnowledgeEntry.content.isnot(None),
                KnowledgeEntry.content != "",
                (KnowledgeEntry.content_html.is_(None)) | (KnowledgeEntry.content_html == ""),
            )
            .all()
        )
        for e in no_html:
            from app.services.doc_renderer import render_from_content
            ext = (e.file_ext or "").lower()
            html = render_from_content(e.content, ext)
            if html:
                print(f"  entry #{e.id} '{e.title}': 生成 {len(html)} 字符 HTML")
                if not DRY_RUN:
                    e.content_html = html
                    if e.doc_render_status in (None, "pending"):
                        e.doc_render_status = "ready"
                        e.doc_render_mode = "text_fallback"

        # ── 汇总 ────────────────────────────────────────────────────────
        prefix = "[DRY-RUN] " if DRY_RUN else ""
        print(f"\n{prefix}删除垃圾 job: {total_deleted}")
        print(f"{prefix}修复 stuck running: {len(stuck_jobs)}")
        print(f"{prefix}修复 file_ext: {len(no_ext_entries)}")
        print(f"{prefix}生成 fallback HTML: {len(no_html)}")

        if not DRY_RUN:
            db.commit()
            print("\n✓ 数据修复完成")
        else:
            print("\n(dry-run 模式，未实际修改)")
    except Exception:
        import traceback
        traceback.print_exc()
        db.rollback()
    finally:
        db.close()


if __name__ == "__main__":
    main()
