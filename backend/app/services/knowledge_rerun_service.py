"""知识库 Rerun 服务：目录变更后对文档 folder 重绑 + 编号重算。

不重新调 LLM 分类，只做确定性 folder 重绑 + 编号重算。

前缀格式约定：系统前缀用 `[CODE] ` 格式包裹（如 `[A1.2] 投放策略文档`），
以此稳定区分"系统编号"和"语义标题主体"。
"""
import datetime
import logging
import re
from typing import Optional

from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeEntry, KnowledgeFolder
from app.models.knowledge_admin import KnowledgeRerunJob, RerunStatus

logger = logging.getLogger(__name__)

# 系统前缀格式：[CODE] ——正则匹配
_PREFIX_PATTERN = re.compile(r"^\[([^\]]*)\]\s*")


def _get_subtree_ids(db: Session, folder_id: int) -> list[int]:
    """递归获取 folder_id 子树下所有 folder id（含自身）。"""
    result = [folder_id]
    children = db.query(KnowledgeFolder.id).filter(
        KnowledgeFolder.parent_id == folder_id
    ).all()
    for (child_id,) in children:
        result.extend(_get_subtree_ids(db, child_id))
    return result


def _build_folder_path(db: Session, folder_id: int) -> list[str]:
    """从叶子往根方向构建路径名称列表。"""
    path = []
    current_id: Optional[int] = folder_id
    visited = set()
    while current_id and current_id not in visited:
        visited.add(current_id)
        folder = db.get(KnowledgeFolder, current_id)
        if not folder:
            break
        path.append(folder.name)
        current_id = folder.parent_id
    path.reverse()
    return path


def _compute_title_prefix(folder_path: list[str], taxonomy_code: Optional[str]) -> str:
    """根据目录路径和分类编码生成系统编号前缀（不含方括号包裹）。"""
    if taxonomy_code:
        return taxonomy_code
    if folder_path:
        return "/".join(folder_path[:2])
    return ""


def _format_prefix(prefix: str) -> str:
    """把裸前缀包裹成标准格式 `[CODE] `。"""
    if not prefix:
        return ""
    return f"[{prefix}] "


def _strip_prefix(title: str) -> str:
    """去掉标题中的系统前缀，返回语义标题主体。"""
    return _PREFIX_PATTERN.sub("", title)


def _apply_prefix(title: str, new_prefix: str) -> str:
    """给标题加上新的系统前缀。先移除旧前缀再加新的。"""
    body = _strip_prefix(title)
    if not new_prefix:
        return body
    return f"[{new_prefix}] {body}"


def _build_global_taxonomy_map(db: Session) -> dict[str, int]:
    """构建全局 taxonomy_code → folder_id 映射（仅 is_system=1 目录）。"""
    folders = db.query(KnowledgeFolder).filter(
        KnowledgeFolder.is_system == 1,
        KnowledgeFolder.taxonomy_code.isnot(None),
    ).all()
    return {f.taxonomy_code: f.id for f in folders if f.taxonomy_code}


def _rebind_entry(
    db: Session,
    entry: KnowledgeEntry,
    taxonomy_map: dict[str, int],
    stats: dict,
) -> None:
    """对单条 entry 做 folder 重绑 + 前缀重算。

    修改 stats dict in-place。
    """
    old_folder_id = entry.folder_id
    old_prefix = entry.system_title_prefix or ""

    # 1. 按 taxonomy_code 重绑 folder
    if entry.taxonomy_code and entry.taxonomy_code in taxonomy_map:
        new_folder_id = taxonomy_map[entry.taxonomy_code]
        if new_folder_id != old_folder_id:
            entry.folder_id = new_folder_id
            stats["reclassified"] += 1
    elif entry.taxonomy_code:
        # taxonomy_code 有值但对应 folder 不存在 → needs_review
        entry.classification_status = "needs_review"
        stats["skipped"] += 1
        return
    elif not entry.folder_id:
        # 既没有 taxonomy_code 也没有 folder_id → needs_review
        entry.classification_status = "needs_review"
        stats["skipped"] += 1
        return

    # 2. 重算 system_title_prefix
    if entry.folder_id:
        folder_path = _build_folder_path(db, entry.folder_id)
        new_prefix = _compute_title_prefix(folder_path, entry.taxonomy_code)
        entry.system_title_prefix = new_prefix

        # 3. 如果标题没被用户锁定，用稳定的前缀拆分重命名
        if not entry.manual_title_locked and new_prefix != old_prefix:
            new_title = _apply_prefix(entry.title, new_prefix)
            if new_title != entry.title:
                entry.title = new_title
                stats["renamed"] += 1


def execute_rerun(db: Session, job: KnowledgeRerunJob) -> None:
    """执行 rerun 作业（rename/move 场景）。

    处理 target_folder_id 子树下所有 knowledge_entries。
    """
    job.status = RerunStatus.RUNNING
    job.started_at = datetime.datetime.utcnow()
    db.commit()

    try:
        subtree_ids = _get_subtree_ids(db, job.target_folder_id)
        taxonomy_map = _build_global_taxonomy_map(db)

        entries = db.query(KnowledgeEntry).filter(
            KnowledgeEntry.folder_id.in_(subtree_ids)
        ).all()

        stats = {"reclassified": 0, "renamed": 0, "failed": 0, "skipped": 0}
        errors: list[str] = []

        for entry in entries:
            try:
                _rebind_entry(db, entry, taxonomy_map, stats)
            except Exception as e:
                stats["failed"] += 1
                errors.append(f"entry_id={entry.id}: {str(e)}")
                logger.warning(f"Rerun failed for entry {entry.id}: {e}")

        _finalize_job(job, len(entries), stats, errors)
        db.commit()

    except Exception as e:
        logger.error(f"Rerun job {job.id} failed: {e}")
        job.status = RerunStatus.FAILED
        job.error_log = str(e)
        job.finished_at = datetime.datetime.utcnow()
        db.commit()
        raise


def execute_orphan_rerun(
    db: Session, job: KnowledgeRerunJob, orphan_entry_ids: list[int],
) -> None:
    """执行 orphan rerun（folder_delete 场景）。

    处理 folder_id 已被清空但仍有 taxonomy_code 的文档，
    尝试按 taxonomy_code 全局重绑到其他存活的系统目录。
    无法重绑的标 needs_review。
    """
    job.status = RerunStatus.RUNNING
    job.started_at = datetime.datetime.utcnow()
    db.commit()

    try:
        taxonomy_map = _build_global_taxonomy_map(db)

        # 分批处理避免内存爆炸
        BATCH = 500
        stats = {"reclassified": 0, "renamed": 0, "failed": 0, "skipped": 0}
        errors: list[str] = []
        total = 0

        for i in range(0, len(orphan_entry_ids), BATCH):
            batch_ids = orphan_entry_ids[i : i + BATCH]
            entries = db.query(KnowledgeEntry).filter(
                KnowledgeEntry.id.in_(batch_ids)
            ).all()
            total += len(entries)

            for entry in entries:
                try:
                    _rebind_entry(db, entry, taxonomy_map, stats)
                except Exception as e:
                    stats["failed"] += 1
                    errors.append(f"entry_id={entry.id}: {str(e)}")
                    logger.warning(f"Orphan rerun failed for entry {entry.id}: {e}")

            db.flush()

        _finalize_job(job, total, stats, errors)
        db.commit()

    except Exception as e:
        logger.error(f"Orphan rerun job {job.id} failed: {e}")
        job.status = RerunStatus.FAILED
        job.error_log = str(e)
        job.finished_at = datetime.datetime.utcnow()
        db.commit()
        raise


def _finalize_job(
    job: KnowledgeRerunJob,
    affected: int,
    stats: dict,
    errors: list[str],
) -> None:
    job.affected_count = affected
    job.reclassified_count = stats["reclassified"]
    job.renamed_count = stats["renamed"]
    job.failed_count = stats["failed"]
    job.skipped_count = stats["skipped"]
    job.status = RerunStatus.SUCCESS if stats["failed"] == 0 else RerunStatus.FAILED
    job.error_log = "\n".join(errors) if errors else None
    job.finished_at = datetime.datetime.utcnow()
