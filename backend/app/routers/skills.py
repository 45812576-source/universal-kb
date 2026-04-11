import logging
import re

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, Request, UploadFile

logger = logging.getLogger(__name__)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.user import User, Role
from app.models.skill import Skill, SkillVersion, SkillStatus
from app.services.llm_gateway import llm_gateway

router = APIRouter(prefix="/api/skills", tags=["skills"])


class SkillCreate(BaseModel):
    name: str
    description: str = ""
    mode: str = "hybrid"
    department_id: Optional[int] = None
    knowledge_tags: list[str] = []
    auto_inject: bool = True
    system_prompt: str
    variables: list[str] = []
    required_inputs: list[dict] = []
    model_config_id: Optional[int] = None
    output_schema: Optional[dict] = None


class SkillVersionCreate(BaseModel):
    system_prompt: str
    variables: list[str] = []
    required_inputs: list[dict] = []
    model_config_id: Optional[int] = None
    change_note: str = ""
    output_schema: Optional[dict] = None


def _skill_summary(s: Skill) -> dict:
    latest = s.versions[0] if s.versions else None
    return {
        "id": s.id,
        "name": s.name,
        "description": s.description,
        "mode": s.mode.value,
        "status": s.status.value,
        "knowledge_tags": s.knowledge_tags or [],
        "auto_inject": s.auto_inject,
        "current_version": latest.version if latest else 0,
        "department_id": s.department_id,
        "created_at": s.created_at.isoformat(),
        "scope": s.scope or "personal",
        "created_by": s.created_by,
        "source_type": s.source_type or "local",
        "source_files": s.source_files or [],
    }


class SaveFromMarketRequest(BaseModel):
    skill_id: int


def _sync_skill_to_workspace_config(db: Session, user: User, skill_id: int, source: str, *, add: bool = True):
    """同步 skill 到用户的 workspace config（add=True 追加，add=False 移除）。"""
    from app.models.workspace import UserWorkspaceConfig

    cfg = db.query(UserWorkspaceConfig).filter(UserWorkspaceConfig.user_id == user.id).first()
    if not cfg:
        if not add:
            return
        cfg = UserWorkspaceConfig(user_id=user.id, mounted_skills=[], mounted_tools=[], needs_prompt_refresh=True)
        db.add(cfg)
        db.flush()

    skills = list(cfg.mounted_skills or [])
    existing_ids = {item["skill_id"] for item in skills}

    if add and skill_id not in existing_ids:
        skills.append({"skill_id": skill_id, "source": source, "mounted": True})
        cfg.mounted_skills = skills
        cfg.needs_prompt_refresh = True
        db.commit()
    elif not add and skill_id in existing_ids:
        cfg.mounted_skills = [item for item in skills if item["skill_id"] != skill_id]
        cfg.needs_prompt_refresh = True
        db.commit()


@router.post("/save-from-market")
def save_from_market(
    req: SaveFromMarketRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """保存公司级 Skill 到个人收藏。"""
    from app.models.skill import UserSavedSkill
    from sqlalchemy.exc import IntegrityError

    skill = db.get(Skill, req.skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")

    saved = UserSavedSkill(user_id=user.id, skill_id=req.skill_id)
    db.add(saved)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # 已保存，忽略

    # 同步到 workspace config
    _sync_skill_to_workspace_config(db, user, req.skill_id, "market", add=True)
    return {"ok": True}


@router.delete("/save-from-market/{skill_id}")
def unsave_from_market(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """取消保存。"""
    from app.models.skill import UserSavedSkill

    row = (
        db.query(UserSavedSkill)
        .filter(UserSavedSkill.user_id == user.id, UserSavedSkill.skill_id == skill_id)
        .first()
    )
    if row:
        db.delete(row)
        db.commit()

    # 从 workspace config 移除
    _sync_skill_to_workspace_config(db, user, skill_id, "market", add=False)
    return {"ok": True}


@router.get("/my-saved")
def list_my_saved(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """返回当前用户保存的公司级 Skill 列表。"""
    from app.models.skill import UserSavedSkill
    import datetime as _dt

    rows = (
        db.query(UserSavedSkill)
        .filter(UserSavedSkill.user_id == user.id)
        .all()
    )

    result = []
    for row in rows:
        skill = db.get(Skill, row.skill_id)
        if not skill:
            continue
        summary = _skill_summary(skill)
        # has_update: skill.updated_at > saved_at
        has_update = False
        if skill.updated_at and row.saved_at:
            has_update = skill.updated_at > row.saved_at
        summary["has_update"] = has_update
        summary["saved_at"] = row.saved_at.isoformat() if row.saved_at else None
        result.append(summary)
    return result


@router.get("")
def list_skills(
    status: str = None,
    scope: str = None,
    mine: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from sqlalchemy import or_
    q = db.query(Skill)
    # 员工：看已发布的 + 自己创建的（含草稿）
    if user.role == Role.EMPLOYEE:
        q = q.filter(
            or_(
                Skill.status == SkillStatus.PUBLISHED,
                Skill.created_by == user.id,
            )
        )
    elif status:
        q = q.filter(Skill.status == status)

    if mine:
        q = q.filter(Skill.created_by == user.id)
    elif scope == "department":
        q = q.filter(Skill.department_id == user.department_id, Skill.status == SkillStatus.PUBLISHED)
    elif scope == "company":
        q = q.filter(Skill.scope == "company", Skill.status == SkillStatus.PUBLISHED)

    skills = q.order_by(Skill.updated_at.desc()).all()

    # 批量查询调用次数，避免 N+1
    skill_ids = [s.id for s in skills]
    usage_map: dict[int, int] = {}
    if skill_ids:
        from sqlalchemy import func as _func
        from app.models.skill import SkillExecutionLog
        rows = (
            db.query(SkillExecutionLog.skill_id, _func.count(SkillExecutionLog.id))
            .filter(SkillExecutionLog.skill_id.in_(skill_ids))
            .group_by(SkillExecutionLog.skill_id)
            .all()
        )
        usage_map = {sid: cnt for sid, cnt in rows}

    result = []
    for s in skills:
        summary = _skill_summary(s)
        summary["usage_count"] = usage_map.get(s.id, 0)
        result.append(summary)
    return result


MAX_EMPLOYEE_UNPUBLISHED_SKILLS = 3


@router.post("")
def create_skill(
    req: SkillCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # 员工/部门管理员：最多 3 个未发布的个人 Skill（DRAFT + REVIEWING，不含 ARCHIVED）
    if user.role in (Role.EMPLOYEE, Role.DEPT_ADMIN):
        unpublished = (
            db.query(Skill)
            .filter(
                Skill.created_by == user.id,
                Skill.status.in_([SkillStatus.DRAFT, SkillStatus.REVIEWING]),
            )
            .all()
        )
        if len(unpublished) >= MAX_EMPLOYEE_UNPUBLISHED_SKILLS:
            names = "、".join(s.name for s in unpublished[:5])
            raise HTTPException(
                400,
                f"最多只能有 {MAX_EMPLOYEE_UNPUBLISHED_SKILLS} 个未发布 Skill（当前有：{names}），请先发布或删除已有草稿",
            )

    if db.query(Skill).filter(Skill.name == req.name, Skill.created_by == user.id).first():
        raise HTTPException(400, f"你已有同名 Skill '{req.name}'，请修改名称或更新已有版本")

    # 员工创建的 Skill 固定为个人草稿，不可自行设置 scope
    skill = Skill(
        name=req.name,
        description=req.description,
        mode=req.mode,
        scope="personal" if user.role == Role.EMPLOYEE else (req.department_id and "department" or "company"),
        department_id=None if user.role == Role.EMPLOYEE else req.department_id,
        knowledge_tags=req.knowledge_tags,
        auto_inject=req.auto_inject,
        created_by=user.id,
        status=SkillStatus.DRAFT,
    )
    db.add(skill)
    db.flush()

    v = SkillVersion(
        skill_id=skill.id,
        version=1,
        system_prompt=req.system_prompt,
        variables=req.variables,
        required_inputs=req.required_inputs,
        model_config_id=req.model_config_id,
        output_schema=req.output_schema,
        created_by=user.id,
        change_note="初始版本",
    )
    db.add(v)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(400, f"已存在同名 Skill '{req.name}'，请修改名称")
    db.refresh(skill)
    return {"id": skill.id, "name": skill.name}


def _parse_skill_md(content: str) -> dict:
    """Parse a SKILL.md file: YAML frontmatter (name, description) + body as system_prompt.

    Also extracts {variable} placeholders from the body.
    """
    frontmatter = {}
    body = content

    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if fm_match:
        for line in fm_match.group(1).splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                frontmatter[key.strip()] = val.strip()
        body = content[fm_match.end():]

    name = frontmatter.get("name", "").strip()
    description = frontmatter.get("description", "").strip()

    # Extract {variable} placeholders (but not {{escaped}} or {code blocks})
    variables = sorted(set(re.findall(r"(?<!\{)\{(\w+)\}(?!\})", body)))

    return {
        "name": name,
        "description": description,
        "system_prompt": body.strip(),
        "variables": [f"{{{v}}}" for v in variables],
    }


# ─── 外部 Skill 导入转换 ────────────────────────────────────────────────────

# 前端相关文件扩展名（沙盒检测用）
FRONTEND_EXTENSIONS = {
    ".tsx", ".jsx", ".html", ".css", ".scss", ".less", ".sass",
    ".vue", ".svelte", ".styl", ".pcss",
}

# 前端语义关键词（用于 prompt 内容检测）
FRONTEND_KEYWORDS = [
    r"\bReact\b", r"\bVue\b", r"\bSvelte\b", r"\bAngular\b",
    r"\bHTML\b.*组件", r"生成.*页面", r"生成.*组件", r"生成.*界面",
    r"前端", r"\bCSS\b", r"\bDOM\b", r"\bJSX\b", r"\bTSX\b",
    r"render.*component", r"create.*component", r"build.*UI",
    r"web\s*page", r"landing\s*page", r"网页",
]

_IMPORT_CONVERT_PROMPT = """你是 Skill 格式转换专家。将以下外部 Skill 内容转换为内部统一格式。

我们的系统是一个 AI Skill 工作台，Skill 的核心是 system_prompt（给 AI 的指令），不支持前端渲染。
工具（tool）在我们系统中有独立的 Tool Registry 管理，不在 Skill 的 prompt 里定义。

转换规则：
1. 提取 name（skill 名称，简短中文或英文）
2. 提取 description（一句话描述 skill 的用途，中文）
3. system_prompt 只保留「给 AI 的行为指令」部分（prompt body），这是核心内容
4. 移除以下不兼容内容，记入 removed_sections：
   - frontmatter 中的 globs / tools / alwaysApply / model 等字段定义
   - function calling / tool_use 的 JSON schema 声明
   - 前端相关代码或生成 UI 的指令
5. 检测 prompt 中是否包含前端/UI 相关意图（生成页面、组件、HTML/CSS 等）

原始内容：
```
{raw_content}
```

只输出 JSON，不要其他内容：
{{"name": "skill名称", "description": "一句话描述", "system_prompt": "转换后的 prompt 内容", "removed_sections": [{{"section": "被移除内容摘要", "reason": "移除原因"}}], "warnings": ["告警信息"], "has_frontend_content": false, "frontend_detail": ""}}"""


def _detect_frontend_in_text(text: str) -> list[str]:
    """检测文本中的前端相关内容，返回匹配到的关键词列表。"""
    hits = []
    for pattern in FRONTEND_KEYWORDS:
        if re.search(pattern, text, re.IGNORECASE):
            hits.append(re.search(pattern, text, re.IGNORECASE).group(0))
    return hits


class ImportConvertRequest(BaseModel):
    content: str


@router.post("/import-convert")
async def import_convert_skill(
    file: UploadFile = File(None),
    content: str = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """智能解析外部 Skill 内容，转换为内部统一格式。

    支持三种输入方式：
    - 上传 .md / .txt 文件
    - 上传 .zip 包（自动解压，读取 SKILL.md 或第一个 .md 文件）
    - 直接传入文本内容（content 字段）

    返回转换预览结果（不入库），前端确认后再调用 POST /skills 创建。
    """
    import json as _json

    raw_content = ""
    source_filename = None

    if file and file.filename:
        raw = await file.read()
        source_filename = file.filename

        # zip 包：解压后读取 SKILL.md 或第一个 .md 文件
        if file.filename.lower().endswith(".zip"):
            import zipfile as _zf
            import io as _io
            try:
                with _zf.ZipFile(_io.BytesIO(raw)) as zf:
                    names = zf.namelist()
                    # 优先找 SKILL.md（不区分路径深度）
                    md_file = None
                    for n in names:
                        base = n.rsplit("/", 1)[-1] if "/" in n else n
                        if base.upper() == "SKILL.MD":
                            md_file = n
                            break
                    # 回退：第一个 .md 文件
                    if not md_file:
                        for n in names:
                            if n.lower().endswith(".md") and not n.startswith("__MACOSX"):
                                md_file = n
                                break
                    if not md_file:
                        raise HTTPException(400, "zip 包中未找到 .md 文件")
                    raw_content = zf.read(md_file).decode("utf-8")
            except _zf.BadZipFile:
                raise HTTPException(400, "无效的 zip 文件")
        else:
            try:
                raw_content = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(400, "文件编码必须是 UTF-8")
    elif content:
        raw_content = content
    else:
        raise HTTPException(400, "请上传文件或提供文本内容")

    if not raw_content.strip():
        raise HTTPException(400, "内容为空")

    # Step 1: 尝试本地解析（已是标准格式则跳过 AI）
    local_parsed = _parse_skill_md(raw_content)
    is_standard = bool(local_parsed["name"]) and len(local_parsed["system_prompt"]) > 30

    # Step 2: 本地前端检测（文件名 + 内容关键词）
    frontend_hits = _detect_frontend_in_text(raw_content)
    file_frontend_warning = None
    if source_filename:
        import os
        ext = os.path.splitext(source_filename)[1].lower()
        if ext in FRONTEND_EXTENSIONS:
            file_frontend_warning = f"文件 {source_filename} 是前端文件"

    # Step 3: 如果已是标准格式且无复杂内容，直接返回（不调 AI）
    has_complex_frontmatter = any(
        kw in raw_content[:500].lower()
        for kw in ["globs:", "tools:", "alwaysapply:", "model:", "tool_use", "function_call"]
    )

    if is_standard and not has_complex_frontmatter:
        result = {
            "name": local_parsed["name"],
            "description": local_parsed["description"],
            "system_prompt": local_parsed["system_prompt"],
            "original_content": raw_content,
            "removed_sections": [],
            "warnings": [],
            "has_frontend_content": bool(frontend_hits) or bool(file_frontend_warning),
            "frontend_detail": "",
            "ai_converted": False,
        }
        if frontend_hits:
            result["frontend_detail"] = f"检测到前端相关内容：{', '.join(frontend_hits[:5])}"
            result["warnings"].append(
                "此 Skill 包含前端相关内容。Le Desk 的 tool 不带前端界面，"
                "Skill 应专注于数据处理、分析、文案生成等后端能力。"
            )
        if file_frontend_warning:
            result["warnings"].insert(0, file_frontend_warning)
        return result

    # Step 4: 调 AI 做智能转换
    try:
        prompt = _IMPORT_CONVERT_PROMPT.format(raw_content=raw_content[:8000])
        ai_result, _ = await llm_gateway.chat(
            model_config=llm_gateway.resolve_config(db, "skill.classify"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=4000,
        )
        text = ai_result.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        parsed = _json.loads(text.strip())
    except Exception as e:
        logger.warning(f"AI import-convert failed: {e}, falling back to local parse")
        # AI 失败时回退到本地解析
        parsed = {
            "name": local_parsed["name"] or "unnamed-skill",
            "description": local_parsed["description"],
            "system_prompt": local_parsed["system_prompt"],
            "removed_sections": [],
            "warnings": [f"AI 智能转换失败（{str(e)[:80]}），已使用基础解析"],
            "has_frontend_content": bool(frontend_hits),
            "frontend_detail": f"检测到前端关键词：{', '.join(frontend_hits[:5])}" if frontend_hits else "",
        }

    # 补充本地前端检测结果
    if frontend_hits and not parsed.get("has_frontend_content"):
        parsed["has_frontend_content"] = True
        parsed["frontend_detail"] = (
            parsed.get("frontend_detail", "") +
            f" 本地检测到前端关键词：{', '.join(frontend_hits[:5])}"
        ).strip()

    if parsed.get("has_frontend_content"):
        warnings = parsed.get("warnings", [])
        frontend_msg = (
            "此 Skill 包含前端相关内容。Le Desk 的 tool 不带前端界面，"
            "Skill 应专注于数据处理、分析、文案生成等后端能力。"
        )
        if frontend_msg not in warnings:
            warnings.append(frontend_msg)
        parsed["warnings"] = warnings

    if file_frontend_warning:
        parsed.setdefault("warnings", []).insert(0, file_frontend_warning)

    parsed["original_content"] = raw_content
    parsed["ai_converted"] = True
    return parsed


@router.post("/upload-md")
async def upload_skill_md(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Upload a .md file to create or update a Skill.

    If a skill with the same name exists, a new version is created.
    Otherwise a new skill is created and auto-published.
    """
    if not file.filename or not file.filename.endswith(".md"):
        raise HTTPException(400, "只支持 .md 文件")

    raw = await file.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "文件编码必须是 UTF-8")

    parsed = _parse_skill_md(content)
    if not parsed["name"]:
        raise HTTPException(400, "文件缺少 frontmatter 中的 name 字段")

    # 先查自己名下的同名 Skill；超管可以更新任意同名 Skill
    if user.role == Role.SUPER_ADMIN:
        existing = db.query(Skill).filter(Skill.name == parsed["name"]).first()
    else:
        existing = db.query(Skill).filter(
            Skill.name == parsed["name"],
            Skill.created_by == user.id,
        ).first()
        # 名字被别人占用时给出明确提示
        if not existing:
            name_taken = db.query(Skill).filter(Skill.name == parsed["name"]).first()
            if name_taken:
                raise HTTPException(400, f"Skill 名称「{parsed['name']}」已被占用，请修改 md 文件中的 name 字段后重新上传")

    if existing:
        # Add a new version
        latest = existing.versions[0] if existing.versions else None
        new_ver = (latest.version + 1) if latest else 1
        v = SkillVersion(
            skill_id=existing.id,
            version=new_ver,
            system_prompt=parsed["system_prompt"],
            variables=parsed["variables"],
            required_inputs=latest.required_inputs if latest else [],
            model_config_id=latest.model_config_id if latest else None,
            output_schema=latest.output_schema if latest else None,
            created_by=user.id,
            change_note=f"从 md 文件上传更新 v{new_ver}",
        )
        db.add(v)
        if parsed["description"]:
            existing.description = parsed["description"]
        db.commit()
        return {
            "action": "updated",
            "id": existing.id,
            "name": existing.name,
            "version": new_ver,
        }
    else:
        # 超管直接发布；其他角色先存为草稿，由用户自行决定何时提交审批
        if user.role == Role.SUPER_ADMIN:
            new_status = SkillStatus.PUBLISHED
            new_scope = "company"
        else:
            new_status = SkillStatus.DRAFT
            new_scope = "personal"
        skill = Skill(
            name=parsed["name"],
            description=parsed["description"],
            mode="hybrid",
            status=new_status,
            scope=new_scope,
            auto_inject=True,
            created_by=user.id,
        )
        db.add(skill)
        db.flush()
        v = SkillVersion(
            skill_id=skill.id,
            version=1,
            system_prompt=parsed["system_prompt"],
            variables=parsed["variables"],
            created_by=user.id,
            change_note="从 md 文件上传创建",
        )
        db.add(v)
        db.commit()
        db.refresh(skill)
        return {
            "action": "created",
            "id": skill.id,
            "name": skill.name,
            "version": 1,
            "status": new_status.value,
        }


@router.post("/batch-upload-md")
async def batch_upload_skill_md(
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Upload multiple .md files at once."""
    results = []
    for f in files:
        if not f.filename or not f.filename.endswith(".md"):
            results.append({"filename": f.filename, "error": "不是 .md 文件"})
            continue
        raw = await f.read()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            results.append({"filename": f.filename, "error": "编码错误"})
            continue
        parsed = _parse_skill_md(content)
        if not parsed["name"]:
            results.append({"filename": f.filename, "error": "缺少 name"})
            continue

        existing = db.query(Skill).filter(Skill.name == parsed["name"]).first()
        if existing:
            latest = existing.versions[0] if existing.versions else None
            new_ver = (latest.version + 1) if latest else 1
            v = SkillVersion(
                skill_id=existing.id,
                version=new_ver,
                system_prompt=parsed["system_prompt"],
                variables=parsed["variables"],
                required_inputs=latest.required_inputs if latest else [],
                model_config_id=latest.model_config_id if latest else None,
                output_schema=latest.output_schema if latest else None,
                created_by=user.id,
                change_note=f"批量上传更新 v{new_ver}",
            )
            db.add(v)
            if parsed["description"]:
                existing.description = parsed["description"]
            results.append({"filename": f.filename, "action": "updated", "id": existing.id, "name": existing.name, "version": new_ver})
        else:
            skill = Skill(
                name=parsed["name"],
                description=parsed["description"],
                mode="hybrid",
                status=SkillStatus.PUBLISHED,
                scope="company",
                auto_inject=True,
                created_by=user.id,
            )
            db.add(skill)
            db.flush()
            v = SkillVersion(
                skill_id=skill.id,
                version=1,
                system_prompt=parsed["system_prompt"],
                variables=parsed["variables"],
                created_by=user.id,
                change_note="批量上传创建",
            )
            db.add(v)
            results.append({"filename": f.filename, "action": "created", "id": skill.id, "name": parsed["name"], "version": 1})

    db.commit()
    return {"results": results, "total": len(results)}


@router.post("/upload-zip")
async def upload_skill_zip(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """上传 .zip 压缩包创建复杂 Skill（主 .md 文件 + 附属参考文件/脚本）。

    zip 包结构：
    - 必须包含一个 .md 文件（优先 index.md / README.md，否则取第一个 .md）
    - 其余文件作为附属文件存储到 uploads/skills/<skill_id>/ 目录
    """
    import zipfile as _zipfile
    import tempfile
    import os as _os
    import shutil
    from pathlib import Path as _Path
    from app.config import settings

    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "只支持 .zip 文件")

    raw = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name

    try:
        with _zipfile.ZipFile(tmp_path, "r") as zf:
            names = zf.namelist()
    except _zipfile.BadZipFile:
        _os.unlink(tmp_path)
        raise HTTPException(400, "不是有效的 zip 文件")

    # 找主 md 文件（忽略 __MACOSX 等系统目录）
    md_files = [n for n in names if n.endswith(".md") and not n.startswith("__") and "/" not in n.lstrip("/")]
    # 也允许一层子目录内的 md
    if not md_files:
        md_files = [n for n in names if n.endswith(".md") and not n.startswith("__")]
    if not md_files:
        _os.unlink(tmp_path)
        raise HTTPException(400, "zip 包中未找到 .md 文件")

    # 优先选 index.md / README.md
    main_md = next(
        (n for n in md_files if _Path(n).name.lower() in ("index.md", "readme.md", "skill.md")),
        md_files[0],
    )

    with _zipfile.ZipFile(tmp_path, "r") as zf:
        md_content = zf.read(main_md).decode("utf-8")
        # 其他文件（排除系统文件和其他 .md 可选保留）
        other_files = [
            n for n in names
            if n != main_md
            and not n.endswith("/")
            and not _Path(n).name.startswith(".")
            and not n.startswith("__MACOSX")
        ]

    parsed = _parse_skill_md(md_content)
    if not parsed["name"]:
        _os.unlink(tmp_path)
        raise HTTPException(400, "主 .md 文件缺少 frontmatter 中的 name 字段")

    # 检查同名 skill
    existing = db.query(Skill).filter(Skill.name == parsed["name"]).first()

    if user.role == Role.SUPER_ADMIN:
        new_status = SkillStatus.PUBLISHED
        new_scope = "company"
        initial_stage = None
    else:
        new_status = SkillStatus.REVIEWING
        new_scope = "personal"
        initial_stage = "super_pending" if user.role == Role.DEPT_ADMIN else "dept_pending"

    if existing:
        skill_id = existing.id
        latest = existing.versions[0] if existing.versions else None
        new_ver = (latest.version + 1) if latest else 1
        v = SkillVersion(
            skill_id=skill_id,
            version=new_ver,
            system_prompt=parsed["system_prompt"],
            variables=parsed["variables"],
            required_inputs=latest.required_inputs if latest else [],
            model_config_id=latest.model_config_id if latest else None,
            created_by=user.id,
            change_note="从 zip 包上传更新",
        )
        db.add(v)
        if parsed["description"]:
            existing.description = parsed["description"]
        action = "updated"
        version = new_ver
    else:
        skill = Skill(
            name=parsed["name"],
            description=parsed["description"],
            mode="hybrid",
            status=new_status,
            scope=new_scope,
            auto_inject=True,
            created_by=user.id,
            source_type="local",
        )
        db.add(skill)
        db.flush()
        skill_id = skill.id
        v = SkillVersion(
            skill_id=skill_id,
            version=1,
            system_prompt=parsed["system_prompt"],
            variables=parsed["variables"],
            created_by=user.id,
            change_note="从 zip 包上传创建",
        )
        db.add(v)
        _zip_approval_id = None
        if initial_stage:
            from app.models.permission import ApprovalRequest, ApprovalRequestType, ApprovalStatus as AStatus
            # Fix 6: 自动采集证据包
            try:
                from app.services.approval_templates import get_auto_evidence
                auto_ep = get_auto_evidence("skill_publish", "skill", skill_id, db)
            except Exception:
                auto_ep = None
            approval = ApprovalRequest(
                request_type=ApprovalRequestType.SKILL_PUBLISH,
                target_id=skill_id,
                target_type="skill",
                requester_id=user.id,
                status=AStatus.PENDING,
                stage=initial_stage,
                evidence_pack=auto_ep if auto_ep else None,
            )
            db.add(approval)
            db.flush()
            _zip_approval_id = approval.id
        action = "created"
        version = 1

    # 提取附属文件到 uploads/skills/<skill_id>/
    upload_base = _Path(settings.UPLOAD_DIR) / "skills" / str(skill_id)
    upload_base.mkdir(parents=True, exist_ok=True)

    saved_files = []
    if other_files:
        with _zipfile.ZipFile(tmp_path, "r") as zf:
            for name in other_files:
                safe_name = _Path(name).name  # 只取文件名，防止路径穿越
                if not safe_name:
                    continue
                dest = upload_base / safe_name
                data = zf.read(name)
                dest.write_bytes(data)
                saved_files.append({
                    "filename": safe_name,
                    "path": f"uploads/skills/{skill_id}/{safe_name}",
                    "size": len(data),
                    "category": _infer_category(safe_name),
                })

    # 更新 source_files
    target_skill = db.get(Skill, skill_id)
    if target_skill:
        target_skill.source_files = saved_files

    db.commit()
    _os.unlink(tmp_path)

    # 异步触发安全扫描（zip 包上传且创建了审批单时）
    if _zip_approval_id:
        import asyncio
        from app.database import SessionLocal
        from app.models.permission import ApprovalRequest as _AR
        from app.services.skill_security_scanner import skill_security_scanner

        async def _run_zip_scan(aid: int, sid: int):
            scan_db = SessionLocal()
            try:
                result = await skill_security_scanner.scan(sid, scan_db)
                req = scan_db.get(_AR, aid)
                if req:
                    req.security_scan_result = result
                    scan_db.commit()
            except Exception as e:
                logger.error(f"安全扫描（zip）后台任务失败 approval={aid}: {e}")
            finally:
                scan_db.close()

        asyncio.create_task(_run_zip_scan(_zip_approval_id, skill_id))

    return {
        "action": action,
        "id": skill_id,
        "name": parsed["name"],
        "version": version,
        "status": new_status.value if not existing else (existing.status.value),
        "source_files": saved_files,
        "stage": initial_stage,
    }


# ─── Skill file CRUD ──────────────────────────────────────────────────────────

TEXT_EXTENSIONS = {".md", ".txt", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".sh", ".toml", ".xml", ".csv"}


def _infer_category(filename: str) -> str:
    """根据文件名/扩展名推断资产文件角色。"""
    name_lower = filename.lower()
    base = name_lower.rsplit("/", 1)[-1]
    if base.endswith((".js", ".py", ".sh", ".ts")):
        return "tool"
    if "template" in base or base.startswith("_"):
        return "template"
    if base.startswith("example") or "example" in base or "/examples/" in name_lower:
        return "example"
    if "-kb." in base or "knowledge" in base:
        return "knowledge-base"
    if "reference" in base or base.endswith((".dot", ".xml")):
        return "reference"
    return "other"


def _check_skill_write_access(skill: Skill, user: User):
    if user.role != Role.SUPER_ADMIN and skill.created_by != user.id:
        raise HTTPException(403, "无权操作此 Skill 的文件")


def _check_skill_read_access(skill: Skill, user: User):
    """读取权限：创建者 / 超管 / 同部门的部门管理员。"""
    if user.role == Role.SUPER_ADMIN:
        return
    if skill.created_by == user.id:
        return
    if user.role == Role.DEPT_ADMIN and skill.department_id == user.department_id:
        return
    raise HTTPException(403, "无权查看此 Skill 的文件")


def _safe_skill_dir(skill_id: int) -> "Path":
    from pathlib import Path
    from app.config import settings
    return Path(settings.UPLOAD_DIR) / "skills" / str(skill_id)


def _safe_file_path(skill_id: int, filename: str) -> "Path":
    from pathlib import Path
    base = _safe_skill_dir(skill_id)
    # 防止路径穿越：只取文件名部分
    safe = Path(filename).name
    if not safe or safe.startswith("."):
        raise HTTPException(400, "无效文件名")
    return base / safe


@router.get("/{skill_id}/files/{filename}")
def get_skill_file(
    skill_id: int,
    filename: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """读取 skill 附属文件内容（文本）。"""
    from pathlib import Path
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")
    _check_skill_read_access(skill, user)

    path = _safe_file_path(skill_id, filename)
    if not path.exists():
        raise HTTPException(404, "文件不存在")

    ext = Path(filename).suffix.lower()
    if ext not in TEXT_EXTENSIONS:
        raise HTTPException(400, "该文件类型不支持文本预览")

    return {"content": path.read_text(encoding="utf-8", errors="replace")}


class SkillFileUpdate(BaseModel):
    content: str


@router.put("/{skill_id}/files/{filename}")
def update_skill_file(
    skill_id: int,
    filename: str,
    req: SkillFileUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """保存 skill 附属文件内容。若文件不存在则创建并追加到 source_files。"""
    from pathlib import Path
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")
    _check_skill_write_access(skill, user)

    path = _safe_file_path(skill_id, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(req.content, encoding="utf-8")

    size = len(req.content.encode("utf-8"))
    rel_path = f"uploads/skills/{skill_id}/{path.name}"
    files = list(skill.source_files or [])
    for f in files:
        if f.get("filename") == path.name:
            f["size"] = size
            break
    else:
        files.append({"filename": path.name, "path": rel_path, "size": size, "category": _infer_category(path.name)})
    skill.source_files = files
    db.commit()
    return {"ok": True, "filename": path.name, "size": size}


@router.post("/{skill_id}/files")
async def upload_skill_file(
    skill_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """上传新 asset 文件到 skill 目录。"""
    from pathlib import Path
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")
    _check_skill_write_access(skill, user)

    if not file.filename:
        raise HTTPException(400, "文件名不能为空")
    safe_name = Path(file.filename).name
    if not safe_name or safe_name.startswith("."):
        raise HTTPException(400, "无效文件名")

    dest = _safe_skill_dir(skill_id) / safe_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = await file.read()
    dest.write_bytes(data)

    files = list(skill.source_files or [])
    rel_path = f"uploads/skills/{skill_id}/{safe_name}"
    for f in files:
        if f.get("filename") == safe_name:
            f["size"] = len(data)
            break
    else:
        files.append({"filename": safe_name, "path": rel_path, "size": len(data), "category": _infer_category(safe_name)})
    skill.source_files = files
    db.commit()
    return {"ok": True, "filename": safe_name, "size": len(data), "source_files": files}


@router.delete("/{skill_id}/files/{filename}")
def delete_skill_file(
    skill_id: int,
    filename: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """删除 skill 附属文件（磁盘 + source_files 列表）。"""
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")
    _check_skill_write_access(skill, user)

    path = _safe_file_path(skill_id, filename)
    if path.exists():
        path.unlink()

    files = [f for f in (skill.source_files or []) if f.get("filename") != path.name]
    skill.source_files = files
    db.commit()
    return {"ok": True, "source_files": files}


@router.patch("/{skill_id}/files/{filename}/category")
def update_file_category(
    skill_id: int,
    filename: str,
    req: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """手动修改资产文件的 category（覆盖自动推断）。"""
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")
    _check_skill_write_access(skill, user)

    category = req.get("category", "other")
    valid = {"knowledge-base", "reference", "example", "tool", "template", "other"}
    if category not in valid:
        raise HTTPException(400, f"无效 category，可选值：{', '.join(sorted(valid))}")

    from pathlib import Path as _P
    safe = _P(filename).name
    files = list(skill.source_files or [])
    found = False
    for f in files:
        if f.get("filename") == safe:
            f["category"] = category
            found = True
            break
    if not found:
        raise HTTPException(404, "文件不存在")
    skill.source_files = files
    db.commit()
    return {"ok": True, "filename": safe, "category": category}


@router.get("/{skill_id}/export-zip")
def export_skill_zip(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """将 Skill 打包为 zip 下载（SKILL.md + 所有 source_files）。"""
    import io as _io
    import zipfile as _zf
    from starlette.responses import StreamingResponse

    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")

    # 获取最新 system_prompt
    latest_ver = (
        db.query(SkillVersion)
        .filter(SkillVersion.skill_id == skill_id)
        .order_by(SkillVersion.version.desc())
        .first()
    )
    system_prompt = latest_ver.system_prompt if latest_ver else ""

    buf = _io.BytesIO()
    with _zf.ZipFile(buf, "w", _zf.ZIP_DEFLATED) as zf:
        # 写入主文件 SKILL.md（带 frontmatter）
        frontmatter = f"---\nname: {skill.name}\ndescription: {skill.description or ''}\n---\n\n"
        zf.writestr("SKILL.md", frontmatter + (system_prompt or ""))

        # 写入附属文件
        for f in (skill.source_files or []):
            file_path = _safe_file_path(skill_id, f["filename"])
            if file_path.exists():
                zf.write(file_path, f["filename"])

    buf.seek(0)
    safe_name = skill.name.replace(" ", "-").replace("/", "-")
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.zip"'},
    )


# ─── Tool binding ─────────────────────────────────────────────────────────────

@router.get("/{skill_id}/bound-tools")
def get_bound_tools(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查询 Skill 已绑定的 ToolRegistry 工具列表。"""
    from app.models.tool import ToolRegistry, SkillTool
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")
    tools = (
        db.query(ToolRegistry)
        .join(SkillTool, SkillTool.tool_id == ToolRegistry.id)
        .filter(SkillTool.skill_id == skill_id)
        .all()
    )
    return [
        {
            "id": t.id,
            "name": t.name,
            "display_name": t.display_name,
            "tool_type": t.tool_type.value if hasattr(t.tool_type, "value") else str(t.tool_type),
            "description": t.description or "",
            "status": t.status.value if hasattr(t.status, "value") else str(t.status),
        }
        for t in tools
    ]


@router.post("/{skill_id}/upload-tool")
async def upload_and_bind_tool(
    skill_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """上传 .py 文件，自动注册为 Tool 并绑定到当前 Skill。"""
    from app.models.tool import ToolRegistry, ToolType, SkillTool
    import ast
    import importlib
    import inspect
    from pathlib import Path as _P

    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")
    _check_skill_write_access(skill, user)

    if not file.filename or not file.filename.endswith(".py"):
        raise HTTPException(400, "仅支持 .py 文件")

    data = await file.read()
    source = data.decode("utf-8")

    # 解析 Python 模块，提取函数签名
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise HTTPException(400, f"Python 语法错误：{e}")

    # 找到 execute 函数或第一个公开函数
    func_name = None
    func_doc = ""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            if func_name is None or node.name == "execute":
                func_name = node.name
                func_doc = ast.get_docstring(node) or ""
                if node.name == "execute":
                    break

    if not func_name:
        raise HTTPException(400, "未找到任何公开函数（需要至少一个不以 _ 开头的函数）")

    # 从文件名生成 tool name
    safe_name = _P(file.filename).stem
    tool_name = f"skill_{skill_id}_{safe_name}"

    # 写入 app/tools/ 目录
    tools_dir = _P(__file__).parent.parent / "tools"
    tools_dir.mkdir(exist_ok=True)
    dest = tools_dir / f"{tool_name}.py"
    dest.write_text(source, encoding="utf-8")

    # 检查是否已存在同名 tool
    existing = db.query(ToolRegistry).filter(ToolRegistry.name == tool_name).first()
    if existing:
        # 更新源码，保留绑定
        existing.config = {"module": tool_name, "function": func_name, "source": f"tools/{tool_name}.py"}
        existing.description = func_doc[:500] if func_doc else f"来自 Skill {skill.name} 的工具"
        db.flush()
        tool_id = existing.id
    else:
        tool = ToolRegistry(
            name=tool_name,
            display_name=safe_name,
            description=func_doc[:500] if func_doc else f"来自 Skill {skill.name} 的工具",
            tool_type=ToolType.BUILTIN,
            config={"module": tool_name, "function": func_name, "source": f"tools/{tool_name}.py"},
            input_schema={},
            scope="personal",
            created_by=user.id,
        )
        db.add(tool)
        db.flush()
        tool_id = tool.id

    # 绑定到 Skill（如果尚未绑定）
    exists_binding = (
        db.query(SkillTool)
        .filter(SkillTool.skill_id == skill_id, SkillTool.tool_id == tool_id)
        .first()
    )
    if not exists_binding:
        db.add(SkillTool(skill_id=skill_id, tool_id=tool_id))

    db.commit()
    return {"ok": True, "tool_id": tool_id, "tool_name": tool_name, "bound": True}


# ─── Skill ranking / hot list ─────────────────────────────────────────────────

@router.get("/ranking")
def get_skill_ranking(
    scope: str = "company",
    department_id: Optional[int] = None,
    days: int = 30,
    limit: int = 10,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Skill 热榜：按过去 N 天的对话数排序。
    scope=company → 通用榜（company scope skills）
    scope=department → 专业榜（department scope skills，可按 department_id 筛选）
    """
    import datetime as dt
    from sqlalchemy import func
    from app.models.conversation import Conversation

    since = dt.datetime.utcnow() - dt.timedelta(days=days)

    q = db.query(Skill).filter(
        Skill.status == SkillStatus.PUBLISHED,
        Skill.scope == scope,
    )
    if scope == "department" and department_id:
        q = q.filter(Skill.department_id == department_id)
    candidate_skill_ids = [s.id for s in q.all()]

    if not candidate_skill_ids:
        return []

    counts = (
        db.query(
            Conversation.skill_id,
            func.count(Conversation.id).label("conv_count"),
            func.count(func.distinct(Conversation.user_id)).label("user_count"),
        )
        .filter(
            Conversation.skill_id.in_(candidate_skill_ids),
            Conversation.created_at >= since,
        )
        .group_by(Conversation.skill_id)
        .order_by(func.count(Conversation.id).desc())
        .limit(limit)
        .all()
    )

    result = []
    for i, (skill_id, conv_count, user_count) in enumerate(counts):
        s = db.get(Skill, skill_id)
        if not s:
            continue
        total = (
            db.query(func.count(Conversation.id))
            .filter(Conversation.skill_id == skill_id)
            .scalar() or 0
        )
        result.append({
            "rank": i + 1,
            "skill_id": skill_id,
            "name": s.name,
            "description": s.description,
            "scope": s.scope,
            "department_id": s.department_id,
            "knowledge_tags": s.knowledge_tags or [],
            "current_version": max((v.version for v in s.versions), default=0),
            "conv_count_recent": conv_count,
            "user_count_recent": user_count,
            "conv_count_total": total,
        })

    return result


# ─── Usage stats (super_admin only) ──────────────────────────────────────────

@router.get("/{skill_id}/usage")
def get_skill_usage(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """超管查看某个 Skill 的使用详情：每个用户使用了多少次对话、多少条消息。"""
    from sqlalchemy import func
    from app.models.conversation import Conversation, Message, MessageRole

    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")

    from app.models.user import User as UserModel
    stats = (
        db.query(
            Conversation.user_id,
            func.count(Conversation.id).label("conv_count"),
        )
        .filter(Conversation.skill_id == skill_id)
        .group_by(Conversation.user_id)
        .all()
    )

    result = []
    total_convs = 0
    for user_id, conv_count in stats:
        u = db.get(UserModel, user_id)
        msg_count = (
            db.query(func.count(Message.id))
            .join(Conversation, Conversation.id == Message.conversation_id)
            .filter(
                Conversation.skill_id == skill_id,
                Conversation.user_id == user_id,
                Message.role == MessageRole.USER,
            )
            .scalar() or 0
        )
        total_convs += conv_count
        result.append({
            "user_id": user_id,
            "display_name": u.display_name if u else f"user#{user_id}",
            "department_id": u.department_id if u else None,
            "conv_count": conv_count,
            "msg_count": msg_count,
        })

    result.sort(key=lambda x: x["conv_count"], reverse=True)
    return {
        "skill_id": skill_id,
        "skill_name": skill.name,
        "total_conv_count": total_convs,
        "total_user_count": len(result),
        "by_user": result,
    }


@router.get("/{skill_id}/execution-stats")
def get_execution_stats(
    skill_id: int,
    days: int = 30,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取 Skill 近 N 天的执行统计（成功率、使用量、平均耗时、平均评分）。"""
    from app.services.skill_engine import skill_engine
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    stats = skill_engine.get_execution_stats(db, skill_id, days=days)
    return {"skill_id": skill_id, "days": days, **stats}


def _get_rejection_comment(skill_id: int, db: Session) -> str | None:
    """返回该 Skill 最近一次被驳回时审批人写的意见（供提交人看）。"""
    try:
        from app.models.permission import ApprovalRequest, ApprovalAction, ApprovalStatus, ApprovalActionType
        req = (
            db.query(ApprovalRequest)
            .filter(
                ApprovalRequest.target_id == skill_id,
                ApprovalRequest.target_type == "skill",
                ApprovalRequest.status == ApprovalStatus.REJECTED,
            )
            .order_by(ApprovalRequest.created_at.desc())
            .first()
        )
        if not req:
            return None
        action = (
            db.query(ApprovalAction)
            .filter(
                ApprovalAction.request_id == req.id,
                ApprovalAction.action == ApprovalActionType.REJECT,
            )
            .order_by(ApprovalAction.created_at.desc())
            .first()
        )
        return action.comment if action and action.comment else None
    except Exception:
        return None


@router.get("/{skill_id}")
def get_skill(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")

    is_external = skill.source_type in ("imported", "forked")
    is_own = skill.created_by == user.id
    is_own_dept = (user.role == Role.DEPT_ADMIN and skill.department_id == user.department_id)
    is_super = user.role == Role.SUPER_ADMIN

    # employee 看别人的非外部 Skill：只返回摘要
    if user.role == Role.EMPLOYEE and not is_own:
        if not is_external:
            return _skill_summary(skill)
        # 外部引入：返回最新版 system_prompt（只读，不含版本历史）
        latest = skill.versions[0] if skill.versions else None
        return {
            **_skill_summary(skill),
            "source_type": skill.source_type,
            "system_prompt": latest.system_prompt if latest else "",
        }

    # 可查看完整 prompt 的条件：超管 / 同部门管理员 / 自己创建的 / 外部引入
    can_view_prompt = is_super or is_own_dept or is_own or is_external

    def _version_dict(v) -> dict:
        base = {
            "id": v.id,
            "version": v.version,
            "variables": v.variables or [],
            "required_inputs": v.required_inputs or [],
            "model_config_id": v.model_config_id,
            "output_schema": v.output_schema,
            "change_note": v.change_note,
            "created_by": v.created_by,
            "created_at": v.created_at.isoformat(),
        }
        if can_view_prompt:
            base["system_prompt"] = v.system_prompt
        return base

    return {
        **_skill_summary(skill),
        "source_type": skill.source_type,
        "versions": [_version_dict(v) for v in skill.versions],
        "rejection_comment": _get_rejection_comment(skill.id, db),
    }


@router.put("/{skill_id}")
def update_skill(
    skill_id: int,
    req: SkillCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")

    # 权限：超管/部门管理员可编辑，普通员工只能编辑自己创建的
    is_admin = user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN)
    if not is_admin and skill.created_by != user.id:
        raise HTTPException(403, "无权编辑此 Skill")

    skill.name = req.name
    skill.description = req.description
    skill.mode = req.mode
    # 普通员工不可自行设置 department_id / knowledge_tags
    if is_admin:
        skill.department_id = req.department_id
        skill.knowledge_tags = req.knowledge_tags
    skill.auto_inject = req.auto_inject
    db.commit()
    return {"id": skill.id}


@router.patch("/{skill_id}/data-queries")
def update_data_queries(
    skill_id: int,
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Replace the data_queries bindings for a skill."""
    from app.models.business import SkillDataQuery
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    # Only owner or admin may edit
    is_admin = user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN)
    if not is_admin and skill.created_by != user.id:
        raise HTTPException(403, "无权限")
    queries = body.get("data_queries") or []
    db.query(SkillDataQuery).filter(SkillDataQuery.skill_id == skill_id).delete()
    for q in queries:
        table_name = q.get("table_name", "").strip()
        if not table_name:
            continue
        db.add(SkillDataQuery(
            skill_id=skill_id,
            query_name=q.get("query_name") or f"read_{table_name}",
            query_type=q.get("query_type") or "read",
            table_name=table_name,
            description=q.get("description") or "",
        ))
    db.commit()
    return {"ok": True}


@router.post("/{skill_id}/versions")
def add_version(
    skill_id: int,
    req: SkillVersionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")

    # 权限：超管/部门管理员可编辑，普通员工只能编辑自己创建的
    if user.role not in (Role.SUPER_ADMIN, Role.DEPT_ADMIN) and skill.created_by != user.id:
        raise HTTPException(403, "无权编辑此 Skill")

    max_ver = max((v.version for v in skill.versions), default=0)
    v = SkillVersion(
        skill_id=skill_id,
        version=max_ver + 1,
        system_prompt=req.system_prompt,
        variables=req.variables,
        required_inputs=req.required_inputs,
        model_config_id=req.model_config_id,
        output_schema=req.output_schema,
        created_by=user.id,
        change_note=req.change_note,
    )
    # Mark as customized if this is an imported/forked skill being modified
    if skill.source_type in ("imported", "forked"):
        import datetime as _dt
        skill.is_customized = True
        skill.local_modified_at = _dt.datetime.utcnow()
    db.add(v)
    db.flush()

    # 已发布 Skill 的版本变更 → 创建审批单
    approval_id = None
    if skill.status == SkillStatus.PUBLISHED and v.version > 1:
        from app.models.permission import (
            ApprovalRequest, ApprovalRequestType, ApprovalStatus,
        )
        existing_approval = (
            db.query(ApprovalRequest)
            .filter(
                ApprovalRequest.target_id == skill_id,
                ApprovalRequest.target_type == "skill",
                ApprovalRequest.request_type
                == ApprovalRequestType.SKILL_VERSION_CHANGE,
                ApprovalRequest.status == ApprovalStatus.PENDING,
            )
            .first()
        )
        if not existing_approval:
            try:
                from app.services.approval_templates import get_auto_evidence
                auto_ep = get_auto_evidence("skill_version_change", "skill", skill_id, db)
            except Exception:
                auto_ep = None
            approval = ApprovalRequest(
                request_type=ApprovalRequestType.SKILL_VERSION_CHANGE,
                target_id=skill_id,
                target_type="skill",
                requester_id=user.id,
                status=ApprovalStatus.PENDING,
                stage="dept_pending",
                evidence_pack=auto_ep if auto_ep else None,
            )
            db.add(approval)
            db.flush()
            approval_id = approval.id

    db.commit()

    # Gap 7: 新版本保存时，如上一版本有 baseline，发射回归触发事件
    try:
        prev_ver = (
            db.query(SkillVersion)
            .filter(
                SkillVersion.skill_id == skill_id,
                SkillVersion.version < v.version,
                SkillVersion.baseline_sandbox_session_id.isnot(None),
            )
            .order_by(SkillVersion.version.desc())
            .first()
        )
        if prev_ver:
            from app.services import event_bus
            event_bus.emit(
                db, event_type="regression_triggered", source_type="skill", source_id=skill_id,
                payload={"baseline_version": prev_ver.version, "new_version": v.version},
                user_id=user.id,
            )
    except Exception:
        pass

    return {
        "version": v.version,
        "id": v.id,
        "approval_id": approval_id,
    }


@router.post("/{skill_id}/transfer-ownership")
def transfer_ownership(
    skill_id: int,
    new_owner_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """发起 Skill 所有权转让审批。"""
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    if skill.created_by != user.id and user.role != Role.SUPER_ADMIN:
        raise HTTPException(403, "只有 Skill 创建者或超管可以发起转让")
    if skill.created_by == new_owner_id:
        raise HTTPException(400, "新所有者与当前所有者相同")

    from app.models.user import User as UserModel
    new_owner = db.get(UserModel, new_owner_id)
    if not new_owner:
        raise HTTPException(404, "目标用户不存在")

    from app.models.permission import (
        ApprovalRequest, ApprovalRequestType, ApprovalStatus,
    )
    existing = (
        db.query(ApprovalRequest)
        .filter(
            ApprovalRequest.target_id == skill_id,
            ApprovalRequest.target_type == "skill",
            ApprovalRequest.request_type
            == ApprovalRequestType.SKILL_OWNERSHIP_TRANSFER,
            ApprovalRequest.status == ApprovalStatus.PENDING,
        )
        .first()
    )
    if existing:
        raise HTTPException(400, "已有待审批的转让申请")

    try:
        from app.services.approval_templates import get_auto_evidence
        auto_ep = get_auto_evidence("skill_ownership_transfer", "skill", skill_id, db)
    except Exception:
        auto_ep = None
    approval = ApprovalRequest(
        request_type=ApprovalRequestType.SKILL_OWNERSHIP_TRANSFER,
        target_id=skill_id,
        target_type="skill",
        requester_id=user.id,
        status=ApprovalStatus.PENDING,
        stage="dept_pending",
        conditions=[{"new_owner_id": new_owner_id}],
        evidence_pack=auto_ep if auto_ep else None,
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)
    return {"approval_id": approval.id, "status": "pending"}


def _cascade_tool_status_on_publish(skill_id: int, db: Session) -> None:
    """Skill 发布通过后，自动将绑定的 Tool 状态改为 published + is_active。"""
    import datetime as _dt
    from app.models.tool import SkillTool, ToolRegistry
    tool_ids = [row.tool_id for row in db.query(SkillTool).filter(SkillTool.skill_id == skill_id).all()]
    for tid in tool_ids:
        tool = db.get(ToolRegistry, tid)
        if tool and tool.status != "published":
            tool.status = "published"
            tool.is_active = True
            tool.updated_at = _dt.datetime.utcnow()


def _cascade_tool_status_on_archive(skill_id: int, db: Session) -> None:
    """Skill 归档时，自动将绑定的 Tool 也归档。"""
    import datetime as _dt
    from app.models.tool import SkillTool, ToolRegistry
    tool_ids = [row.tool_id for row in db.query(SkillTool).filter(SkillTool.skill_id == skill_id).all()]
    for tid in tool_ids:
        tool = db.get(ToolRegistry, tid)
        if tool and tool.status != "archived":
            tool.status = "archived"
            tool.is_active = False
            tool.updated_at = _dt.datetime.utcnow()


@router.patch("/{skill_id}/status")
def update_status(
    skill_id: int,
    status: str = Query(...),
    scope: Optional[str] = Query(None),          # company / department / personal
    department_id: Optional[int] = Query(None),  # 指定部门时填写
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """更新 Skill 状态，发布时可同时设置可见范围。
    scope: company=全公司, department=指定部门（需提供 department_id）, personal=仅自己

    权限：
    - EMPLOYEE：只能对自己创建的 Skill 提交审核（status=published → 实际转为 reviewing）
    - DEPT_ADMIN：可对本部门 Skill 提交审核
    - SUPER_ADMIN：可直接发布
    """
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    if status not in [s.value for s in SkillStatus]:
        raise HTTPException(400, f"Invalid status: {status}")

    # 权限校验：员工只能操作自己创建的 Skill
    if user.role == Role.EMPLOYEE:
        if skill.created_by != user.id:
            raise HTTPException(403, "只能操作自己创建的 Skill")

    # 发布前校验：无绑定工具的 Skill，system_prompt 行数不得低于 200
    if status == SkillStatus.PUBLISHED.value:
        from app.models.tool import SkillTool
        has_tools = db.query(SkillTool).filter(SkillTool.skill_id == skill_id).first() is not None
        if not has_tools:
            latest_ver = (
                db.query(SkillVersion)
                .filter(SkillVersion.skill_id == skill_id)
                .order_by(SkillVersion.version.desc())
                .first()
            )
            prompt = latest_ver.system_prompt if latest_ver else ""
            line_count = len(prompt.strip().splitlines()) if prompt and prompt.strip() else 0
            if line_count < 200:
                raise HTTPException(
                    400,
                    f"Skill 未绑定任何工具，system_prompt 当前仅 {line_count} 行，"
                    f"发布要求至少 200 行（请补充完整的指令内容，或为 Skill 绑定工具）",
                )

        # 知识引用安全校验
        from app.services.skill_knowledge_checker import validate_skill_knowledge_references
        kr_result = validate_skill_knowledge_references(skill_id, user.id, db)
        if kr_result.get("blocked"):
            raise HTTPException(400, {
                "blocked": True,
                "reasons": kr_result["block_reasons"],
                "risk_summary": kr_result.get("risk_summary", {}),
            })

        # 沙盒报告版本校验：最新报告的 target_version 必须等于当前最新版本
        from app.models.sandbox import SandboxTestReport
        latest_report = (
            db.query(SandboxTestReport)
            .filter(
                SandboxTestReport.target_id == skill_id,
                SandboxTestReport.target_type == "skill",
            )
            .order_by(SandboxTestReport.created_at.desc())
            .first()
        )
        current_ver = (
            db.query(SkillVersion)
            .filter(SkillVersion.skill_id == skill_id)
            .order_by(SkillVersion.version.desc())
            .first()
        )
        current_version_num = current_ver.version if current_ver else None
        if not latest_report:
            raise HTTPException(
                400,
                f"发布前需要至少通过一次质量检测（沙盒测试）",
            )
        if latest_report.target_version is not None and current_version_num is not None:
            if latest_report.target_version != current_version_num:
                raise HTTPException(
                    400,
                    f"质量检测报告基于 v{latest_report.target_version}，"
                    f"但当前 Skill 已更新到 v{current_version_num}，请重新运行质量检测",
                )
        if latest_report.approval_eligible is False:
            raise HTTPException(
                400,
                f"最近一次质量检测结果为不可发布，请修复后重新检测",
            )

    # EMPLOYEE / DEPT_ADMIN 申请发布 → 转为审核中，创建审批单等超管审批
    if status == SkillStatus.PUBLISHED.value and user.role in (Role.EMPLOYEE, Role.DEPT_ADMIN):
        if scope is not None:
            skill.scope = scope
        if department_id is not None:
            skill.department_id = department_id
        skill.status = SkillStatus.REVIEWING
        db.flush()
        # 幂等：同一 skill 不重复创建待审批单
        from app.models.permission import ApprovalRequest, ApprovalRequestType, ApprovalStatus
        existing_approval = (
            db.query(ApprovalRequest)
            .filter(
                ApprovalRequest.target_id == skill_id,
                ApprovalRequest.target_type == "skill",
                ApprovalRequest.status == ApprovalStatus.PENDING,
            )
            .first()
        )
        if not existing_approval:
            try:
                from app.services.approval_templates import get_auto_evidence
                auto_ep = get_auto_evidence("skill_publish", "skill", skill_id, db)
            except Exception:
                auto_ep = None
            approval = ApprovalRequest(
                request_type=ApprovalRequestType.SKILL_PUBLISH,
                target_id=skill_id,
                target_type="skill",
                requester_id=user.id,
                status=ApprovalStatus.PENDING,
                evidence_pack=auto_ep if auto_ep else None,
            )
            db.add(approval)
            db.flush()
            approval_id = approval.id
        else:
            approval_id = existing_approval.id
        # 保存知识引用快照
        _save_knowledge_reference_snapshot(skill_id, user.id, kr_result, db)
        db.commit()
        # 异步触发安全扫描（不阻塞提交流程）
        import asyncio
        from app.database import SessionLocal
        from app.services.skill_security_scanner import skill_security_scanner

        async def _run_scan(aid: int, sid: int):
            scan_db = SessionLocal()
            try:
                result = await skill_security_scanner.scan(sid, scan_db)
                req = scan_db.get(ApprovalRequest, aid)
                if req:
                    req.security_scan_result = result
                    scan_db.commit()
            except Exception as e:
                logger.error(f"安全扫描后台任务失败 approval={aid}: {e}")
            finally:
                scan_db.close()

        asyncio.create_task(_run_scan(approval_id, skill_id))
        return {"id": skill_id, "status": SkillStatus.REVIEWING.value, "scope": skill.scope}

    skill.status = status
    if scope is not None:
        skill.scope = scope
    if department_id is not None:
        skill.department_id = department_id

    # SUPER_ADMIN 直接发布时生成 SkillPolicy
    if status == SkillStatus.PUBLISHED.value:
        _ensure_skill_policy(skill_id, user, db)
        # 写入知识引用快照
        _save_knowledge_reference_snapshot(skill_id, user.id, kr_result, db)
        # 联动发布绑定的 Tool
        _cascade_tool_status_on_publish(skill_id, db)

    # 归档时联动归档绑定的 Tool
    if status == SkillStatus.ARCHIVED.value:
        _cascade_tool_status_on_archive(skill_id, db)

    db.commit()
    return {"id": skill_id, "status": status, "scope": skill.scope}


# ── 知识引用安全检查 ─────────────────────────────────────────────────────────

@router.post("/{skill_id}/publish-precheck")
def publish_precheck(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """发布前知识引用安全校验。"""
    from app.services.skill_knowledge_checker import validate_skill_knowledge_references
    result = validate_skill_knowledge_references(skill_id, user.id, db)
    return result


@router.get("/{skill_id}/knowledge-references")
def get_knowledge_references(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查询 Skill 已审知识引用列表。"""
    from app.models.skill_knowledge_ref import SkillKnowledgeReference
    refs = (
        db.query(SkillKnowledgeReference)
        .filter(SkillKnowledgeReference.skill_id == skill_id)
        .order_by(SkillKnowledgeReference.created_at.desc())
        .all()
    )
    return {
        "skill_id": skill_id,
        "references": [
            {
                "id": r.id,
                "knowledge_id": r.knowledge_id,
                "folder_path": r.folder_path,
                "snapshot_desensitization_level": r.snapshot_desensitization_level,
                "snapshot_data_type_hits": r.snapshot_data_type_hits,
                "snapshot_document_type": r.snapshot_document_type,
                "snapshot_permission_domain": r.snapshot_permission_domain,
                "snapshot_mask_rules": r.snapshot_mask_rules,
                "mask_rule_source": r.mask_rule_source,
                "manager_scope_ok": r.manager_scope_ok,
                "publish_version": r.publish_version,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in refs
        ],
    }


class BatchPublishRequest(BaseModel):
    skill_ids: list[int]
    scope: str = "company"  # company / department / personal


@router.post("/batch-publish")
def batch_publish(
    req: BatchPublishRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    """超管批量发布 Skill，全部设为 published + 指定 scope。"""
    if req.scope not in ("company", "department", "personal"):
        raise HTTPException(400, "Invalid scope")
    from app.models.tool import SkillTool
    results = []
    for skill_id in req.skill_ids:
        skill = db.get(Skill, skill_id)
        if not skill:
            results.append({"id": skill_id, "ok": False, "reason": "not found"})
            continue
        # 无绑定工具时校验 system_prompt 行数
        has_tools = db.query(SkillTool).filter(SkillTool.skill_id == skill_id).first() is not None
        if not has_tools:
            latest_ver = (
                db.query(SkillVersion)
                .filter(SkillVersion.skill_id == skill_id)
                .order_by(SkillVersion.version.desc())
                .first()
            )
            prompt = latest_ver.system_prompt if latest_ver else ""
            line_count = len(prompt.strip().splitlines()) if prompt and prompt.strip() else 0
            if line_count < 200:
                results.append({
                    "id": skill_id,
                    "ok": False,
                    "name": skill.name,
                    "reason": f"未绑定工具且 system_prompt 仅 {line_count} 行（要求 ≥200 行）",
                })
                continue
        skill.status = SkillStatus.PUBLISHED
        skill.scope = req.scope
        _ensure_skill_policy(skill_id, user, db)
        _cascade_tool_status_on_publish(skill_id, db)
        results.append({"id": skill_id, "ok": True, "name": skill.name})
    db.commit()
    ok_count = sum(1 for r in results if r["ok"])
    return {"published": ok_count, "total": len(req.skill_ids), "results": results}


def _save_knowledge_reference_snapshot(skill_id: int, user_id: int, kr_result: dict, db) -> None:
    """将 precheck 结果写入 SkillKnowledgeReference 快照。"""
    from app.models.skill_knowledge_ref import SkillKnowledgeReference
    refs = kr_result.get("references", [])
    if not refs:
        return
    # 获取当前最大 publish_version
    max_ver = (
        db.query(SkillKnowledgeReference.publish_version)
        .filter(SkillKnowledgeReference.skill_id == skill_id)
        .order_by(SkillKnowledgeReference.publish_version.desc())
        .first()
    )
    new_ver = (max_ver[0] + 1) if max_ver else 1
    for ref in refs:
        db.add(SkillKnowledgeReference(
            skill_id=skill_id,
            knowledge_id=ref["knowledge_id"],
            snapshot_desensitization_level=ref.get("desensitization_level"),
            snapshot_data_type_hits=ref.get("data_type_hits", []),
            snapshot_document_type=ref.get("document_type"),
            snapshot_permission_domain=ref.get("permission_domain"),
            snapshot_mask_rules=[
                {"data_type": r.get("data_type"), "mask_action": r.get("mask_action")}
                for r in ref.get("effective_mask_rules", [])
            ],
            mask_rule_source=ref.get("mask_rule_source"),
            folder_id=ref.get("folder_id"),
            folder_path=ref.get("folder_path"),
            manager_scope_ok=ref.get("manager_scope_ok", False),
            publish_version=new_ver,
        ))


def _ensure_skill_policy(skill_id: int, user: User, db) -> None:
    """发布时自动生成 SkillPolicy（若已存在则跳过）。
    publish_scope（可用范围）按 skill.scope 映射：personal→self_only, department→same_role, company→org_wide
    view_scope（可见范围）默认比 publish_scope 宽一级（至少同部门可见）
    """
    from app.models.permission import SkillPolicy, PublishScope
    existing = db.query(SkillPolicy).filter(SkillPolicy.skill_id == skill_id).first()
    if existing:
        return

    skill = db.get(Skill, skill_id)
    scope_map = {
        "personal": PublishScope.SELF_ONLY,
        "department": PublishScope.SAME_ROLE,
        "company": PublishScope.ORG_WIDE,
    }
    publish_scope = scope_map.get(skill.scope or "personal", PublishScope.SAME_ROLE)
    # view_scope 默认比 use_scope 宽一级
    view_scope_map = {
        PublishScope.SELF_ONLY: PublishScope.SAME_ROLE,
        PublishScope.SAME_ROLE: PublishScope.SAME_ROLE,
        PublishScope.CROSS_ROLE: PublishScope.ORG_WIDE,
        PublishScope.ORG_WIDE: PublishScope.ORG_WIDE,
    }
    view_scope = view_scope_map.get(publish_scope, PublishScope.ORG_WIDE)

    policy = SkillPolicy(
        skill_id=skill_id,
        publish_scope=publish_scope,
        view_scope=view_scope,
        default_data_scope={},
    )
    db.add(policy)
    db.flush()


class AIEditRequest(BaseModel):
    instruction: str
    model_config_id: Optional[int] = None


class AIEditApply(BaseModel):
    proposed: dict
    change_note: str


@router.post("/{skill_id}/edit-with-ai")
async def edit_with_ai(
    skill_id: int,
    req: AIEditRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Generate AI-powered edit preview from natural language instruction."""
    from app.services.skill_editor import skill_editor
    model_config = llm_gateway.resolve_config(db, "skill.run_in_router", req.model_config_id)
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    if user.role not in (Role.SUPER_ADMIN, Role.DEPT_ADMIN) and skill.created_by != user.id:
        raise HTTPException(403, "无权编辑此 Skill")
    try:
        preview = await skill_editor.edit_skill(skill_id, req.instruction, model_config, db)
        return preview
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/{skill_id}/edit-with-ai/apply")
def apply_ai_edit(
    skill_id: int,
    req: AIEditApply,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Apply AI-generated edit by creating a new version."""
    from app.services.skill_editor import skill_editor
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    if user.role not in (Role.SUPER_ADMIN, Role.DEPT_ADMIN) and skill.created_by != user.id:
        raise HTTPException(403, "无权编辑此 Skill")
    try:
        result = skill_editor.apply_edit(skill_id, req.proposed, req.change_note, user.id, db)
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


class IterateRequest(BaseModel):
    suggestion_ids: list[int]
    model_config_id: Optional[int] = None


class IterateApply(BaseModel):
    proposed: dict
    change_note: str
    suggestion_ids: list[int]


@router.post("/{skill_id}/iterate")
async def iterate_from_suggestions(
    skill_id: int,
    req: IterateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Generate AI-powered diff based on adopted suggestions."""
    from app.services.skill_editor import skill_editor
    model_config = llm_gateway.resolve_config(db, "skill.run_in_router", req.model_config_id)
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    if user.role not in (Role.SUPER_ADMIN, Role.DEPT_ADMIN) and skill.created_by != user.id:
        raise HTTPException(403, "无权编辑此 Skill")
    try:
        preview = await skill_editor.iterate_from_suggestions(
            skill_id, req.suggestion_ids, model_config, db
        )
        return preview
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/{skill_id}/iterate/apply")
def apply_iterate(
    skill_id: int,
    req: IterateApply,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Apply iterated version and generate attributions."""
    from app.services.skill_editor import skill_editor
    from app.services.attribution import attribution_service
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    if user.role not in (Role.SUPER_ADMIN, Role.DEPT_ADMIN) and skill.created_by != user.id:
        raise HTTPException(403, "无权编辑此 Skill")
    latest = skill.versions[0] if skill.versions else None
    version_from = latest.version if latest else 0
    try:
        result = skill_editor.apply_edit(skill_id, req.proposed, req.change_note, user.id, db)
        version_to = result["version"]
        # Fire-and-forget attribution (non-blocking)
        import asyncio
        try:
            model_config = llm_gateway.resolve_config(db, "skill.run_in_router")
            asyncio.create_task(
                attribution_service.generate_attributions(
                    skill_id=skill_id,
                    version_from=version_from,
                    version_to=version_to,
                    suggestion_ids=req.suggestion_ids,
                    model_config=model_config,
                    db=db,
                )
            )
        except Exception:
            pass  # Attribution is best-effort
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


@router.delete("/{skill_id}")
def delete_skill(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    if user.role == Role.EMPLOYEE:
        if skill.created_by != user.id:
            raise HTTPException(403, "只能删除自己创建的 Skill")
        # 已发布的 Skill：ownership 转公司，不真正删除
        if skill.status == SkillStatus.PUBLISHED:
            skill.created_by = None
            skill.scope = "company"
            _sync_skill_to_workspace_config(db, user, skill_id, "own", add=False)
            db.commit()
            return {"ok": True, "transferred": True}
    elif user.role == Role.DEPT_ADMIN:
        if skill.department_id != user.department_id and skill.created_by != user.id:
            raise HTTPException(403, "只能删除本部门的 Skill")

    # 级联删除关联记录（外键约束）
    from sqlalchemy import text
    sid = skill_id
    # 子 skill 的 parent 引用置空（自引用外键）
    db.execute(text("UPDATE skills SET parent_skill_id = NULL WHERE parent_skill_id = :sid"), {"sid": sid})
    # skill_policies 下的子表先删
    db.execute(text("DELETE FROM skill_mask_overrides WHERE skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM role_policy_overrides WHERE skill_policy_id IN (SELECT id FROM skill_policies WHERE skill_id = :sid)"), {"sid": sid})
    db.execute(text("DELETE FROM skill_output_schemas WHERE skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM skill_agent_connections WHERE skill_policy_id IN (SELECT id FROM skill_policies WHERE skill_id = :sid) OR connected_skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM handoff_executions WHERE upstream_skill_id = :sid OR downstream_skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM handoff_schema_caches WHERE upstream_skill_id = :sid OR downstream_skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM handoff_templates WHERE upstream_skill_id = :sid OR downstream_skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM skill_policies WHERE skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM approval_actions WHERE request_id IN (SELECT id FROM approval_requests WHERE target_id = :sid AND target_type = 'skill')"), {"sid": sid})
    db.execute(text("DELETE FROM approval_requests WHERE target_id = :sid AND target_type = 'skill'"), {"sid": sid})
    db.execute(text("DELETE FROM workspace_skills WHERE skill_id = :sid"), {"sid": sid})
    # skill 自身关联表
    db.execute(text("DELETE FROM skill_attributions WHERE skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM skill_tools WHERE skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM skill_upstream_checks WHERE skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM skill_data_queries WHERE skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM user_saved_skills WHERE skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM skill_suggestions WHERE skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM skill_memos WHERE skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM skill_preflight_results WHERE skill_id = :sid"), {"sid": sid})
    db.execute(text("DELETE FROM skill_versions WHERE skill_id = :sid"), {"sid": sid})
    # conversations 置空 skill_id（保留对话记录）
    db.execute(text("UPDATE conversations SET skill_id = NULL WHERE skill_id = :sid"), {"sid": sid})
    # sandbox 测试数据（表可能尚未创建，安全跳过）
    _sandbox_stmts = [
        ("UPDATE sandbox_test_sessions SET report_id = NULL"
         " WHERE report_id IN (SELECT id FROM sandbox_test_reports WHERE session_id IN"
         " (SELECT id FROM sandbox_test_sessions WHERE target_type = 'skill' AND target_id = :sid))"),
        "DELETE FROM sandbox_test_reports WHERE session_id IN (SELECT id FROM sandbox_test_sessions WHERE target_type = 'skill' AND target_id = :sid)",
        "DELETE FROM sandbox_test_cases WHERE session_id IN (SELECT id FROM sandbox_test_sessions WHERE target_type = 'skill' AND target_id = :sid)",
        "DELETE FROM sandbox_test_evidences WHERE session_id IN (SELECT id FROM sandbox_test_sessions WHERE target_type = 'skill' AND target_id = :sid)",
        "DELETE FROM sandbox_test_sessions WHERE target_type = 'skill' AND target_id = :sid",
    ]
    for stmt in _sandbox_stmts:
        try:
            db.execute(text(stmt), {"sid": sid})
        except Exception:
            pass  # 表不存在时安全跳过
    # 清理附属文件目录（如果有）
    from pathlib import Path as _Path
    from app.config import settings
    import shutil as _shutil
    skill_files_dir = _Path(settings.UPLOAD_DIR) / "skills" / str(sid)
    if skill_files_dir.exists():
        _shutil.rmtree(skill_files_dir, ignore_errors=True)

    try:
        db.delete(skill)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"删除失败，数据库约束错误：{e}")
    return {"ok": True}


@router.get("/{skill_id}/upstream-diff")
def get_upstream_diff(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Return upstream vs local diff for an imported skill."""
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    if not skill.upstream_content:
        return {"has_upstream": False}

    latest = skill.versions[0] if skill.versions else None
    local_prompt = latest.system_prompt if latest else ""

    from app.models.mcp import SkillUpstreamCheck
    latest_check = (
        db.query(SkillUpstreamCheck)
        .filter(SkillUpstreamCheck.skill_id == skill_id)
        .order_by(SkillUpstreamCheck.checked_at.desc())
        .first()
    )

    return {
        "has_upstream": True,
        "source_type": skill.source_type,
        "upstream_version": skill.upstream_version,
        "upstream_synced_at": skill.upstream_synced_at.isoformat() if skill.upstream_synced_at else None,
        "is_customized": skill.is_customized,
        "upstream_content": skill.upstream_content,
        "local_content": local_prompt,
        "has_new_upstream": latest_check.has_diff if latest_check else False,
        "new_upstream_version": latest_check.upstream_version if latest_check else None,
        "diff_summary": latest_check.diff_summary if latest_check else None,
        "check_action": latest_check.action if latest_check else None,
    }


class UpstreamSyncRequest(BaseModel):
    action: str  # overwrite / ignore


@router.post("/{skill_id}/upstream-sync")
def upstream_sync(
    skill_id: int,
    req: UpstreamSyncRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Handle sync decision: overwrite local with upstream, or ignore upstream update."""
    from app.models.mcp import SkillUpstreamCheck, McpSource
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")

    latest_check = (
        db.query(SkillUpstreamCheck)
        .filter(SkillUpstreamCheck.skill_id == skill_id, SkillUpstreamCheck.has_diff == True)
        .order_by(SkillUpstreamCheck.checked_at.desc())
        .first()
    )

    if req.action == "ignore":
        if latest_check:
            latest_check.action = "ignored"
        db.commit()
        return {"ok": True, "action": "ignored"}

    if req.action == "overwrite":
        source = db.query(McpSource).filter(McpSource.is_active == True).first()
        if not source or not skill.upstream_id:
            raise HTTPException(400, "Cannot fetch upstream: no active source")

        from app.services.mcp_client import fetch_remote_skill, McpClientError
        try:
            remote = fetch_remote_skill(source, skill.upstream_id)
        except McpClientError as e:
            raise HTTPException(502, str(e))

        new_prompt = remote.get("system_prompt", "")
        new_version = remote.get("upstream_version", "")

        max_ver = max((v.version for v in skill.versions), default=0)
        import datetime as dt
        latest_local = skill.versions[0] if skill.versions else None
        v = SkillVersion(
            skill_id=skill_id,
            version=max_ver + 1,
            system_prompt=new_prompt,
            variables=[],
            output_schema=remote.get("output_schema", latest_local.output_schema if latest_local else None),
            created_by=user.id,
            change_note=f"同步上游 v{new_version}",
        )
        db.add(v)

        skill.upstream_content = new_prompt
        skill.upstream_version = new_version
        skill.upstream_synced_at = dt.datetime.utcnow()
        skill.is_customized = False

        if latest_check:
            latest_check.action = "synced"

        db.commit()
        return {"ok": True, "action": "overwrite", "new_version": v.version}

    raise HTTPException(400, f"Unknown action: {req.action}")


# ─── 安全扫描 ────────────────────────────────────────────────────────────────

@router.post("/{skill_id}/security-scan")
async def trigger_security_scan(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """手动触发 Skill 安全扫描，将结果写入最近一条审批单。"""
    from app.models.permission import ApprovalRequest
    from app.services.skill_security_scanner import skill_security_scanner

    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")

    # 找最近一条审批单
    approval = (
        db.query(ApprovalRequest)
        .filter(
            ApprovalRequest.target_id == skill_id,
            ApprovalRequest.target_type == "skill",
        )
        .order_by(ApprovalRequest.created_at.desc())
        .first()
    )

    result = await skill_security_scanner.scan(skill_id, db)

    if approval:
        approval.security_scan_result = result
        db.commit()

    return {
        "ok": True,
        "skill_id": skill_id,
        "approval_id": approval.id if approval else None,
        "scan_result": result,
    }


# ─── Long text ingest pipeline ────────────────────────────────────────────────


class IngestPasteBody(BaseModel):
    content: str


@router.post("/{skill_id}/ingest-paste")
async def ingest_paste(
    skill_id: int,
    body: IngestPasteBody,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """长文本预处理管线：意图识别 → 拆块存储 → 关系分析，返回 SSE 事件流。"""
    import json
    from pathlib import Path
    from fastapi.responses import StreamingResponse

    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")
    _check_skill_write_access(skill, user)

    content = body.content
    if not content or not content.strip():
        raise HTTPException(400, "内容不能为空")

    latest = skill.versions[0] if skill.versions else None
    system_prompt_preview = (latest.system_prompt[:2000] if latest and latest.system_prompt else "")

    def _sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    async def generate():
        # SSE generator 运行时 Depends(get_db) 的 session 已关闭，需要独立 session
        from app.database import SessionLocal
        _db = SessionLocal()
        try:
            async for chunk in _generate_inner(_db):
                yield chunk
        finally:
            _db.close()

    async def _generate_inner(db):
        # 重新加载 skill（外层 session 已关闭）
        skill = db.get(Skill, skill_id)
        try:
            # ── Step A: 意图识别 + 内容拆块 ──
            yield _sse("stage", {"stage": "ingest_parsing", "label": "识别内容类型..."})

            parse_prompt = f"""你是内容分析助手。用户在 Skill Studio 中粘贴了一段长文本，请识别意图并拆分为独立内容块。

当前 Skill 信息：
- 名称：{skill.name}
- 描述：{skill.description or '无'}

用户粘贴的内容：
---
{content}
---

请返回 JSON：
{{
  "user_intent": "用户的操作意图（一句话，如'提供了 input schema 定义'）",
  "blocks": [
    {{
      "suggested_filename": "语义化文件名.ext（如 input-schema.json, competitor-prompt.md）",
      "content": "该块的完整内容（不要截断）",
      "block_type": "json-schema | prompt | knowledge | example | config | other"
    }}
  ]
}}

规则：
1. 分离"用户说明文字"和"内容体"，说明文字融入 user_intent，不要存为块
2. 如果内容本身是一个整体（如完整 JSON），不要强行拆分，存为单个块
3. filename 要语义化，扩展名准确（JSON 内容用 .json，prompt/文档用 .md，纯文本用 .txt）
4. content 必须是完整原文，不能截断或摘要
5. 只返回 JSON，不要其他文字"""

            parse_config = llm_gateway.resolve_config(db, "studio.ingest_parse")
            parse_result, _ = await llm_gateway.chat(
                parse_config,
                [{"role": "user", "content": parse_prompt}],
                temperature=0.1,
                max_tokens=8192,
            )

            # 从 LLM 响应中提取 JSON
            parse_data = _extract_json(parse_result)
            if not parse_data or "blocks" not in parse_data:
                yield _sse("error", {"message": "内容分析失败：无法解析 LLM 返回结果"})
                return

            user_intent = parse_data.get("user_intent", "用户粘贴了长文本内容")
            blocks = parse_data["blocks"]

            if not blocks:
                yield _sse("error", {"message": "内容分析失败：未识别到有效内容块"})
                return

            # ── 存储每个块为子文件 ──
            yield _sse("stage", {"stage": "ingest_saving", "label": "存储子文件..."})

            saved_files = []
            files = list(skill.source_files or [])
            for block in blocks:
                filename = Path(block.get("suggested_filename", "untitled.txt")).name
                if not filename or filename.startswith("."):
                    filename = "untitled.txt"
                block_content = block.get("content", "")
                block_type = block.get("block_type", "other")

                path = _safe_file_path(skill_id, filename)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(block_content, encoding="utf-8")

                size = len(block_content.encode("utf-8"))
                rel_path = f"uploads/skills/{skill_id}/{path.name}"

                # 更新 source_files（去重）
                found = False
                for f in files:
                    if f.get("filename") == path.name:
                        f["size"] = size
                        found = True
                        break
                if not found:
                    files.append({
                        "filename": path.name,
                        "path": rel_path,
                        "size": size,
                        "category": _infer_category(path.name),
                    })
                saved_files.append({
                    "filename": path.name,
                    "block_type": block_type,
                    "size": size,
                })

            skill.source_files = files
            db.commit()

            yield _sse("ingest_files_saved", {"files": [f["filename"] for f in saved_files]})

            # ── Step B: 关系分析 ──
            yield _sse("stage", {"stage": "ingest_analyzing", "label": "分析与 Skill 的关系..."})

            files_summary = "\n".join(
                f"- {f['filename']}（{f['block_type']}）：{blocks[i].get('content', '')[:500]}"
                for i, f in enumerate(saved_files)
            )

            analyze_prompt = f"""你是 Skill 架构分析助手。以下子文件刚从用户粘贴内容中拆分出来，请分析每个文件与当前 Skill 的关系。

当前 Skill：
- 名称：{skill.name}
- 描述：{skill.description or '无'}
- 主 Prompt（前 2000 字符）：
{system_prompt_preview}

已存储的子文件：
{files_summary}

用户原始意图：{user_intent}

请返回 JSON：
{{
  "blocks": [
    {{
      "filename": "input-schema.json",
      "relation": "该文件定义了 Skill 的输入数据结构",
      "suggested_role": "input_definition | knowledge | reference | example"
    }}
  ],
  "summary": "一段完整的摘要（包含用户意图 + 各文件角色 + 建议操作），将发送给 Studio Agent 继续交互"
}}

summary 格式要求：
- 开头复述用户意图
- 列出每个文件名及其角色
- 提出下一步建议（如"建议在主 prompt 中引用 input-schema.json 作为输入定义"）
- 只返回 JSON，不要其他文字"""

            analyze_config = llm_gateway.resolve_config(db, "studio.ingest_analyze")
            analyze_result, _ = await llm_gateway.chat(
                analyze_config,
                [{"role": "user", "content": analyze_prompt}],
                temperature=0.1,
                max_tokens=2048,
            )

            analyze_data = _extract_json(analyze_result)
            if not analyze_data:
                # 分析失败但文件已保存，返回基础结果
                file_list = "、".join(f["filename"] for f in saved_files)
                fallback_summary = f"{user_intent}。已存储为子文件：{file_list}。请检查文件内容并决定如何在 Skill 中使用。"
                # 接入 memo（无 suggested_role）
                _notify_memo(db, skill_id, user_intent, saved_files, user)
                yield _sse("ingest_result", {
                    "user_intent": user_intent,
                    "blocks": saved_files,
                    "summary": fallback_summary,
                })
                return

            # 合并关系信息到 blocks
            relation_map = {b["filename"]: b for b in analyze_data.get("blocks", [])}
            for f in saved_files:
                rel = relation_map.get(f["filename"], {})
                f["relation"] = rel.get("relation", "")
                f["suggested_role"] = rel.get("suggested_role", "")

            summary = analyze_data.get("summary", "")
            if not summary:
                file_list = "、".join(f["filename"] for f in saved_files)
                summary = f"{user_intent}。已存储为子文件：{file_list}。"

            # 接入 memo（含 suggested_role）
            _notify_memo(db, skill_id, user_intent, saved_files, user)

            yield _sse("ingest_result", {
                "user_intent": user_intent,
                "blocks": saved_files,
                "summary": summary,
            })

        except Exception as e:
            logger.exception("ingest-paste pipeline error")
            yield _sse("error", {"message": f"长文本分析失败：{str(e)}"})

    return StreamingResponse(generate(), media_type="text/event-stream")


def _notify_memo(db: Session, skill_id: int, user_intent: str, saved_files: list[dict], user: User):
    """将 ingest 结果写入 memo，推进任务、记录日志。失败不阻塞主流程。"""
    try:
        from app.services.skill_memo_service import ingest_from_paste
        ingest_from_paste(db, skill_id, user_intent, saved_files, user.id)
    except Exception:
        logger.warning("ingest_paste memo notification failed", exc_info=True)


def _extract_json(text: str) -> dict | None:
    """从 LLM 响应中提取 JSON 对象（兼容 ```json 代码块和裸 JSON）。"""
    import json
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    if start >= 0:
        end = text.rfind("}")
        if end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    return None


# ─── Usage stats (super_admin only) ──────────────────────────────────────────

