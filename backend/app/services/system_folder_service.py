"""系统归档树服务：基于 taxonomy 分类树自动生成系统文件夹。

系统归档树与用户自建树并存：
- is_system=1: 系统目录，基于 taxonomy 自动生成，不可删除
- is_system=0: 用户自建目录

自动归档默认写入系统归档树。
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.data.knowledge_taxonomy import TAXONOMY
from app.models.knowledge import KnowledgeFolder

logger = logging.getLogger(__name__)

# taxonomy board 中文名
_BOARD_NAMES = {
    "A": "A.渠道与平台",
    "B": "B.行业与品类",
    "C": "C.消费者与用户",
    "D": "D.方法论与策略",
    "E": "E.公司与团队",
    "F": "F.合规与风控",
}


def ensure_system_folders(db: Session, owner_id: int = 1) -> dict[str, int]:
    """确保系统归档树存在。返回 {taxonomy_code: folder_id} 映射。

    幂等操作：已存在的不重建。
    """
    code_to_folder: dict[str, int] = {}

    # 1. 确保 board 级目录存在
    board_folders: dict[str, KnowledgeFolder] = {}
    for board_code, board_name in _BOARD_NAMES.items():
        existing = (
            db.query(KnowledgeFolder)
            .filter(
                KnowledgeFolder.is_system == 1,
                KnowledgeFolder.taxonomy_board == board_code,
                KnowledgeFolder.parent_id.is_(None),
            )
            .first()
        )
        if existing:
            board_folders[board_code] = existing
        else:
            f = KnowledgeFolder(
                name=board_name,
                parent_id=None,
                created_by=owner_id,
                is_system=1,
                taxonomy_board=board_code,
            )
            db.add(f)
            db.flush()
            board_folders[board_code] = f
            logger.info(f"Created system board folder: {board_name} (id={f.id})")

    # 2. 为每个 taxonomy 节点创建叶子目录
    #    按 path 深度展开，跳过 board 级（已创建）
    for node in TAXONOMY:
        code = node["code"]
        board = node["board"]
        path_parts = node.get("path", [])
        node_name = node["name"]

        if board not in board_folders:
            continue

        # 查找或创建
        existing = (
            db.query(KnowledgeFolder)
            .filter(
                KnowledgeFolder.is_system == 1,
                KnowledgeFolder.taxonomy_code == code,
            )
            .first()
        )
        if existing:
            code_to_folder[code] = existing.id
            continue

        # 确定父目录：挂在 board 下
        parent_id = board_folders[board].id

        # 建中间层（如果 path 有多级）
        # path 示例: ["A.渠道与平台", "A1.国内付费渠道", "A1.1.短视频/直播", "A1.1.抖音/巨量引擎"]
        # board 级已有，从第二层开始
        for i, part in enumerate(path_parts[1:-1], start=1):
            mid_key = f"{board}:path:{i}:{part}"
            mid = (
                db.query(KnowledgeFolder)
                .filter(
                    KnowledgeFolder.is_system == 1,
                    KnowledgeFolder.name == part,
                    KnowledgeFolder.parent_id == parent_id,
                )
                .first()
            )
            if mid:
                parent_id = mid.id
            else:
                mid = KnowledgeFolder(
                    name=part,
                    parent_id=parent_id,
                    created_by=owner_id,
                    is_system=1,
                    taxonomy_board=board,
                )
                db.add(mid)
                db.flush()
                parent_id = mid.id

        # 创建叶子节点
        leaf = KnowledgeFolder(
            name=node_name,
            parent_id=parent_id,
            created_by=owner_id,
            is_system=1,
            taxonomy_board=board,
            taxonomy_code=code,
        )
        db.add(leaf)
        db.flush()
        code_to_folder[code] = leaf.id

    db.commit()
    logger.info(f"System folder tree ensured: {len(code_to_folder)} leaf nodes")
    return code_to_folder


def get_system_folder_for_taxonomy(db: Session, taxonomy_code: str) -> Optional[int]:
    """根据分类编码获取系统归档目录 ID。"""
    f = (
        db.query(KnowledgeFolder)
        .filter(
            KnowledgeFolder.is_system == 1,
            KnowledgeFolder.taxonomy_code == taxonomy_code,
        )
        .first()
    )
    return f.id if f else None


def get_system_folder_for_board(db: Session, board: str) -> Optional[int]:
    """根据 board 获取板块级系统目录 ID（用于低置信度归档到板块根目录）。"""
    f = (
        db.query(KnowledgeFolder)
        .filter(
            KnowledgeFolder.is_system == 1,
            KnowledgeFolder.taxonomy_board == board,
            KnowledgeFolder.parent_id.is_(None),
        )
        .first()
    )
    return f.id if f else None
