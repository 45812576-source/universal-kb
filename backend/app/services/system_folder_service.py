"""系统归档树服务：基于 taxonomy 分类树自动生成系统文件夹。

系统归档树与用户自建树并存：
- is_system=0: 用户自建目录
- is_system=1: 系统目录，基于 taxonomy 自动生成，不可删除
- is_system=2: 治理目录，基于 GovernanceObjective 目标树生成

自动归档默认写入系统归档树。
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.data.knowledge_taxonomy import TAXONOMY
from app.models.knowledge import KnowledgeFolder
from app.models.user import Department

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


def _business_unit_key(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _known_business_units(db: Session) -> list[str]:
    rows = db.query(Department.business_unit).distinct().all()
    units = sorted({_business_unit_key(row[0]) for row in rows if _business_unit_key(row[0])})
    return units or ["未分配事业部"]


def ensure_system_folders(db: Session, owner_id: int = 1) -> dict[str, int]:
    """确保系统归档树存在。返回 {business_unit:taxonomy_code: folder_id} 映射。

    幂等操作：已存在的不重建。
    """
    code_to_folder: dict[str, int] = {}
    for business_unit in _known_business_units(db):
        bu_root = (
            db.query(KnowledgeFolder)
            .filter(
                KnowledgeFolder.is_system == 1,
                KnowledgeFolder.parent_id.is_(None),
                KnowledgeFolder.business_unit == business_unit,
                KnowledgeFolder.taxonomy_board.is_(None),
                KnowledgeFolder.taxonomy_code.is_(None),
            )
            .first()
        )
        if not bu_root:
            bu_root = KnowledgeFolder(
                name=business_unit,
                parent_id=None,
                created_by=owner_id,
                is_system=1,
                business_unit=business_unit,
            )
            db.add(bu_root)
            db.flush()

        board_folders: dict[str, KnowledgeFolder] = {}
        for board_code, board_name in _BOARD_NAMES.items():
            existing = (
                db.query(KnowledgeFolder)
                .filter(
                    KnowledgeFolder.is_system == 1,
                    KnowledgeFolder.taxonomy_board == board_code,
                    KnowledgeFolder.parent_id == bu_root.id,
                    KnowledgeFolder.business_unit == business_unit,
                )
                .first()
            )
            if existing:
                board_folders[board_code] = existing
            else:
                f = KnowledgeFolder(
                    name=board_name,
                    parent_id=bu_root.id,
                    created_by=owner_id,
                    is_system=1,
                    taxonomy_board=board_code,
                    business_unit=business_unit,
                )
                db.add(f)
                db.flush()
                board_folders[board_code] = f

        for node in TAXONOMY:
            code = node["code"]
            board = node["board"]
            path_parts = node.get("path", [])
            node_name = node["name"]
            if board not in board_folders:
                continue

            existing = (
                db.query(KnowledgeFolder)
                .filter(
                    KnowledgeFolder.is_system == 1,
                    KnowledgeFolder.taxonomy_code == code,
                    KnowledgeFolder.business_unit == business_unit,
                )
                .first()
            )
            key = f"{business_unit}:{code}"
            if existing:
                code_to_folder[key] = existing.id
                continue

            parent_id = board_folders[board].id
            for part in path_parts[1:-1]:
                mid = (
                    db.query(KnowledgeFolder)
                    .filter(
                        KnowledgeFolder.is_system == 1,
                        KnowledgeFolder.name == part,
                        KnowledgeFolder.parent_id == parent_id,
                        KnowledgeFolder.business_unit == business_unit,
                    )
                    .first()
                )
                if not mid:
                    mid = KnowledgeFolder(
                        name=part,
                        parent_id=parent_id,
                        created_by=owner_id,
                        is_system=1,
                        taxonomy_board=board,
                        business_unit=business_unit,
                    )
                    db.add(mid)
                    db.flush()
                parent_id = mid.id

            leaf = KnowledgeFolder(
                name=node_name,
                parent_id=parent_id,
                created_by=owner_id,
                is_system=1,
                taxonomy_board=board,
                taxonomy_code=code,
                business_unit=business_unit,
            )
            db.add(leaf)
            db.flush()
            code_to_folder[key] = leaf.id

    db.commit()
    logger.info(f"System folder tree ensured: {len(code_to_folder)} leaf nodes")
    return code_to_folder


def get_system_folder_for_taxonomy(db: Session, taxonomy_code: str, business_unit: str | None = None) -> Optional[int]:
    """根据分类编码获取系统归档目录 ID。"""
    q = db.query(KnowledgeFolder).filter(
        KnowledgeFolder.is_system == 1,
        KnowledgeFolder.taxonomy_code == taxonomy_code,
    )
    if business_unit:
        q = q.filter(KnowledgeFolder.business_unit == business_unit)
    f = q.first()
    return f.id if f else None


def get_system_folder_for_board(db: Session, board: str, business_unit: str | None = None) -> Optional[int]:
    """根据 board 获取板块级系统目录 ID（用于低置信度归档到板块根目录）。"""
    q = db.query(KnowledgeFolder).filter(
        KnowledgeFolder.is_system == 1,
        KnowledgeFolder.taxonomy_board == board,
    )
    if business_unit:
        q = q.filter(KnowledgeFolder.business_unit == business_unit)
    f = q.first()
    return f.id if f else None


def ensure_governance_folders(db: Session, owner_id: int = 1) -> dict[str, int]:
    """从治理目标树生成目录（is_system=2）。

    层级：Objective → DepartmentMission → KR → RequiredElement → ResourceLibrary
    幂等操作：已存在的不重建。
    """
    from app.models.knowledge_governance import (
        GovernanceDepartmentMission,
        GovernanceKR,
        GovernanceObjective,
        GovernanceRequiredElement,
        GovernanceResourceLibrary,
    )

    code_to_folder: dict[str, int] = {}

    def _ensure_folder(
        name: str,
        parent_id: int | None,
        tag: str,
        business_unit: str | None = None,
    ) -> int:
        """幂等创建一个治理目录节点，用 name+parent_id+is_system=2 去重。"""
        existing = (
            db.query(KnowledgeFolder)
            .filter(
                KnowledgeFolder.is_system == 2,
                KnowledgeFolder.name == name,
                KnowledgeFolder.parent_id == parent_id if parent_id else KnowledgeFolder.parent_id.is_(None),
            )
            .first()
        )
        if existing:
            return existing.id
        folder = KnowledgeFolder(
            name=name,
            parent_id=parent_id,
            created_by=owner_id,
            is_system=2,
            business_unit=business_unit,
            taxonomy_code=tag,
        )
        db.add(folder)
        db.flush()
        return folder.id

    # 顶层治理目录根节点
    root_id = _ensure_folder("治理目录", None, "governance_root")

    # L0: GovernanceObjective（公司层）
    objectives = (
        db.query(GovernanceObjective)
        .filter(GovernanceObjective.parent_id.is_(None), GovernanceObjective.is_active == True)
        .order_by(GovernanceObjective.sort_order)
        .all()
    )
    for obj in objectives:
        obj_folder_id = _ensure_folder(obj.name, root_id, f"obj:{obj.code}")
        code_to_folder[f"objective:{obj.code}"] = obj_folder_id

        # L1: DepartmentMission
        missions = (
            db.query(GovernanceDepartmentMission)
            .filter(GovernanceDepartmentMission.objective_id == obj.id)
            .order_by(GovernanceDepartmentMission.id)
            .all()
        )
        for mission in missions:
            mission_folder_id = _ensure_folder(mission.name, obj_folder_id, f"mission:{mission.code}")
            code_to_folder[f"mission:{mission.code}"] = mission_folder_id

            # L2: KR
            krs = (
                db.query(GovernanceKR)
                .filter(GovernanceKR.mission_id == mission.id)
                .order_by(GovernanceKR.sort_order)
                .all()
            )
            for kr in krs:
                kr_folder_id = _ensure_folder(kr.name, mission_folder_id, f"kr:{kr.code}")
                code_to_folder[f"kr:{kr.code}"] = kr_folder_id

                # L3: RequiredElement
                elements = (
                    db.query(GovernanceRequiredElement)
                    .filter(GovernanceRequiredElement.kr_id == kr.id)
                    .order_by(GovernanceRequiredElement.sort_order)
                    .all()
                )
                for element in elements:
                    element_folder_id = _ensure_folder(element.name, kr_folder_id, f"element:{element.code}")
                    code_to_folder[f"element:{element.code}"] = element_folder_id

                    # L4: ResourceLibrary（叶子）
                    for lib_code in (element.required_library_codes or []):
                        lib = (
                            db.query(GovernanceResourceLibrary)
                            .filter(GovernanceResourceLibrary.code == lib_code)
                            .first()
                        )
                        if lib:
                            lib_folder_id = _ensure_folder(lib.name, element_folder_id, f"library:{lib.code}")
                            code_to_folder[f"library:{lib.code}"] = lib_folder_id

        # 直属资源库（不经过 mission/kr 路径的）
        direct_libraries = (
            db.query(GovernanceResourceLibrary)
            .filter(GovernanceResourceLibrary.objective_id == obj.id)
            .order_by(GovernanceResourceLibrary.id)
            .all()
        )
        for lib in direct_libraries:
            lib_key = f"library:{lib.code}"
            if lib_key not in code_to_folder:
                lib_folder_id = _ensure_folder(lib.name, obj_folder_id, f"library:{lib.code}")
                code_to_folder[lib_key] = lib_folder_id

    db.commit()
    logger.info(f"Governance folder tree ensured: {len(code_to_folder)} nodes")
    return code_to_folder
