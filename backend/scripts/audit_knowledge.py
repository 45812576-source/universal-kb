"""知识库数据巡检脚本：扫描并修复常见异常状态。

用法: cd backend && python scripts/audit_knowledge.py [--fix]
    不加 --fix 仅报告，加 --fix 自动修复高置信度问题。
"""
import sys
import os
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, text, or_, and_
from app.database import SessionLocal
from app.models.knowledge import KnowledgeEntry, KnowledgeFolder
from app.models.knowledge_job import KnowledgeJob

FIX = "--fix" in sys.argv


def main():
    db = SessionLocal()
    try:
        fixes = 0

        # ── 1. folder_id 指向不存在 folder 的条目 ──
        print("=== 1. folder_id 指向不存在的 folder ===")
        valid_folder_ids = {r[0] for r in db.query(KnowledgeFolder.id).all()}
        orphan_entries = (
            db.query(KnowledgeEntry)
            .filter(
                KnowledgeEntry.folder_id.isnot(None),
            )
            .all()
        )
        orphan_entries = [e for e in orphan_entries if e.folder_id not in valid_folder_ids]
        for e in orphan_entries:
            print(f"  #{e.id} \"{e.title[:40]}\" folder_id={e.folder_id} (不存在)")
        if FIX and orphan_entries:
            for e in orphan_entries:
                e.folder_id = None  # 清空无效 folder_id，让 backfill 重新归类
            fixes += len(orphan_entries)
            print(f"  → 已清空 {len(orphan_entries)} 条的 folder_id")

        # ── 2. ready 但 content_html 和 content 都为空 ──
        print("\n=== 2. doc_render_status=ready 但正文为空 ===")
        fake_ready = (
            db.query(KnowledgeEntry)
            .filter(
                KnowledgeEntry.doc_render_status == "ready",
                or_(
                    KnowledgeEntry.content_html.is_(None),
                    KnowledgeEntry.content_html == "",
                ),
                or_(
                    KnowledgeEntry.content.is_(None),
                    KnowledgeEntry.content == "",
                ),
            )
            .all()
        )
        for e in fake_ready:
            print(f"  #{e.id} \"{e.title[:40]}\" mode={e.doc_render_mode} oss={bool(e.oss_key)}")
        if FIX and fake_ready:
            for e in fake_ready:
                if e.oss_key:
                    # 有 OSS 文件但正文空，标记为 pending 让 worker 重新渲染
                    e.doc_render_status = "pending"
                    e.doc_render_error = "巡检发现正文为空，已重置为 pending"
                else:
                    e.doc_render_status = "failed"
                    e.doc_render_error = "无文件且正文为空"
            fixes += len(fake_ready)
            print(f"  → 已修复 {len(fake_ready)} 条")

        # ── 3. source_type=lark_doc 但缺 external_edit_mode 或 lark_doc_url ──
        print("\n=== 3. 飞书文档缺 external_edit_mode 或 lark_doc_url ===")
        lark_missing = (
            db.query(KnowledgeEntry)
            .filter(
                KnowledgeEntry.source_type == "lark_doc",
                or_(
                    KnowledgeEntry.external_edit_mode.is_(None),
                    KnowledgeEntry.lark_doc_url.is_(None),
                    KnowledgeEntry.lark_doc_url == "",
                ),
            )
            .all()
        )
        for e in lark_missing:
            print(f"  #{e.id} \"{e.title[:40]}\" edit_mode={e.external_edit_mode} url={bool(e.lark_doc_url)}")
        if FIX and lark_missing:
            for e in lark_missing:
                if not e.external_edit_mode:
                    e.external_edit_mode = "detached_copy"
                if not e.lark_doc_url and e.source_file and "feishu.cn" in (e.source_file or ""):
                    e.lark_doc_url = e.source_file
            fixes += len(lark_missing)
            print(f"  → 已修复 {len(lark_missing)} 条")

        # ── 4. content 有值但 content_html 为空（缺 fallback） ──
        print("\n=== 4. 有 content 但无 content_html（缺 fallback） ===")
        no_html = (
            db.query(KnowledgeEntry)
            .filter(
                KnowledgeEntry.content.isnot(None),
                KnowledgeEntry.content != "",
                or_(
                    KnowledgeEntry.content_html.is_(None),
                    KnowledgeEntry.content_html == "",
                ),
            )
            .all()
        )
        for e in no_html:
            print(f"  #{e.id} \"{e.title[:40]}\" ext={e.file_ext} render={e.doc_render_status}")
        if FIX and no_html:
            from app.services.doc_renderer import render_from_content
            fixed_count = 0
            for e in no_html:
                ext = (e.file_ext or "").lower()
                html = render_from_content(e.content, ext)
                if html:
                    e.content_html = html
                    if e.doc_render_status in (None, "pending", "failed"):
                        e.doc_render_status = "ready"
                        e.doc_render_mode = "text_fallback"
                    fixed_count += 1
            fixes += fixed_count
            print(f"  → 已生成 fallback HTML: {fixed_count} 条")

        # ── 5. stuck running jobs (>10min) ──
        print("\n=== 5. Stuck running jobs (>10min) ===")
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(minutes=10)
        stuck = (
            db.query(KnowledgeJob)
            .filter(
                KnowledgeJob.status == "running",
                KnowledgeJob.started_at < cutoff,
            )
            .all()
        )
        for j in stuck:
            print(f"  job#{j.id} entry#{j.knowledge_id} type={j.job_type} started={j.started_at}")
        if FIX and stuck:
            for j in stuck:
                if j.attempt_count >= j.max_attempts:
                    j.status = "failed"
                else:
                    j.status = "queued"
                j.error_message = (j.error_message or "") + " [audit: stuck >10min]"
                j.finished_at = datetime.datetime.utcnow()
            fixes += len(stuck)
            print(f"  → 已回收 {len(stuck)} 个 stuck job")

        # ── 6. file_ext 为 None 的上传条目 ──
        print("\n=== 6. 上传文件缺 file_ext ===")
        no_ext = (
            db.query(KnowledgeEntry)
            .filter(
                KnowledgeEntry.file_ext.is_(None),
                KnowledgeEntry.source_file.isnot(None),
                KnowledgeEntry.source_type.in_(["upload", "chat_upload"]),
            )
            .all()
        )
        for e in no_ext:
            print(f"  #{e.id} \"{e.title[:40]}\" source_file={e.source_file}")
        if FIX and no_ext:
            fixed_count = 0
            for e in no_ext:
                ext = os.path.splitext(e.source_file or "")[1].lower()
                if ext:
                    e.file_ext = ext
                    fixed_count += 1
            fixes += fixed_count
            print(f"  → 已修复 {fixed_count} 条")

        # ── 汇总 ──
        print(f"\n{'[FIX] ' if FIX else ''}总计发现问题:")
        print(f"  orphan folder_id: {len(orphan_entries)}")
        print(f"  fake ready: {len(fake_ready)}")
        print(f"  lark missing fields: {len(lark_missing)}")
        print(f"  no content_html: {len(no_html)}")
        print(f"  stuck jobs: {len(stuck)}")
        print(f"  no file_ext: {len(no_ext)}")

        if FIX:
            db.commit()
            print(f"\n✓ 已修复 {fixes} 项")
        else:
            print("\n(仅报告模式，加 --fix 自动修复)")

    except Exception:
        import traceback
        traceback.print_exc()
        db.rollback()
    finally:
        db.close()


if __name__ == "__main__":
    main()
