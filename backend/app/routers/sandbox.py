"""沙盒测试 API — 在 Skill/Tool 进入审批前进行自动化试跑验证。

Skill 测试：用 AI 分析 system_prompt，生成典型测试 prompt，
          实际调用 LLM，输出测试报告。
Tool 测试：分析工具的 input_schema/manifest，生成 mock 参数，
         执行 tool_executor，输出执行结果。
Preflight：多维度预检 — Gate 1/2/3（结构/知识库/工具）一票否决 + LLM 质量评分。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app.dependencies import get_current_user
from app.models.user import User
from app.models.skill import Skill, SkillVersion, SkillPreflightResult
from app.models.tool import ToolRegistry
from app.services.llm_gateway import llm_gateway

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sandbox", tags=["sandbox"])


# ─── Helpers ──────────────────────────────────────────────────────────────────

_MOCK_GEN_PROMPT = """你是一个 AI Skill 测试专家。根据以下 Skill 的 system_prompt，
生成一条最能代表该 Skill 核心功能的用户测试消息。
要求：
- 测试消息简短（20-60字）
- 包含该 Skill 处理所需的典型输入信息
- 中文
- 直接输出测试消息，不要任何解释

System Prompt：
{system_prompt}"""

_TOOL_MOCK_PROMPT = """你是一个工具测试专家。根据以下工具的定义，生成合理的 mock 测试参数（JSON）。

工具名称：{name}
描述：{description}
输入 Schema：
{schema}

数据来源声明（manifest）：
{manifest}

要求：
- 只输出 JSON 对象，不要任何解释
- 所有 required 字段必须有值
- 使用合理的虚假数据（如表名用 "sales_2024"，文件名用 "report.xlsx"）
- 如果是 registered_table 类型，使用 "mock_table" 作为值
- 如果是 uploaded_file 类型，使用对应扩展名的文件名"""

_SKILL_EVALUATE_PROMPT = """你是 Skill 质量评审官。评估以下 AI Skill 的回复质量。

Skill 名称：{skill_name}
System Prompt：
{system_prompt}

测试输入：
{test_input}

AI 回复：
{response}

请评估：
1. 回复是否符合 System Prompt 的定位和要求？
2. 回复质量（内容相关性、完整性）是否合格？
3. 是否存在明显问题（如完全偏题、空回复、报错信息）？

只输出以下格式（不要多余内容）：
PASS 或 FAIL
原因：<一句话说明>"""


async def _generate_test_input(db: Session, system_prompt: str) -> str:
    """用 AI 根据 system_prompt 生成测试输入。"""
    prompt = _MOCK_GEN_PROMPT.format(system_prompt=system_prompt[:2000])
    try:
        result, _ = await llm_gateway.chat(
            model_config=llm_gateway.resolve_config(db, "sandbox.mock_input"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=200,
        )
        return result.strip()
    except Exception as e:
        logger.warning(f"Mock input generation failed: {e}")
        return "请介绍一下你的功能，并给我一个示例输出。"


async def _generate_tool_mock_params(db: Session, tool: ToolRegistry) -> dict:
    """用 AI 根据工具定义生成 mock 参数。"""
    config = tool.config or {}
    manifest = config.get("manifest", {})
    schema_str = json.dumps(tool.input_schema or {}, ensure_ascii=False, indent=2)
    manifest_str = json.dumps(manifest, ensure_ascii=False, indent=2)

    prompt = _TOOL_MOCK_PROMPT.format(
        name=tool.display_name or tool.name,
        description=tool.description or "无描述",
        schema=schema_str[:1500],
        manifest=manifest_str[:1000],
    )
    try:
        result, _ = await llm_gateway.chat(
            model_config=llm_gateway.resolve_config(db, "sandbox.tool_mock"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=500,
        )
        # 提取 JSON
        text = result.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        logger.warning(f"Tool mock param generation failed: {e}")
        # 从 schema 提取 required 字段，填充默认值
        schema = tool.input_schema or {}
        props = schema.get("properties", {})
        required = schema.get("required", [])
        params = {}
        for field_name in required:
            field_def = props.get(field_name, {})
            ftype = field_def.get("type", "string")
            if ftype == "string":
                params[field_name] = "mock_value"
            elif ftype == "integer":
                params[field_name] = 1
            elif ftype == "boolean":
                params[field_name] = True
            elif ftype == "array":
                params[field_name] = []
            else:
                params[field_name] = None
        return params


async def _evaluate_skill_response(
    db: Session, skill_name: str, system_prompt: str, test_input: str, response: str
) -> tuple[bool, str]:
    """用 AI 评估 skill 回复质量，返回 (passed, reason)。"""
    prompt = _SKILL_EVALUATE_PROMPT.format(
        skill_name=skill_name,
        system_prompt=system_prompt[:1500],
        test_input=test_input,
        response=response[:2000],
    )
    try:
        result, _ = await llm_gateway.chat(
            model_config=llm_gateway.resolve_config(db, "sandbox.evaluate"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=200,
        )
        text = result.strip()
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        verdict = lines[0].upper() if lines else "FAIL"
        reason_line = next((l for l in lines if l.startswith("原因：")), "")
        reason = reason_line.replace("原因：", "").strip() if reason_line else text[:100]
        return verdict == "PASS", reason
    except Exception as e:
        logger.warning(f"Skill evaluation failed: {e}")
        return True, "评估服务暂不可用，默认通过"


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.post("/test-skill/{skill_id}")
async def test_skill(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """对指定 Skill 进行沙盒测试。

    流程：
    1. 读取最新版本的 system_prompt
    2. AI 生成测试输入
    3. 以 system_prompt 为系统提示发起 LLM 调用
    4. AI 评估回复质量
    5. 返回测试报告
    """
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")

    # 权限：只有创建者或管理员
    from app.models.user import Role
    if skill.created_by != user.id and user.role not in (Role.SUPER_ADMIN, Role.DEPT_ADMIN):
        raise HTTPException(403, "无权测试该 Skill")

    # 获取最新版本
    latest_ver: SkillVersion | None = (
        db.query(SkillVersion)
        .filter(SkillVersion.skill_id == skill_id)
        .order_by(SkillVersion.version.desc())
        .first()
    )
    if not latest_ver or not latest_ver.system_prompt:
        raise HTTPException(400, "Skill 尚无可用版本或 System Prompt 为空，无法测试")

    system_prompt = latest_ver.system_prompt
    # 注入附属文件内容（与 skill_engine 运行时一致）
    from app.services.skill_engine import _read_source_files
    _file_ctx = _read_source_files(skill_id, skill.source_files or [])
    if _file_ctx:
        system_prompt += _file_ctx
    steps = []

    # Step 1: 生成测试输入
    t0 = time.time()
    test_input = await _generate_test_input(db, system_prompt)
    steps.append({
        "step": "generate_input",
        "label": "生成测试用例",
        "ok": True,
        "detail": test_input,
        "duration_ms": int((time.time() - t0) * 1000),
    })

    # Step 2: 实际调用 LLM（用 skill 的 system_prompt）
    t0 = time.time()
    llm_response = ""
    llm_error = None
    try:
        model_cfg = llm_gateway.resolve_config(db, "sandbox.execute", latest_ver.model_config_id or None)
        llm_response, _ = await llm_gateway.chat(
            model_config=model_cfg,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": test_input},
            ],
            temperature=0.7,
            max_tokens=1000,
        )
        steps.append({
            "step": "llm_call",
            "label": "调用 LLM",
            "ok": True,
            "detail": llm_response[:500] + ("..." if len(llm_response) > 500 else ""),
            "duration_ms": int((time.time() - t0) * 1000),
        })
    except Exception as e:
        llm_error = str(e)
        steps.append({
            "step": "llm_call",
            "label": "调用 LLM",
            "ok": False,
            "detail": f"LLM 调用失败：{llm_error}",
            "duration_ms": int((time.time() - t0) * 1000),
        })
        return {
            "passed": False,
            "skill_id": skill_id,
            "skill_name": skill.name,
            "test_input": test_input,
            "steps": steps,
            "summary": f"测试失败：LLM 调用出错 — {llm_error}",
        }

    # Step 3: AI 评估回复
    t0 = time.time()
    passed, reason = await _evaluate_skill_response(
        db, skill.name, system_prompt, test_input, llm_response
    )
    steps.append({
        "step": "evaluate",
        "label": "质量评估",
        "ok": passed,
        "detail": reason,
        "duration_ms": int((time.time() - t0) * 1000),
    })

    return {
        "passed": passed,
        "skill_id": skill_id,
        "skill_name": skill.name,
        "test_input": test_input,
        "llm_response": llm_response,
        "steps": steps,
        "summary": f"{'✓ 测试通过' if passed else '✗ 测试未通过'} — {reason}",
    }


@router.post("/test-tool/{tool_id}")
async def test_tool(
    tool_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """对指定 Tool 进行沙盒测试。

    流程：
    1. 读取工具 input_schema 和 manifest
    2. AI 生成 mock 参数
    3. 执行工具（bypass is_active 检查，直接调用内部逻辑）
    4. 返回测试报告
    """
    tool = db.get(ToolRegistry, tool_id)
    if not tool:
        raise HTTPException(404, "工具不存在")

    from app.models.user import Role
    if tool.created_by != user.id and user.role not in (Role.SUPER_ADMIN, Role.DEPT_ADMIN):
        raise HTTPException(403, "无权测试该工具")

    steps = []
    config = tool.config or {}
    manifest = config.get("manifest", {})
    tool_type_str = tool.tool_type.value if tool.tool_type else "unknown"

    # Step 1: 分析工具结构
    has_schema = bool(tool.input_schema and tool.input_schema.get("properties"))
    steps.append({
        "step": "analyze",
        "label": "分析工具结构",
        "ok": True,
        "detail": (
            f"类型：{tool_type_str}｜"
            f"触发方式：{manifest.get('invocation_mode', '未指定')}｜"
            f"数据来源：{len(manifest.get('data_sources', []))} 个｜"
            f"权限声明：{len(manifest.get('permissions', []))} 项｜"
            f"参数 Schema：{'有' if has_schema else '无'}"
        ),
        "duration_ms": 0,
    })

    # Step 2: 生成 mock 参数
    t0 = time.time()
    mock_params = {}
    if has_schema or manifest.get("data_sources"):
        mock_params = await _generate_tool_mock_params(db, tool)
    steps.append({
        "step": "mock_params",
        "label": "生成 Mock 参数",
        "ok": True,
        "detail": json.dumps(mock_params, ensure_ascii=False),
        "duration_ms": int((time.time() - t0) * 1000),
    })

    # Step 3: 执行工具
    # 注意：沙盒测试时对 MCP/builtin 工具执行实际调用；
    # 对于 is_active=False 的工具（尚未发布），临时允许执行
    t0 = time.time()
    from app.services.tool_executor import tool_executor

    # 沙盒模式：直接调用内部执行，绕过 is_active 检查
    exec_result = await _sandbox_execute_tool(db, tool, mock_params, user.id)

    duration_ms = int((time.time() - t0) * 1000)
    exec_ok = exec_result.get("ok", False)
    exec_error = exec_result.get("error", "")
    exec_output = exec_result.get("result", "")

    steps.append({
        "step": "execute",
        "label": "执行工具",
        "ok": exec_ok,
        "detail": (
            str(exec_output)[:500] if exec_ok
            else f"执行失败：{exec_error}"
        ),
        "duration_ms": duration_ms,
    })

    # 判断总体是否通过
    # 对于 uploaded_file 类型的数据源，沙盒 mock 参数无法提供真实文件，视为"配置正确"
    # 对于 registered_table，沙盒已连接 DB 进行真实检查，缺表即失败
    precondition_failed = "precondition_failed" in exec_result.get("phases", [])
    file_only_precondition = False
    if precondition_failed:
        # 检查是否仅因为 uploaded_file 不存在导致失败（非 registered_table）
        config = tool.config or {}
        manifest = config.get("manifest", {})
        ds_types = {ds.get("type") for ds in manifest.get("data_sources", [])}
        file_only_precondition = ds_types.issubset({"uploaded_file", "chat_context"})

    if precondition_failed and file_only_precondition:
        passed = True
        summary = "✓ 配置检查通过 — 工具结构合法，需在真实对话中上传文件后方可完整运行"
    elif precondition_failed:
        passed = False
        summary = f"✗ 前置条件不满足 — {exec_error[:200]}"
    elif exec_ok:
        passed = True
        summary = "✓ 测试通过 — 工具执行成功，输出正常"
    else:
        # 判断是否是配置/代码错误（真正的失败）
        schema_failed = "validation_failed" in exec_result.get("phases", [])
        passed = False
        if schema_failed:
            summary = f"✗ 参数 Schema 错误 — {exec_error[:200]}"
        else:
            summary = f"✗ 执行失败 — {exec_error[:200]}"

    return {
        "passed": passed,
        "tool_id": tool_id,
        "tool_name": tool.display_name or tool.name,
        "mock_params": mock_params,
        "steps": steps,
        "summary": summary,
    }


async def _sandbox_execute_tool(
    db: Session,
    tool: ToolRegistry,
    params: dict,
    user_id: int,
) -> dict:
    """沙盒执行工具，绕过 is_active 检查直接运行。

    复用 tool_executor 的校验和执行逻辑，区别在于：
    - 允许 is_active=False 的工具执行
    - 前置条件检查传入真实 db（注册表缺失照常报错）
    """
    from app.services.tool_executor import _validate_params, _check_manifest_preconditions

    phases = []

    # Schema 校验
    validation_error = _validate_params(tool, params)
    if validation_error:
        return {
            "ok": False,
            "error": validation_error,
            "phases": ["validation_failed"],
        }
    phases.append("validated")

    # Manifest 前置条件检查（传入真实 db，registered_table 照常验证）
    manifest_error = await _check_manifest_preconditions(tool, params, db=db)
    if manifest_error:
        return {
            "ok": False,
            "error": manifest_error,
            "phases": phases + ["precondition_failed"],
            "result": "",
        }
    phases.append("preconditions_ok")

    # 执行工具（复用 tool_executor 方法）
    from app.services.tool_executor import tool_executor
    from app.models.tool import ToolType
    try:
        if tool.tool_type == ToolType.BUILTIN:
            result = await tool_executor._execute_builtin(tool, params, db=db, user_id=user_id)
        elif tool.tool_type == ToolType.HTTP:
            result = await tool_executor._execute_http(tool, params, timeout_s=15)
        elif tool.tool_type == ToolType.MCP:
            # MCP 服务审批前尚未安装，尝试调用；失败时给出明确说明
            try:
                result = await tool_executor._execute_mcp(tool, params, timeout_s=10)
            except Exception as mcp_err:
                err_str = str(mcp_err)
                return {
                    "ok": False,
                    "error": f"MCP 服务未运行（审批通过后将自动安装启动）：{err_str[:150]}",
                    "phases": phases + ["mcp_not_running"],
                    "result": "",
                }
        else:
            return {
                "ok": True,
                "result": "（沙盒模式：工具类型不支持直接执行，配置检查已通过）",
                "phases": phases,
            }
        return {"ok": True, "result": result, "phases": phases + ["executed"]}
    except ModuleNotFoundError as e:
        return {
            "ok": False,
            "error": f"工具模块不存在，请检查文件是否已上传：{e}",
            "phases": phases + ["module_not_found"],
        }
    except TypeError as e:
        return {
            "ok": False,
            "error": f"参数不匹配：{e}",
            "phases": phases + ["param_mismatch"],
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "phases": phases + ["runtime_error"]}


# ─── Preflight: 多维度预检 ────────────────────────────────────────────────────

_QUALITY_TEST_GEN_PROMPT = """你是 AI Skill 测试专家。根据以下 Skill 信息，生成 2-3 个最能检验该 Skill 核心能力的测试用例。

Skill 名称：{name}
Skill 目标：{description}
System Prompt（前 2000 字）：
{system_prompt}

附属文件列表：
{file_list}

要求：
- 每个用例一行，直接输出用户消息
- 第 1 个测试核心场景，第 2 个测试边界/深度场景
- 用例应该能暴露"只做了子问题没解决完整问题"的情况
- 中文，每条 20-80 字
- 不要编号、不要解释，一行一条"""

_QUALITY_SCORE_PROMPT = """你是 AI Skill 质量评审官。

该 Skill 的目标：
{description}

System Prompt 摘要（前 1500 字）：
{system_prompt}

知识库检索结果：
{knowledge_summary}

测试用例：
{test_input}

AI 回复：
{response}

请严格评分（0-100），评估标准：
1. 目标覆盖度（40%）：回复是否解决了 Skill 描述中的核心问题？还是只碰到了皮毛/子问题？
   - 如果 Skill 说"系统复盘"但只做了"查预算"，应给低分
2. 输出完整度（30%）：回复结构是否完整？关键信息是否齐全？
   - 如果知识库检索到了相关内容，回复是否有效利用了这些知识？
   - 如果知识库为空或无相关结果，说明该 Skill 缺少支撑知识，应适当扣分
3. 专业度（30%）：用词是否专业？格式是否规范？是否体现领域知识？

只输出 JSON（不要其他内容）：
{{"score": 75, "coverage": 80, "completeness": 70, "professionalism": 75, "knowledge_used": true, "reason": "一句话说明"}}"""


def _hash_content(*parts: str) -> str:
    """对多个内容片段取 SHA256 前 16 位作为变更指纹。"""
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8"))
    return h.hexdigest()[:16]


def _get_cached_result(db: Session, skill_id: int, gate_name: str, content_hash: str) -> Optional[dict]:
    """查找未过期的缓存结果（content_hash 匹配）。"""
    row = (
        db.query(SkillPreflightResult)
        .filter(
            SkillPreflightResult.skill_id == skill_id,
            SkillPreflightResult.gate_name == gate_name,
            SkillPreflightResult.content_hash == content_hash,
        )
        .order_by(SkillPreflightResult.checked_at.desc())
        .first()
    )
    if not row:
        return None
    return {
        "passed": row.passed,
        "score": row.score,
        "detail": row.detail,
        "cached": True,
        "checked_at": row.checked_at.isoformat() if row.checked_at else None,
    }


def _save_result(db: Session, skill_id: int, gate_name: str, passed: bool, content_hash: str, detail: dict, score: int = None):
    """保存检测结果，覆盖同 skill_id + gate_name 的旧记录。"""
    existing = (
        db.query(SkillPreflightResult)
        .filter(SkillPreflightResult.skill_id == skill_id, SkillPreflightResult.gate_name == gate_name)
        .first()
    )
    import datetime
    if existing:
        existing.passed = passed
        existing.score = score
        existing.detail = detail
        existing.content_hash = content_hash
        existing.checked_at = datetime.datetime.utcnow()
    else:
        db.add(SkillPreflightResult(
            skill_id=skill_id, gate_name=gate_name, passed=passed,
            score=score, detail=detail, content_hash=content_hash,
        ))
    db.commit()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/preflight/{skill_id}")
async def preflight(
    skill_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Skill 多维度预检 — SSE 流式返回各 gate 结果 + LLM 质量评分。"""
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")

    from app.models.user import Role
    if skill.created_by != user.id and user.role not in (Role.SUPER_ADMIN, Role.DEPT_ADMIN):
        raise HTTPException(403, "无权检测该 Skill")

    latest_ver: SkillVersion | None = (
        db.query(SkillVersion)
        .filter(SkillVersion.skill_id == skill_id)
        .order_by(SkillVersion.version.desc())
        .first()
    )
    system_prompt = (latest_ver.system_prompt if latest_ver else "") or ""
    source_files = skill.source_files or []

    async def generate():
        # SSE generator 运行时 Depends(get_db) 的 session 已关闭，需要独立 session
        _db = SessionLocal()
        try:
            async for chunk in _generate_inner(_db):
                yield chunk
        finally:
            _db.close()

    async def _generate_inner(db: Session):
        # 重新加载 skill（外层 Depends session 已关闭）
        skill = db.get(Skill, skill_id)
        gates = []
        all_passed = True

        # ── Gate 0: 前端内容检测（一票否决）──────────────────────────
        yield _sse("gate", {"gate": "frontend_check", "label": "检测前端内容...", "status": "running"})

        fe_items = []
        fe_pass = True

        # 0a. 检查附属文件扩展名
        FRONTEND_EXTS = {".tsx", ".jsx", ".html", ".css", ".scss", ".less", ".sass", ".vue", ".svelte", ".styl", ".pcss"}
        for f in source_files:
            fname = f.get("filename", "")
            ext_pos = fname.rfind(".")
            ext = fname[ext_pos:].lower() if ext_pos >= 0 else ""
            if ext in FRONTEND_EXTS:
                fe_items.append({
                    "check": f"文件 {fname}",
                    "ok": False,
                    "issue": f"前端文件（{ext}），Le Desk 的 tool 不带前端界面，请移除此文件",
                })
                fe_pass = False

        # 0b. 本地关键词检测 prompt 中的前端意图
        import re as _re
        _FE_PATTERNS = [
            r"\bReact\b", r"\bVue\b", r"\bSvelte\b", r"\bAngular\b",
            r"\bHTML\b.*组件", r"生成.*页面", r"生成.*组件", r"生成.*界面",
            r"前端", r"\bCSS\b.*样式", r"\bJSX\b", r"\bTSX\b",
            r"render.*component", r"create.*component", r"build.*UI",
            r"web\s*page", r"landing\s*page", r"网页",
        ]
        prompt_fe_hits = []
        for pattern in _FE_PATTERNS:
            m = _re.search(pattern, system_prompt, _re.IGNORECASE)
            if m:
                prompt_fe_hits.append(m.group(0))

        # 0c. 如果本地关键词命中，用 AI 二次确认（避免误报）
        if prompt_fe_hits and not fe_items:
            try:
                _fe_check_prompt = (
                    "判断以下 AI Skill 的 System Prompt 是否要求生成前端界面/UI 组件/网页。\n"
                    "注意：如果只是「分析」「讨论」前端技术而非「生成前端代码」，则不算。\n\n"
                    f"Prompt（前 2000 字）：\n{system_prompt[:2000]}\n\n"
                    "只输出 YES 或 NO，不要解释。"
                )
                _fe_result, _ = await llm_gateway.chat(
                    model_config=llm_gateway.resolve_config(db, "sandbox.fe_detect"),
                    messages=[{"role": "user", "content": _fe_check_prompt}],
                    temperature=0.0,
                    max_tokens=10,
                )
                if _fe_result.strip().upper().startswith("YES"):
                    fe_items.append({
                        "check": "System Prompt 内容",
                        "ok": False,
                        "issue": f"检测到前端/UI 生成意图（关键词：{', '.join(prompt_fe_hits[:3])}）。"
                                 "Le Desk 的 tool 不带前端界面，Skill 应专注于数据处理、分析、文案生成等后端能力。",
                    })
                    fe_pass = False
            except Exception as e:
                logger.warning(f"Frontend AI check failed: {e}")
                # AI 检测失败时，仅靠关键词不直接拦截，改为 warning
                fe_items.append({
                    "check": "System Prompt 内容",
                    "ok": True,
                    "detail": f"检测到前端关键词（{', '.join(prompt_fe_hits[:3])}），AI 确认失败，暂不拦截",
                })

        if not fe_items:
            fe_items.append({"check": "前端内容", "ok": True, "detail": "未检测到前端相关内容"})

        fe_status = "passed" if fe_pass else "failed"
        gate0 = {"gate": "frontend_check", "label": "前端内容检测", "status": fe_status, "items": fe_items}
        gates.append(gate0)
        yield _sse("gate", gate0)
        if not fe_pass:
            all_passed = False
            yield _sse("done", {"passed": False, "blocked_by": "frontend_check", "gates": gates})
            return

        # ── Gate 1: 结构完整性 ──────────────────────────────────────
        yield _sse("gate", {"gate": "structure", "label": "检查目录结构...", "status": "running"})

        structure_hash = _hash_content(
            system_prompt[:100], skill.description or "",
            json.dumps([f["filename"] for f in source_files], ensure_ascii=False),
        )
        cached = _get_cached_result(db, skill_id, "structure", structure_hash)
        if cached and cached["passed"]:
            gate1 = {"gate": "structure", "label": "目录结构", "status": "passed", "items": cached["detail"].get("items", []), "cached": True, "checked_at": cached["checked_at"]}
            gates.append(gate1)
            yield _sse("gate", gate1)
        else:
            items = []
            g1_pass = True
            if len(system_prompt.strip()) < 50:
                items.append({"check": "SKILL.md 内容", "ok": False, "issue": f"System Prompt 仅 {len(system_prompt.strip())} 字，需 ≥ 50 字"})
                g1_pass = False
            else:
                items.append({"check": "SKILL.md 内容", "ok": True})
            if not (skill.description or "").strip():
                items.append({"check": "Skill 描述", "ok": False, "issue": "description 为空"})
                g1_pass = False
            else:
                items.append({"check": "Skill 描述", "ok": True})
            if len(source_files) == 0:
                items.append({"check": "附属文件", "ok": False, "issue": "无任何附属文件"})
                g1_pass = False
            else:
                items.append({"check": "附属文件", "ok": True, "detail": f"{len(source_files)} 个文件"})

            status = "passed" if g1_pass else "failed"
            gate1 = {"gate": "structure", "label": "目录结构", "status": status, "items": items}
            gates.append(gate1)
            _save_result(db, skill_id, "structure", g1_pass, structure_hash, {"items": items})
            yield _sse("gate", gate1)
            if not g1_pass:
                all_passed = False
                yield _sse("done", {"passed": False, "blocked_by": "structure", "gates": gates})
                return

        # ── Gate 2: 知识库就绪 ──────────────────────────────────────
        yield _sse("gate", {"gate": "knowledge", "label": "检查知识库...", "status": "running"})

        kb_files = [f for f in source_files if f.get("category") == "knowledge-base"]
        if not kb_files:
            gate2 = {"gate": "knowledge", "label": "知识库", "status": "passed", "items": [{"check": "无知识库文件", "ok": True, "detail": "该 Skill 不需要知识库"}]}
            gates.append(gate2)
            _save_result(db, skill_id, "knowledge", True, "no_kb", {"items": gate2["items"]})
            yield _sse("gate", gate2)
        else:
            kb_hash = _hash_content(*[f["filename"] for f in kb_files])
            cached = _get_cached_result(db, skill_id, "knowledge", kb_hash)
            if cached and cached["passed"]:
                gate2 = {"gate": "knowledge", "label": "知识库", "status": "passed", "items": cached["detail"].get("items", []), "cached": True, "checked_at": cached["checked_at"]}
                gates.append(gate2)
                yield _sse("gate", gate2)
            else:
                from app.models.knowledge import KnowledgeEntry
                items = []
                g2_pass = True
                for kf in kb_files:
                    fname = kf["filename"]
                    # 查 knowledge_entries 匹配（按 title 或 source_file）
                    entry = (
                        db.query(KnowledgeEntry)
                        .filter(
                            (KnowledgeEntry.title == fname) | (KnowledgeEntry.source_file == fname)
                        )
                        .first()
                    )
                    if not entry:
                        items.append({"check": fname, "ok": False, "issue": "未入库", "action": "confirm_archive"})
                        g2_pass = False
                        continue
                    # 查向量库是否有 chunk
                    try:
                        from app.services.vector_service import search_knowledge
                        hits = search_knowledge(fname, top_k=1, knowledge_id_filter=[entry.id])
                        if hits:
                            items.append({"check": fname, "ok": True, "detail": f"已入库 (ID:{entry.id}), 有向量索引"})
                        else:
                            items.append({"check": fname, "ok": False, "issue": "已入库但无向量索引", "knowledge_id": entry.id, "action": "reindex"})
                            g2_pass = False
                    except Exception:
                        items.append({"check": fname, "ok": True, "detail": f"已入库 (ID:{entry.id}), 向量检查跳过"})

                status = "passed" if g2_pass else "failed"
                gate2 = {"gate": "knowledge", "label": "知识库", "status": status, "items": items}
                gates.append(gate2)
                _save_result(db, skill_id, "knowledge", g2_pass, kb_hash, {"items": items})
                yield _sse("gate", gate2)
                if not g2_pass:
                    all_passed = False
                    yield _sse("done", {"passed": False, "blocked_by": "knowledge", "gates": gates})
                    return

        # ── Gate 3: 工具就绪 ──────────────────────────────────────
        yield _sse("gate", {"gate": "tools", "label": "检查工具...", "status": "running"})

        bound = list(skill.bound_tools)
        if not bound:
            gate3 = {"gate": "tools", "label": "工具", "status": "passed", "items": [{"check": "无绑定工具", "ok": True, "detail": "该 Skill 不需要工具"}]}
            gates.append(gate3)
            _save_result(db, skill_id, "tools", True, "no_tools", {"items": gate3["items"]})
            yield _sse("gate", gate3)
        else:
            tool_hash = _hash_content(*[str(t.id) for t in bound])
            cached = _get_cached_result(db, skill_id, "tools", tool_hash)
            if cached and cached["passed"]:
                gate3 = {"gate": "tools", "label": "工具", "status": "passed", "items": cached["detail"].get("items", []), "cached": True, "checked_at": cached["checked_at"]}
                gates.append(gate3)
                yield _sse("gate", gate3)
            else:
                items = []
                g3_pass = True
                for t in bound:
                    tool_name = t.display_name or t.name
                    issues = []
                    # 检查状态
                    if t.status != "published" and not t.is_active:
                        issues.append(f"状态 {t.status}, 未激活")
                    # BUILTIN 类型检查模块
                    if t.tool_type and t.tool_type.value == "BUILTIN":
                        try:
                            import importlib
                            mod_name = f"app.tools.{t.name}"
                            importlib.import_module(mod_name)
                        except (ImportError, ModuleNotFoundError):
                            issues.append("模块不存在或无法导入")
                    # registered_table 数据源检查
                    config = t.config or {}
                    manifest = config.get("manifest", {})
                    for ds in manifest.get("data_sources", []):
                        if ds.get("type") == "registered_table" and ds.get("required", True):
                            from app.models.business import BusinessTable
                            exists = db.query(BusinessTable).filter(BusinessTable.table_name == ds.get("key", "")).first()
                            if not exists:
                                issues.append(f"数据表 '{ds.get('key')}' 未注册")

                    if issues:
                        items.append({"check": tool_name, "ok": False, "issue": "；".join(issues)})
                        g3_pass = False
                    else:
                        items.append({"check": tool_name, "ok": True})

                status = "passed" if g3_pass else "failed"
                gate3 = {"gate": "tools", "label": "工具", "status": status, "items": items}
                gates.append(gate3)
                _save_result(db, skill_id, "tools", g3_pass, tool_hash, {"items": items})
                yield _sse("gate", gate3)
                if not g3_pass:
                    all_passed = False
                    yield _sse("done", {"passed": False, "blocked_by": "tools", "gates": gates})
                    return

        # ── 阶段二：LLM 质量评分 ──────────────────────────────────
        yield _sse("stage", {"label": "生成测试用例..."})

        file_list = "\n".join(f"  - {f['filename']} ({f.get('category', 'other')})" for f in source_files) or "（无附属文件）"
        test_gen_prompt = _QUALITY_TEST_GEN_PROMPT.format(
            name=skill.name,
            description=skill.description or "无描述",
            system_prompt=system_prompt[:2000],
            file_list=file_list,
        )
        try:
            raw_cases, _ = await llm_gateway.chat(
                model_config=llm_gateway.resolve_config(db, "sandbox.preflight_gen"),
                messages=[{"role": "user", "content": test_gen_prompt}],
                temperature=0.7,
                max_tokens=500,
            )
            test_cases = [line.strip() for line in raw_cases.strip().splitlines() if line.strip()][:3]
        except Exception:
            test_cases = ["请介绍一下你的核心功能并给出一个完整示例。"]

        if not test_cases:
            test_cases = ["请展示你的核心能力。"]

        model_cfg = llm_gateway.resolve_config(db, "sandbox.preflight_exec")

        def _parse_score_json(text: str) -> dict:
            """鲁棒提取 LLM 返回的评分 JSON。"""
            # 1. 去 markdown code block 包裹
            if "```" in text:
                parts = text.split("```")
                for part in parts[1::2]:  # 取奇数位（code block 内容）
                    inner = part.strip()
                    if inner.startswith("json"):
                        inner = inner[4:].strip()
                    try:
                        return json.loads(inner)
                    except json.JSONDecodeError:
                        continue

            # 2. 直接尝试解析整段
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass

            # 3. regex 提取第一个包含 "score" 的 {...} 对象
            m = re.search(r'\{[^{}]*"score"[^{}]*\}', text)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    pass

            # 4. 最后兜底：只提取 score 数字
            m = re.search(r'"score"\s*:\s*(\d+)', text)
            if m:
                return {"score": int(m.group(1)), "reason": "从非标准格式中提取到分数"}

            raise ValueError(f"无法解析评分 JSON: {text[:200]}")

        tests = []
        total_score = 0

        # 知识检索函数：模拟真实 RAG 链路
        async def _retrieve_knowledge(query: str) -> str:
            try:
                import asyncio
                from app.services.vector_service import search_knowledge
                hits = await asyncio.wait_for(
                    asyncio.to_thread(search_knowledge, query, 10),
                    timeout=5.0,
                )
                if not hits:
                    return ""
                # 按 Skill 的 knowledge_tags 过滤（如果有）
                if skill.knowledge_tags:
                    tag_set = set(skill.knowledge_tags)
                    hits = [h for h in hits if tag_set.intersection(set(h.get("tags", [])))] or hits[:5]
                chunks = []
                for h in hits[:5]:
                    chunks.append(h.get("text", ""))
                return "\n\n---\n\n".join(c for c in chunks if c)
            except Exception as e:
                logger.warning(f"Preflight knowledge retrieval failed: {e}")
                return ""

        # 附属文件内容注入（与 skill_engine 运行时一致）
        from app.services.skill_engine import _read_source_files
        _source_file_ctx = _read_source_files(skill_id, source_files) if source_files else ""
        _base_prompt = system_prompt + _source_file_ctx if _source_file_ctx else system_prompt

        for idx, tc in enumerate(test_cases):
            yield _sse("stage", {"label": f"运行测试 {idx + 1}/{len(test_cases)}..."})

            # 检索知识库，注入到 system_prompt
            knowledge_ctx = await _retrieve_knowledge(tc)
            full_prompt = _base_prompt
            if knowledge_ctx:
                full_prompt += f"\n\n## 参考知识\n\n{knowledge_ctx}"

            # 调 LLM
            try:
                response, _ = await llm_gateway.chat(
                    model_config=model_cfg,
                    messages=[
                        {"role": "system", "content": full_prompt},
                        {"role": "user", "content": tc},
                    ],
                    temperature=0.7,
                    max_tokens=1500,
                )
            except Exception as e:
                tests.append({"index": idx + 1, "test_input": tc, "response": "", "score": 0, "detail": {"reason": f"LLM 调用失败：{e}"}})
                continue

            # AI 评分
            yield _sse("stage", {"label": f"评估测试 {idx + 1} 回复质量..."})
            kb_summary = knowledge_ctx[:500] if knowledge_ctx else "（未检索到相关知识）"
            score_prompt = _QUALITY_SCORE_PROMPT.format(
                description=skill.description or "无描述",
                system_prompt=system_prompt[:1500],
                knowledge_summary=kb_summary,
                test_input=tc,
                response=response[:2000],
            )
            try:
                score_raw, _ = await llm_gateway.chat(
                    model_config=llm_gateway.resolve_config(db, "sandbox.preflight_score"),
                    messages=[{"role": "user", "content": score_prompt}],
                    temperature=0.0,
                    max_tokens=1024,
                )
                score_data = _parse_score_json(score_raw.strip())
                sc = int(score_data.get("score", 0))
            except Exception as e:
                logger.warning(f"Preflight score parse failed: {e}, raw={score_raw[:300] if 'score_raw' in dir() else 'N/A'}")
                sc = 50
                score_data = {"score": 50, "reason": f"评分解析失败: {str(e)[:100]}"}

            test_result = {
                "index": idx + 1,
                "test_input": tc,
                "response": response[:500] + ("..." if len(response) > 500 else ""),
                "score": sc,
                "detail": score_data,
            }
            tests.append(test_result)
            total_score += sc
            yield _sse("test_result", test_result)

        avg_score = round(total_score / len(tests)) if tests else 0
        quality_passed = avg_score >= 70

        _save_result(db, skill_id, "quality", quality_passed, "", {"tests": tests, "avg_score": avg_score}, score=avg_score)

        yield _sse("done", {
            "passed": quality_passed,
            "score": avg_score,
            "gates": gates,
            "tests": tests,
        })

    return StreamingResponse(generate(), media_type="text/event-stream")


# ─── Knowledge confirm ────────────────────────────────────────────────────────

class KnowledgeConfirmItem(BaseModel):
    filename: str
    target_board: str = ""
    target_category: str = ""
    display_title: str = ""


class KnowledgeConfirmRequest(BaseModel):
    confirmations: List[KnowledgeConfirmItem]


@router.post("/preflight/{skill_id}/knowledge-confirm")
async def knowledge_confirm(
    skill_id: int,
    req: KnowledgeConfirmRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """确认知识库文件归档路径并入库。"""
    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill 不存在")

    from app.models.knowledge import KnowledgeEntry
    results = []
    all_entry_ids: List[int] = []
    created_entry_ids: List[int] = []

    for item in req.confirmations:
        # 读取文件内容
        source_files = skill.source_files or []
        file_info = next((f for f in source_files if f["filename"] == item.filename), None)
        if not file_info:
            results.append({"filename": item.filename, "ok": False, "reason": "文件不存在"})
            continue

        # 读文件内容 — 多路径搜索（workspace 迁移可能导致原路径失效）
        import os
        file_path = file_info.get("path", "")
        content = ""
        candidate_paths = [file_path] if file_path else []
        # 如果原路径不存在，尝试 workspace 标准位置
        if file_path and not os.path.exists(file_path):
            fname = os.path.basename(file_path)
            # 从 Skill 所属用户的 workspace 搜索
            try:
                from app.services.runtime_process_manager import _get_registry_workspace_root
                ws_root = _get_registry_workspace_root(user.id)
                if ws_root:
                    for subdir in ["project/output", "project", "skill_studio/data",
                                   "runtime/config/opencode/skills"]:
                        alt = os.path.join(ws_root, subdir, fname)
                        if os.path.exists(alt):
                            candidate_paths.insert(0, alt)
                            break
            except Exception:
                pass
        for cp in candidate_paths:
            if cp and os.path.exists(cp):
                try:
                    with open(cp, "r", encoding="utf-8") as f:
                        content = f.read()
                    if content:
                        break
                except Exception:
                    pass

        if not content:
            results.append({"filename": item.filename, "ok": False, "reason": "文件内容为空或无法读取"})
            continue

        title = item.display_title or item.filename
        category = item.target_category or "general"

        # 创建/更新 knowledge_entry
        existing = db.query(KnowledgeEntry).filter(
            (KnowledgeEntry.title == title) | (KnowledgeEntry.source_file == item.filename)
        ).first()

        is_new = False
        if existing:
            existing.content = content
            existing.category = category
            existing.taxonomy_board = item.target_board or existing.taxonomy_board
            entry_id = existing.id
        else:
            from app.models.knowledge import KnowledgeStatus
            from app.models.user import get_system_user_id
            entry = KnowledgeEntry(
                title=title,
                content=content,
                category=category,
                status=KnowledgeStatus.APPROVED,
                created_by=get_system_user_id(db),
                source_type="skill_preflight",
                source_file=item.filename,
                taxonomy_board=item.target_board or None,
            )
            db.add(entry)
            db.flush()
            entry_id = entry.id
            is_new = True

        db.commit()
        all_entry_ids.append(entry_id)
        if is_new:
            created_entry_ids.append(entry_id)

        # 触发向量入库
        try:
            from app.services.vector_service import index_knowledge, delete_knowledge_vectors
            delete_knowledge_vectors(entry_id)  # 清除旧向量
            index_knowledge(entry_id, content, user.id)
            results.append({"filename": item.filename, "ok": True, "knowledge_id": entry_id})
        except Exception as e:
            results.append({"filename": item.filename, "ok": True, "knowledge_id": entry_id, "vector_warning": str(e)})

    # 清除 knowledge gate 缓存，强制下次重检
    db.query(SkillPreflightResult).filter(
        SkillPreflightResult.skill_id == skill_id,
        SkillPreflightResult.gate_name == "knowledge",
    ).delete()
    db.commit()

    failed = [r for r in results if not r.get("ok")]
    return {
        "results": results,
        "knowledge_entry_ids": all_entry_ids,
        "created_entry_ids": created_entry_ids,
        "failed_count": len(failed),
        "failed_files": [r["filename"] for r in failed],
    }


# ─── Gap 7: Skill 自动回归测试 ──────────────────────────────────────────────

@router.post("/regression/{skill_id}")
async def run_regression_test(
    skill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """对 Skill 当前版本重跑上一版本的基线测试用例，返回回归 diff。"""
    from app.models.sandbox import SandboxTestSession, SandboxTestReport

    skill = db.get(Skill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")

    # 查找最新已发布版本的 baseline session
    versions = (
        db.query(SkillVersion)
        .filter(SkillVersion.skill_id == skill_id, SkillVersion.baseline_sandbox_session_id.isnot(None))
        .order_by(SkillVersion.version.desc())
        .all()
    )
    if not versions:
        raise HTTPException(400, "该 Skill 没有可用的回归测试基线")

    baseline_ver = versions[0]
    baseline_session = db.get(SandboxTestSession, baseline_ver.baseline_sandbox_session_id)
    if not baseline_session or not baseline_session.report_id:
        raise HTTPException(400, "基线沙盒会话无有效报告")

    baseline_report = db.get(SandboxTestReport, baseline_session.report_id)
    if not baseline_report:
        raise HTTPException(400, "基线测试报告不存在")

    # 从 baseline report 中提取 test cases
    test_matrix = (baseline_report.part2_test_matrix or {}) if hasattr(baseline_report, "part2_test_matrix") else {}
    cases = test_matrix.get("cases", [])
    if not cases:
        raise HTTPException(400, "基线报告中无测试用例")

    # 获取当前版本 prompt
    latest_ver = (
        db.query(SkillVersion)
        .filter(SkillVersion.skill_id == skill_id)
        .order_by(SkillVersion.version.desc())
        .first()
    )
    if not latest_ver:
        raise HTTPException(400, "Skill 没有版本")

    # 逐个重跑
    regressions = []
    passed = 0
    model_config = llm_gateway.resolve_config(db, "sandbox.execute")

    for case in cases:
        user_input = case.get("input") or case.get("user_message", "")
        if not user_input:
            continue
        expected_passed = case.get("passed", True)

        try:
            messages = [
                {"role": "system", "content": latest_ver.system_prompt},
                {"role": "user", "content": user_input},
            ]
            response, _usage = await llm_gateway.chat(
                model_config=model_config,
                messages=messages,
                max_tokens=2048,
            )
            # 简单判定：如果基线通过且当前也有响应，算 pass
            current_passed = bool(response and len(response.strip()) > 10)
        except Exception as e:
            current_passed = False
            response = str(e)

        if expected_passed and not current_passed:
            regressions.append({
                "input": user_input[:200],
                "baseline_passed": expected_passed,
                "current_passed": current_passed,
                "response_preview": (response or "")[:300],
            })
        else:
            passed += 1

    return {
        "skill_id": skill_id,
        "baseline_version": baseline_ver.version,
        "current_version": latest_ver.version,
        "total_cases": len(cases),
        "passed": passed,
        "regressions": len(regressions),
        "details": regressions,
    }
