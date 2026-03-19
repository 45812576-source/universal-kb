"""Contribution statistics API."""
import os
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.conversation import Message, MessageRole
from app.models.knowledge import KnowledgeEntry
from app.models.opencode import OpenCodeWorkspaceMapping
from app.models.skill import SkillAttribution, SkillSuggestion, SuggestionStatus, AttributionLevel
from app.models.user import Department, Role, User

router = APIRouter(prefix="/api/contributions", tags=["contributions"])


@router.get("/stats")
def contribution_stats(
    department_id: int = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Per-user contribution stats: submission count, adoption rate, influence score, skill count."""
    # Get all users (filtered by dept if requested)
    user_q = db.query(User)
    if department_id:
        user_q = user_q.filter(User.department_id == department_id)
    users = user_q.all()
    user_ids = [u.id for u in users]

    if not user_ids:
        return []

    # Suggestion counts per user
    suggestion_rows = (
        db.query(
            SkillSuggestion.submitted_by,
            func.count(SkillSuggestion.id).label("total"),
        )
        .filter(SkillSuggestion.submitted_by.in_(user_ids))
        .group_by(SkillSuggestion.submitted_by)
        .all()
    )
    suggestion_map = {r.submitted_by: r.total for r in suggestion_rows}

    # Adopted/partial counts per user
    adopted_rows = (
        db.query(
            SkillSuggestion.submitted_by,
            func.count(SkillSuggestion.id).label("adopted"),
        )
        .filter(
            SkillSuggestion.submitted_by.in_(user_ids),
            SkillSuggestion.status.in_([SuggestionStatus.ADOPTED, SuggestionStatus.PARTIAL]),
        )
        .group_by(SkillSuggestion.submitted_by)
        .all()
    )
    adopted_map = {r.submitted_by: r.adopted for r in adopted_rows}

    # Attribution influence scores (full×3 + partial×1)
    # Join suggestion → attribution to get per-user attribution
    attr_rows = (
        db.query(
            SkillSuggestion.submitted_by,
            SkillAttribution.attribution_level,
            func.count(SkillAttribution.id).label("cnt"),
            func.count(func.distinct(SkillAttribution.skill_id)).label("skill_count"),
        )
        .join(SkillAttribution, SkillAttribution.suggestion_id == SkillSuggestion.id)
        .filter(SkillSuggestion.submitted_by.in_(user_ids))
        .group_by(SkillSuggestion.submitted_by, SkillAttribution.attribution_level)
        .all()
    )

    score_map: dict[int, int] = {}
    skill_count_map: dict[int, set] = {}
    for r in attr_rows:
        uid = r.submitted_by
        if r.attribution_level == AttributionLevel.FULL:
            score_map[uid] = score_map.get(uid, 0) + r.cnt * 3
        elif r.attribution_level == AttributionLevel.PARTIAL:
            score_map[uid] = score_map.get(uid, 0) + r.cnt * 1
        if uid not in skill_count_map:
            skill_count_map[uid] = set()

    # Get distinct skill counts per user
    skill_rows = (
        db.query(
            SkillSuggestion.submitted_by,
            func.count(func.distinct(SkillAttribution.skill_id)).label("skill_count"),
        )
        .join(SkillAttribution, SkillAttribution.suggestion_id == SkillSuggestion.id)
        .filter(SkillSuggestion.submitted_by.in_(user_ids))
        .filter(SkillAttribution.attribution_level != AttributionLevel.NONE)
        .group_by(SkillSuggestion.submitted_by)
        .all()
    )
    skill_count_final = {r.submitted_by: r.skill_count for r in skill_rows}

    result = []
    for u in users:
        total = suggestion_map.get(u.id, 0)
        adopted = adopted_map.get(u.id, 0)
        score = score_map.get(u.id, 0)
        skills = skill_count_final.get(u.id, 0)
        result.append({
            "user_id": u.id,
            "display_name": u.display_name,
            "department_id": u.department_id,
            "total_suggestions": total,
            "adopted_count": adopted,
            "adoption_rate": round(adopted / total, 2) if total > 0 else 0.0,
            "influence_score": score,
            "impacted_skills": skills,
        })

    # Sort by influence score desc
    result.sort(key=lambda x: (-x["influence_score"], -x["total_suggestions"]))
    return result


@router.get("/kb-stats")
def kb_contribution_stats(
    department_id: int = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Per-user knowledge base contribution stats: entry count, token usage, model distribution."""
    user_q = db.query(User)
    if department_id:
        user_q = user_q.filter(User.department_id == department_id)
    users = user_q.all()
    user_ids = [u.id for u in users]

    if not user_ids:
        return []

    # Knowledge entry counts per user (all statuses)
    entry_rows = (
        db.query(KnowledgeEntry.created_by, func.count(KnowledgeEntry.id).label("total"))
        .filter(KnowledgeEntry.created_by.in_(user_ids))
        .group_by(KnowledgeEntry.created_by)
        .all()
    )
    entry_map = {r.created_by: r.total for r in entry_rows}

    # Approved entry counts
    approved_rows = (
        db.query(KnowledgeEntry.created_by, func.count(KnowledgeEntry.id).label("approved"))
        .filter(
            KnowledgeEntry.created_by.in_(user_ids),
            KnowledgeEntry.status == "approved",
        )
        .group_by(KnowledgeEntry.created_by)
        .all()
    )
    approved_map = {r.created_by: r.approved for r in approved_rows}

    # Token usage from assistant messages (metadata JSON fields)
    # Aggregate input_tokens, output_tokens per user via conversation → message
    from app.models.conversation import Conversation
    from sqlalchemy.dialects.mysql import JSON as MySQLJSON
    import json as _json

    # Fetch all assistant messages for users' conversations
    conv_rows = (
        db.query(Conversation.id, Conversation.user_id)
        .filter(Conversation.user_id.in_(user_ids))
        .all()
    )
    conv_to_user = {r.id: r.user_id for r in conv_rows}
    conv_ids = list(conv_to_user.keys())

    token_map: dict[int, dict] = {}  # user_id → {input, output, models}
    if conv_ids:
        msg_rows = (
            db.query(Message)
            .filter(
                Message.conversation_id.in_(conv_ids),
                Message.role == MessageRole.ASSISTANT,
            )
            .all()
        )
        for msg in msg_rows:
            uid = conv_to_user.get(msg.conversation_id)
            if uid is None:
                continue
            meta = msg.metadata_ or {}
            inp = meta.get("input_tokens") or 0
            out = meta.get("output_tokens") or 0
            model = meta.get("model_id") or ""
            if uid not in token_map:
                token_map[uid] = {"input": 0, "output": 0, "models": {}}
            token_map[uid]["input"] += inp
            token_map[uid]["output"] += out
            if model:
                token_map[uid]["models"][model] = token_map[uid]["models"].get(model, 0) + 1

    result = []
    for u in users:
        total_entries = entry_map.get(u.id, 0)
        approved = approved_map.get(u.id, 0)
        tok = token_map.get(u.id, {"input": 0, "output": 0, "models": {}})
        # top model by usage count
        models_dict = tok["models"]
        top_model = max(models_dict, key=lambda k: models_dict[k]) if models_dict else None
        result.append({
            "user_id": u.id,
            "display_name": u.display_name,
            "department_id": u.department_id,
            "total_entries": total_entries,
            "approved_entries": approved,
            "input_tokens": tok["input"],
            "output_tokens": tok["output"],
            "models": models_dict,
            "top_model": top_model,
        })

    result.sort(key=lambda x: (-x["total_entries"], -x["input_tokens"]))
    return result


@router.get("/leaderboard")
def leaderboard(
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Top contributors leaderboard — visible to all logged-in users."""
    all_stats = contribution_stats.__wrapped__(department_id=None, db=db, _user=_user) \
        if hasattr(contribution_stats, "__wrapped__") else []

    # Simpler direct query for leaderboard
    attr_rows = (
        db.query(
            SkillSuggestion.submitted_by,
            func.count(SkillAttribution.id).label("full_cnt"),
        )
        .join(SkillAttribution, SkillAttribution.suggestion_id == SkillSuggestion.id)
        .filter(SkillAttribution.attribution_level == AttributionLevel.FULL)
        .group_by(SkillSuggestion.submitted_by)
        .all()
    )
    partial_rows = (
        db.query(
            SkillSuggestion.submitted_by,
            func.count(SkillAttribution.id).label("partial_cnt"),
        )
        .join(SkillAttribution, SkillAttribution.suggestion_id == SkillSuggestion.id)
        .filter(SkillAttribution.attribution_level == AttributionLevel.PARTIAL)
        .group_by(SkillSuggestion.submitted_by)
        .all()
    )
    full_map = {r.submitted_by: r.full_cnt for r in attr_rows}
    partial_map = {r.submitted_by: r.partial_cnt for r in partial_rows}
    user_ids = set(full_map.keys()) | set(partial_map.keys())

    if not user_ids:
        return []

    users = db.query(User).filter(User.id.in_(user_ids)).all()
    user_map = {u.id: u for u in users}
    dept_map = {d.id: d.name for d in db.query(Department).all()}

    entries = []
    for uid in user_ids:
        score = full_map.get(uid, 0) * 3 + partial_map.get(uid, 0)
        u = user_map.get(uid)
        if not u:
            continue
        entries.append({
            "user_id": uid,
            "display_name": u.display_name,
            "department": dept_map.get(u.department_id, "") if u.department_id else "",
            "influence_score": score,
        })

    entries.sort(key=lambda x: -x["influence_score"])
    return entries[:limit]


# ─── OpenCode 用量 ─────────────────────────────────────────────────────────────

OPENCODE_DB_PATH = os.environ.get(
    "OPENCODE_DB_PATH",
    os.path.expanduser("~/.local/share/opencode/opencode.db"),
)


def _read_opencode_db() -> dict[str, dict]:
    """从 OpenCode SQLite 读取按 directory 聚合的用量数据。
    返回 {directory: {sessions, input_tokens, output_tokens, models,
                      files_changed, lines_added, lines_deleted, output_files}}
    output_files: [{path, session_title}, ...] 去重后列表
    """
    if not os.path.exists(OPENCODE_DB_PATH):
        return {}

    import json as _json

    con = sqlite3.connect(f"file:{OPENCODE_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        # session 级别：代码产出 + 标题（用 directory 分组）
        session_rows = con.execute(
            "SELECT id, directory, title, summary_files, summary_additions, summary_deletions FROM session"
        ).fetchall()

        # message 级别：token 用量（assistant 角色，无 error）
        msg_rows = con.execute(
            "SELECT s.directory, m.data "
            "FROM message m "
            "JOIN session s ON s.id = m.session_id "
            "WHERE json_extract(m.data, '$.role') = 'assistant' "
            "  AND json_extract(m.data, '$.error') IS NULL"
        ).fetchall()

        # part 级别：write/edit 工具调用 → 提取 filePath
        part_rows = con.execute(
            "SELECT p.data, s.directory, s.title "
            "FROM part p "
            "JOIN session s ON s.id = p.session_id "
            "WHERE json_extract(p.data, '$.type') = 'tool' "
            "  AND json_extract(p.data, '$.tool') IN ('write', 'edit', 'patch')"
        ).fetchall()
    finally:
        con.close()

    result: dict[str, dict] = {}

    def _ws(directory: str) -> dict:
        if directory not in result:
            result[directory] = {
                "sessions": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "models": {},
                "files_changed": 0,
                "lines_added": 0,
                "lines_deleted": 0,
                "_file_set": set(),
                "output_files": [],
            }
        return result[directory]

    for row in session_rows:
        directory = row["directory"] or "__unknown__"
        ws = _ws(directory)
        ws["sessions"] += 1
        ws["files_changed"] += row["summary_files"] or 0
        ws["lines_added"] += row["summary_additions"] or 0
        ws["lines_deleted"] += row["summary_deletions"] or 0

    for row in msg_rows:
        directory = row["directory"] or "__unknown__"
        try:
            data = _json.loads(row["data"])
        except Exception:
            continue
        tokens = data.get("tokens") or {}
        cache = tokens.get("cache") or {}
        inp = tokens.get("input") or 0
        out = tokens.get("output") or 0
        cr = cache.get("read") or 0
        model = data.get("modelID") or ""
        ws = _ws(directory)
        ws["input_tokens"] += inp
        ws["output_tokens"] += out
        ws["cache_read_tokens"] += cr
        if model:
            ws["models"][model] = ws["models"].get(model, 0) + 1

    for row in part_rows:
        directory = row["directory"] or "__unknown__"
        try:
            data = _json.loads(row["data"])
        except Exception:
            continue
        state = data.get("state") or {}
        inp = state.get("input") or {}
        file_path = inp.get("filePath") or inp.get("file_path") or ""
        if not file_path:
            continue
        ws = _ws(directory)
        if file_path not in ws["_file_set"]:
            ws["_file_set"].add(file_path)
            ws["output_files"].append({
                "path": file_path,
                "session_title": row["title"] or "",
            })

    for ws in result.values():
        del ws["_file_set"]

    return result


# ─── Mapping CRUD ─────────────────────────────────────────────────────────────

class MappingCreate(BaseModel):
    opencode_workspace_id: str
    opencode_workspace_name: Optional[str] = None
    user_id: int


class MappingUpdate(BaseModel):
    opencode_workspace_name: Optional[str] = None
    user_id: Optional[int] = None


@router.get("/opencode-workspaces")
def list_opencode_workspaces(
    _user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """列出 OpenCode SQLite 中所有 workspace（供超管配置映射用）。"""
    if not os.path.exists(OPENCODE_DB_PATH):
        return []
    con = sqlite3.connect(f"file:{OPENCODE_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, worktree, name, icon_color, time_created FROM project ORDER BY time_created DESC"
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


@router.get("/opencode-mappings")
def list_mappings(
    db: Session = Depends(get_db),
    _user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    mappings = db.query(OpenCodeWorkspaceMapping).all()
    return [
        {
            "id": m.id,
            "opencode_workspace_id": m.opencode_workspace_id,
            "opencode_workspace_name": m.opencode_workspace_name,
            "user_id": m.user_id,
            "display_name": m.user.display_name if m.user else None,
        }
        for m in mappings
    ]


@router.post("/opencode-mappings")
def create_mapping(
    req: MappingCreate,
    db: Session = Depends(get_db),
    _user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    existing = db.query(OpenCodeWorkspaceMapping).filter(
        OpenCodeWorkspaceMapping.opencode_workspace_id == req.opencode_workspace_id
    ).first()
    if existing:
        raise HTTPException(400, "该 workspace 已有映射，请先删除再重建")
    mapping = OpenCodeWorkspaceMapping(
        opencode_workspace_id=req.opencode_workspace_id,
        opencode_workspace_name=req.opencode_workspace_name,
        user_id=req.user_id,
    )
    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return {"id": mapping.id, "ok": True}


@router.put("/opencode-mappings/{mapping_id}")
def update_mapping(
    mapping_id: int,
    req: MappingUpdate,
    db: Session = Depends(get_db),
    _user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    mapping = db.get(OpenCodeWorkspaceMapping, mapping_id)
    if not mapping:
        raise HTTPException(404, "映射不存在")
    if req.opencode_workspace_name is not None:
        mapping.opencode_workspace_name = req.opencode_workspace_name
    if req.user_id is not None:
        mapping.user_id = req.user_id
    db.commit()
    return {"ok": True}


@router.delete("/opencode-mappings/{mapping_id}")
def delete_mapping(
    mapping_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    mapping = db.get(OpenCodeWorkspaceMapping, mapping_id)
    if not mapping:
        raise HTTPException(404, "映射不存在")
    db.delete(mapping)
    db.commit()
    return {"ok": True}


# ─── OpenCode 用量统计 ─────────────────────────────────────────────────────────

def compute_and_store_opencode_usage(db: Session) -> None:
    """读取 OpenCode SQLite，按用户聚合后写入缓存表。由定时任务和手动触发调用。"""
    import datetime as dt
    from app.models.opencode import OpenCodeUsageCache
    from app.models.skill import Skill, SkillStatus
    from app.models.tool import ToolRegistry

    # 取有 directory 的映射（自动方案）；无 directory 的旧映射忽略
    mappings = db.query(OpenCodeWorkspaceMapping).filter(
        OpenCodeWorkspaceMapping.directory != None
    ).all()
    ws_data = _read_opencode_db()  # key = session.directory

    # 统计每个用户从 dev-studio 提交的 skill/tool 数量
    # dev-studio 保存的 skill: source_type='local', created_by=user.id
    # dev-studio 保存的 tool: created_by=user.id
    skill_counts: dict[int, int] = {}
    for row in db.query(Skill.created_by, func.count(Skill.id)).filter(
        Skill.source_type == "local"
    ).group_by(Skill.created_by).all():
        if row[0]:
            skill_counts[row[0]] = row[1]

    tool_counts: dict[int, int] = {}
    for row in db.query(ToolRegistry.created_by, func.count(ToolRegistry.id)).filter(
        ToolRegistry.created_by.isnot(None)
    ).group_by(ToolRegistry.created_by).all():
        if row[0]:
            tool_counts[row[0]] = row[1]

    user_stats: dict[int, dict] = {}
    for m in mappings:
        uid = m.user_id
        # 匹配所有 directory 以 m.directory 开头的 session（精确匹配或子目录）
        if uid not in user_stats:
            user_stats[uid] = {
                "sessions": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "files_changed": 0,
                "lines_added": 0,
                "lines_deleted": 0,
                "models": {},
                "workspaces": [],
                "output_files": [],
                "_file_paths": set(),
            }
        s = user_stats[uid]
        for dir_key, ws in ws_data.items():
            if dir_key == m.directory or dir_key.startswith(m.directory + "/") or dir_key.startswith(m.directory + os.sep):
                s["sessions"] += ws.get("sessions", 0)
                s["input_tokens"] += ws.get("input_tokens", 0)
                s["output_tokens"] += ws.get("output_tokens", 0)
                s["cache_read_tokens"] += ws.get("cache_read_tokens", 0)
                s["files_changed"] += ws.get("files_changed", 0)
                s["lines_added"] += ws.get("lines_added", 0)
                s["lines_deleted"] += ws.get("lines_deleted", 0)
                for model, cnt in ws.get("models", {}).items():
                    s["models"][model] = s["models"].get(model, 0) + cnt
                for f in ws.get("output_files", []):
                    if f["path"] not in s["_file_paths"]:
                        s["_file_paths"].add(f["path"])
                        s["output_files"].append(f)
        s["workspaces"].append(m.opencode_workspace_name or m.directory or str(uid))

    now = dt.datetime.utcnow()
    for uid, s in user_stats.items():
        del s["_file_paths"]
        row = db.query(OpenCodeUsageCache).filter(OpenCodeUsageCache.user_id == uid).first()
        if row is None:
            row = OpenCodeUsageCache(user_id=uid)
            db.add(row)
        row.sessions = s["sessions"]
        row.input_tokens = s["input_tokens"]
        row.output_tokens = s["output_tokens"]
        row.cache_read_tokens = s["cache_read_tokens"]
        row.files_changed = s["files_changed"]
        row.lines_added = s["lines_added"]
        row.lines_deleted = s["lines_deleted"]
        row.models = s["models"]
        row.workspaces = s["workspaces"]
        row.output_files = s["output_files"]
        row.skills_submitted = skill_counts.get(uid, 0)
        row.tools_submitted = tool_counts.get(uid, 0)
        row.computed_at = now

    db.commit()


@router.get("/opencode-usage")
def opencode_usage(
    db: Session = Depends(get_db),
    _user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """读取缓存的 OpenCode 用量统计（每 12 小时更新一次）。"""
    from app.models.opencode import OpenCodeUsageCache

    rows = db.query(OpenCodeUsageCache).all()
    result = []
    for row in rows:
        models = row.models or {}
        top_model = max(models, key=lambda k: models[k]) if models else None
        result.append({
            "user_id": row.user_id,
            "display_name": row.user.display_name if row.user else str(row.user_id),
            "sessions": row.sessions,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "cache_read_tokens": row.cache_read_tokens,
            "models": models,
            "top_model": top_model,
            "files_changed": row.files_changed,
            "lines_added": row.lines_added,
            "lines_deleted": row.lines_deleted,
            "output_files": row.output_files or [],
            "skills_submitted": row.skills_submitted or 0,
            "tools_submitted": row.tools_submitted or 0,
            "workspaces": row.workspaces or [],
            "computed_at": row.computed_at.isoformat() if row.computed_at else None,
        })

    result.sort(key=lambda x: -(x["input_tokens"] + x["output_tokens"]))
    return result


@router.post("/opencode-usage/refresh")
def refresh_opencode_usage(
    db: Session = Depends(get_db),
    _user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """手动触发重新计算 OpenCode 用量缓存。"""
    compute_and_store_opencode_usage(db)
    return {"ok": True}
