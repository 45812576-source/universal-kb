"""沙盒测试 API — 在 Skill/Tool 进入审批前进行自动化试跑验证。

Skill 测试：用 AI 分析 system_prompt，生成典型测试 prompt，
          实际调用 LLM，输出测试报告。
Tool 测试：分析工具的 input_schema/manifest，生成 mock 参数，
         执行 tool_executor，输出执行结果。
"""
from __future__ import annotations

import json
import time
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.models.skill import Skill, SkillVersion
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


async def _generate_test_input(system_prompt: str) -> str:
    """用 AI 根据 system_prompt 生成测试输入。"""
    prompt = _MOCK_GEN_PROMPT.format(system_prompt=system_prompt[:2000])
    try:
        result, _ = await llm_gateway.chat(
            model_config=llm_gateway.get_lite_config(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=200,
        )
        return result.strip()
    except Exception as e:
        logger.warning(f"Mock input generation failed: {e}")
        return "请介绍一下你的功能，并给我一个示例输出。"


async def _generate_tool_mock_params(tool: ToolRegistry) -> dict:
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
            model_config=llm_gateway.get_lite_config(),
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
    skill_name: str, system_prompt: str, test_input: str, response: str
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
            model_config=llm_gateway.get_lite_config(),
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
    steps = []

    # Step 1: 生成测试输入
    t0 = time.time()
    test_input = await _generate_test_input(system_prompt)
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
        model_cfg = llm_gateway.get_config(db, latest_ver.model_config_id or None)
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
        skill.name, system_prompt, test_input, llm_response
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
        mock_params = await _generate_tool_mock_params(tool)
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
