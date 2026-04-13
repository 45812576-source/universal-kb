"""OpenCode 工作区巡检脚本 — 检测注册表/目录/会话的一致性问题并可选修复。

用法:
    python scripts/opencode_audit.py              # 只读巡检
    python scripts/opencode_audit.py --dry-run    # 同上
    python scripts/opencode_audit.py --fix        # 自动修复高置信度场景
"""
import argparse
import json
import logging
import os
import sys

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import SessionLocal
from app.models.opencode import StudioRegistration, OpenCodeWorkspaceMapping
from app.models.conversation import Conversation
from app.models.workspace import Workspace
from app.models.user import User
from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger("opencode_audit")


def _studio_root() -> str:
    return os.path.abspath(os.path.expanduser(
        getattr(settings, "STUDIO_WORKSPACE_ROOT", "~/studio_workspaces")
    ))


def audit(db, fix: bool = False) -> dict:
    """执行全量巡检，返回 {anomalies, auto_fixed, manual_review}。"""
    anomalies: list[dict] = []
    auto_fixed: list[dict] = []
    manual_review: list[dict] = []

    studio_root = _studio_root()
    oc_ws = db.query(Workspace).filter(Workspace.workspace_type == "opencode").first()
    sk_ws = db.query(Workspace).filter(Workspace.workspace_type == "skill_studio").first()

    # 1. 同一用户多个 opencode registration
    from sqlalchemy import func
    dup_rows = (
        db.query(StudioRegistration.user_id, func.count(StudioRegistration.id).label("cnt"))
        .filter(StudioRegistration.workspace_type == "opencode")
        .group_by(StudioRegistration.user_id)
        .having(func.count(StudioRegistration.id) > 1)
        .all()
    )
    for row in dup_rows:
        anomalies.append({
            "type": "duplicate_registration",
            "user_id": row.user_id,
            "count": row.cnt,
            "severity": "high",
        })
        manual_review.append({
            "type": "duplicate_registration",
            "user_id": row.user_id,
            "action": "手动检查并删除多余注册记录",
        })

    # 2. registration 指向不存在的目录
    all_regs = db.query(StudioRegistration).all()
    for reg in all_regs:
        ws_root_path = os.path.join(studio_root, reg.workspace_root) if not os.path.isabs(reg.workspace_root) else reg.workspace_root
        proj_dir_path = os.path.join(studio_root, reg.project_dir) if not os.path.isabs(reg.project_dir) else reg.project_dir

        if not os.path.isdir(ws_root_path):
            anomalies.append({
                "type": "missing_workspace_root",
                "user_id": reg.user_id,
                "workspace_type": reg.workspace_type,
                "path": ws_root_path,
            })
            if fix:
                from app.services.workdir_manager import ensure_workspace_layout
                try:
                    ensure_workspace_layout(reg.workspace_root, display_name="")
                    auto_fixed.append({
                        "type": "rebuilt_workspace_root",
                        "user_id": reg.user_id,
                        "path": ws_root_path,
                    })
                    logger.info(f"已重建 workspace_root: user={reg.user_id} path={ws_root_path}")
                except Exception as e:
                    manual_review.append({
                        "type": "rebuild_failed",
                        "user_id": reg.user_id,
                        "path": ws_root_path,
                        "error": str(e),
                    })

        if not os.path.isdir(proj_dir_path):
            anomalies.append({
                "type": "missing_project_dir",
                "user_id": reg.user_id,
                "workspace_type": reg.workspace_type,
                "path": proj_dir_path,
            })

    # 3. conversation.workspace_id 不匹配 opencode workspace
    if oc_ws:
        for reg in all_regs:
            if reg.workspace_type != "opencode" or not reg.primary_conversation_id:
                continue
            conv = db.get(Conversation, reg.primary_conversation_id)
            if conv and conv.workspace_id != oc_ws.id:
                anomalies.append({
                    "type": "conversation_workspace_mismatch",
                    "user_id": reg.user_id,
                    "conversation_id": conv.id,
                    "expected_workspace_id": oc_ws.id,
                    "actual_workspace_id": conv.workspace_id,
                })
                if fix:
                    conv.workspace_id = oc_ws.id
                    auto_fixed.append({
                        "type": "fixed_conversation_workspace",
                        "conversation_id": conv.id,
                        "user_id": reg.user_id,
                    })

    # 4. project_dir 被 Skill Studio 共用（opencode 和 skill_studio 的 project_dir 相同）
    oc_regs = [r for r in all_regs if r.workspace_type == "opencode"]
    sk_regs = [r for r in all_regs if r.workspace_type == "skill_studio"]
    sk_dirs = {r.project_dir for r in sk_regs}
    for reg in oc_regs:
        if reg.project_dir in sk_dirs:
            anomalies.append({
                "type": "shared_project_dir",
                "user_id": reg.user_id,
                "project_dir": reg.project_dir,
                "severity": "high",
            })
            manual_review.append({
                "type": "shared_project_dir",
                "user_id": reg.user_id,
                "action": "skill_studio 应使用独立 skill_studio/ 子目录",
            })

    # 5. orphan conversation（用户 A 的 conversation 指向用户 B 的 workspace）
    if oc_ws:
        oc_convs = (
            db.query(Conversation)
            .filter(
                Conversation.workspace_id == oc_ws.id,
                Conversation.is_active == True,
            )
            .all()
        )
        reg_user_ids = {r.user_id for r in oc_regs}
        for conv in oc_convs:
            if conv.user_id not in reg_user_ids:
                anomalies.append({
                    "type": "orphan_conversation",
                    "conversation_id": conv.id,
                    "user_id": conv.user_id,
                    "reason": "用户没有 opencode registration",
                })
                if fix:
                    # 尝试补建 registration
                    from app.services.workdir_manager import _workspace_root_for_user, _workspace_project_dir
                    user = db.get(User, conv.user_id)
                    if user:
                        ws_root = _workspace_root_for_user(user.id, user.display_name or "")
                        proj_dir = _workspace_project_dir(ws_root)
                        new_reg = StudioRegistration(
                            user_id=user.id,
                            workspace_type="opencode",
                            workspace_root=ws_root,
                            project_dir=proj_dir,
                            primary_conversation_id=conv.id,
                            runtime_status="stopped",
                            generation=0,
                        )
                        db.add(new_reg)
                        auto_fixed.append({
                            "type": "created_missing_registration",
                            "user_id": user.id,
                            "conversation_id": conv.id,
                        })
                        logger.info(f"已补建 registration: user={user.id}")

    # 6. 旧 display_name 目录未迁移
    if os.path.isdir(studio_root):
        for dirname in os.listdir(studio_root):
            dirpath = os.path.join(studio_root, dirname)
            if not os.path.isdir(dirpath):
                continue
            if dirname.startswith("user_"):
                continue  # 新格式，跳过
            # 旧 display_name 目录
            anomalies.append({
                "type": "legacy_displayname_dir",
                "dirname": dirname,
                "path": dirpath,
            })
            if fix:
                # 尝试找到对应用户并触发迁移
                user = db.query(User).filter(User.display_name == dirname).first()
                if user:
                    from app.services.workdir_manager import _workspace_root_for_user
                    new_root = _workspace_root_for_user(user.id, user.display_name)
                    if os.path.isdir(os.path.join(studio_root, f"user_{user.id}")):
                        manual_review.append({
                            "type": "legacy_dir_conflict",
                            "dirname": dirname,
                            "user_id": user.id,
                            "action": "新旧目录都存在，需手动合并",
                        })
                    else:
                        auto_fixed.append({
                            "type": "migrated_legacy_dir",
                            "dirname": dirname,
                            "user_id": user.id,
                            "new_root": new_root,
                        })
                        logger.info(f"已迁移旧目录: {dirname} → user_{user.id}")
                else:
                    manual_review.append({
                        "type": "legacy_dir_no_user",
                        "dirname": dirname,
                        "action": "找不到匹配用户，需手动检查",
                    })

    # 7. Skill Studio 旧共享会话迁移（原在前端自动触发，已移至此处）
    if fix:
        from app.services.studio_registry import migrate_skill_conversations
        all_users = db.query(User).filter(User.is_active == True).all()
        total_migrated = 0
        for u in all_users:
            try:
                result = migrate_skill_conversations(db, u)
                if result["migrated"] > 0:
                    total_migrated += result["migrated"]
                    auto_fixed.append({
                        "type": "skill_conversation_migration",
                        "user_id": u.id,
                        "migrated": result["migrated"],
                        "skills": result["skills"],
                    })
            except Exception as e:
                manual_review.append({
                    "type": "skill_migration_failed",
                    "user_id": u.id,
                    "error": str(e),
                })
        if total_migrated > 0:
            logger.info(f"Skill Studio 会话迁移: 共迁移 {total_migrated} 条消息")

    if fix:
        db.commit()

    return {
        "anomalies": anomalies,
        "auto_fixed": auto_fixed,
        "manual_review": manual_review,
    }


def main():
    parser = argparse.ArgumentParser(description="OpenCode 工作区巡检")
    parser.add_argument("--fix", action="store_true", help="自动修复高置信度场景")
    parser.add_argument("--dry-run", action="store_true", help="只读巡检（默认）")
    args = parser.parse_args()

    do_fix = args.fix and not args.dry_run

    db = SessionLocal()
    try:
        report = audit(db, fix=do_fix)
    finally:
        db.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))

    total = len(report["anomalies"])
    fixed = len(report["auto_fixed"])
    review = len(report["manual_review"])
    logger.info(f"巡检完成: 发现 {total} 个异常, 自动修复 {fixed}, 需人工审查 {review}")


if __name__ == "__main__":
    main()
