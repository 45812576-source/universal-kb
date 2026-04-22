"""Studio Tool Runtime — 后端 tool capability 网关。

Phase B9: 把 card contract 的 allowed_tools 变成真实后端 capability。
模型只输出 tool intent，后端检查 contract/card/权限/冲突/human confirmation。
每次 tool call 写 HarnessStep。

工具分类:
- read: 只读查询（studio_chat.ask_one_question, skill_file.open）
- stage: 暂存修改（skill_draft.stage_edit, skill_file.stage_edit, studio_artifact.save/update）
- execute: 执行操作（sandbox.run, sandbox.targeted_rerun）
- publish: 发布级操作（staged_edit.adopt, staged_edit.reject）

M5 边界: 不静默降级，tool 不在白名单 → 返回 error_patch。
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.services import studio_card_contract_service

logger = logging.getLogger(__name__)


class ToolCategory(str, Enum):
    READ = "read"
    STAGE = "stage"
    EXECUTE = "execute"
    PUBLISH = "publish"


class ToolCheckResult(str, Enum):
    ALLOWED = "allowed"
    DENIED_NOT_IN_CONTRACT = "denied_not_in_contract"
    DENIED_NO_CARD = "denied_no_card"
    DENIED_CARD_NOT_ACTIVE = "denied_card_not_active"
    DENIED_FORBIDDEN = "denied_forbidden"
    NEEDS_CONFIRMATION = "needs_confirmation"


@dataclass
class ToolIntent:
    """模型输出的 tool intent — 后端统一验证。"""
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    card_id: str | None = None
    contract_id: str | None = None
    run_id: str | None = None
    step_seq: int = 0


@dataclass
class ToolCheckResponse:
    """验证结果。"""
    result: ToolCheckResult
    tool_name: str
    category: ToolCategory | None = None
    reason: str = ""
    message: str = ""
    confirmation_prompt: str | None = None


@dataclass
class ToolExecutionRecord:
    """工具执行记录 — 对应 HarnessStep。"""
    step_id: str = field(default_factory=lambda: f"step_{uuid.uuid4().hex[:10]}")
    run_id: str = ""
    tool_name: str = ""
    category: str = ""
    step_seq: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    input_summary: str = ""
    output_summary: str = ""
    status: str = "running"  # running | completed | failed | denied
    error: str | None = None


# ── Tool Registry ────────────────────────────────────────────────────────────

# 工具名 → 分类映射
TOOL_CATEGORIES: dict[str, ToolCategory] = {
    # read 类
    "studio_chat.ask_one_question": ToolCategory.READ,
    "skill_file.open": ToolCategory.READ,

    # stage 类
    "skill_draft.stage_edit": ToolCategory.STAGE,
    "skill_draft.generate": ToolCategory.STAGE,
    "skill_file.stage_edit": ToolCategory.STAGE,
    "studio_artifact.save": ToolCategory.STAGE,
    "studio_artifact.update": ToolCategory.STAGE,

    # execute 类
    "sandbox.run": ToolCategory.EXECUTE,
    "sandbox.targeted_rerun": ToolCategory.EXECUTE,
    "skill_governance.open_panel": ToolCategory.EXECUTE,

    # publish 类
    "staged_edit.adopt": ToolCategory.PUBLISH,
    "staged_edit.reject": ToolCategory.PUBLISH,
}

# 需要 human confirmation 的工具
TOOLS_REQUIRING_CONFIRMATION: frozenset[str] = frozenset({
    "staged_edit.adopt",
    "sandbox.run",
})


def get_tool_category(tool_name: str) -> ToolCategory | None:
    """获取工具分类。"""
    return TOOL_CATEGORIES.get(tool_name)


def is_tool_registered(tool_name: str) -> bool:
    """工具是否在已注册列表中。"""
    return tool_name in TOOL_CATEGORIES


# ── Validation ───────────────────────────────────────────────────────────────

def check_tool_intent(
    intent: ToolIntent,
    *,
    active_card: dict[str, Any] | None = None,
    skip_confirmation: bool = False,
) -> ToolCheckResponse:
    """验证 tool intent 是否被允许。

    检查链:
    1. 工具是否已注册
    2. card 是否 active
    3. contract 是否允许此工具
    4. 是否在 forbidden_actions 中
    5. 是否需要 human confirmation
    """
    tool_name = intent.tool_name
    category = get_tool_category(tool_name)

    # 1. 工具未注册
    if not category:
        return ToolCheckResponse(
            result=ToolCheckResult.DENIED_NOT_IN_CONTRACT,
            tool_name=tool_name,
            reason="unregistered_tool",
            message=f"工具「{tool_name}」未注册，无法执行",
        )

    # 2. 无 active card
    if not active_card:
        return ToolCheckResponse(
            result=ToolCheckResult.DENIED_NO_CARD,
            tool_name=tool_name,
            category=category,
            reason="no_active_card",
            message="当前没有 active card，无法执行工具",
        )

    # 3. card 不是 active 状态
    card_status = active_card.get("status", "")
    if card_status not in ("active", "reviewing", "drafting", "diff_ready"):
        return ToolCheckResponse(
            result=ToolCheckResult.DENIED_CARD_NOT_ACTIVE,
            tool_name=tool_name,
            category=category,
            reason="card_not_active",
            message=f"卡片状态为「{card_status}」，需要 active 才能执行工具",
        )

    # 4. contract 白名单检查
    contract_id = intent.contract_id or active_card.get("contract_id")
    if contract_id:
        if not studio_card_contract_service.is_tool_allowed(contract_id, tool_name):
            contract = studio_card_contract_service.get_contract(contract_id)
            allowed = contract.allowed_tools if contract else []
            return ToolCheckResponse(
                result=ToolCheckResult.DENIED_NOT_IN_CONTRACT,
                tool_name=tool_name,
                category=category,
                reason="not_in_allowed_tools",
                message=f"工具「{tool_name}」不在 contract「{contract_id}」的白名单中，允许: {', '.join(allowed)}",
            )

        # 5. forbidden_actions 检查
        contract = studio_card_contract_service.get_contract(contract_id)
        if contract and contract.forbidden_actions:
            for forbidden in contract.forbidden_actions:
                if tool_name in forbidden.lower():
                    return ToolCheckResponse(
                        result=ToolCheckResult.DENIED_FORBIDDEN,
                        tool_name=tool_name,
                        category=category,
                        reason="forbidden_action",
                        message=f"contract 禁止此操作: {forbidden}",
                    )

    # 6. human confirmation 检查
    if not skip_confirmation and tool_name in TOOLS_REQUIRING_CONFIRMATION:
        return ToolCheckResponse(
            result=ToolCheckResult.NEEDS_CONFIRMATION,
            tool_name=tool_name,
            category=category,
            reason="needs_human_confirmation",
            message=f"工具「{tool_name}」需要用户确认后才能执行",
            confirmation_prompt=_build_confirmation_prompt(tool_name, intent),
        )

    return ToolCheckResponse(
        result=ToolCheckResult.ALLOWED,
        tool_name=tool_name,
        category=category,
    )


def _build_confirmation_prompt(tool_name: str, intent: ToolIntent) -> str:
    """构建确认提示文案。"""
    prompts = {
        "staged_edit.adopt": "即将采纳修改并写入文件，确认继续？",
        "sandbox.run": "即将启动 Sandbox 运行测试，确认继续？",
    }
    return prompts.get(tool_name, f"即将执行「{tool_name}」，确认继续？")


# ── Execution Recording ──────────────────────────────────────────────────────

def create_execution_record(
    intent: ToolIntent,
    *,
    check_response: ToolCheckResponse,
) -> ToolExecutionRecord:
    """创建工具执行记录（对应 HarnessStep）。"""
    record = ToolExecutionRecord(
        run_id=intent.run_id or "",
        tool_name=intent.tool_name,
        category=check_response.category.value if check_response.category else "",
        step_seq=intent.step_seq,
        input_summary=_summarize_input(intent),
    )

    if check_response.result != ToolCheckResult.ALLOWED:
        record.status = "denied"
        record.error = check_response.message
        record.finished_at = time.time()

    return record


def complete_execution_record(
    record: ToolExecutionRecord,
    *,
    output_summary: str = "",
    error: str | None = None,
) -> ToolExecutionRecord:
    """完成执行记录。"""
    record.finished_at = time.time()
    record.output_summary = output_summary
    if error:
        record.status = "failed"
        record.error = error
    else:
        record.status = "completed"
    return record


def _summarize_input(intent: ToolIntent) -> str:
    """生成输入摘要。"""
    args_str = ", ".join(f"{k}={v!r}" for k, v in list(intent.arguments.items())[:3])
    if len(intent.arguments) > 3:
        args_str += f", ... (+{len(intent.arguments) - 3})"
    return f"{intent.tool_name}({args_str})"


# ── Patch Builders ───────────────────────────────────────────────────────────

def build_tool_error_patch(
    *,
    run_id: str,
    run_version: int,
    patch_seq: int,
    check_response: ToolCheckResponse,
    card_id: str | None = None,
) -> dict[str, Any]:
    """M5: 工具被拒时生成 tool_error_patch — 不静默降级。

    注意: 返回 patch_type="tool_error_patch"（对应 PATCH_TYPE_BY_EVENT["tool_error"]），
    而非通用 error_patch，前端需要区分工具拒绝和系统错误。
    """
    from app.services.studio_patch_bus import build_patch_envelope
    payload = {
        "error_type": check_response.reason,
        "message": check_response.message,
        "tool_name": check_response.tool_name,
        "card_id": card_id,
        "category": check_response.category.value if check_response.category else None,
        "retryable": False,
    }
    return build_patch_envelope(
        run_id=run_id,
        run_version=run_version,
        patch_seq=patch_seq,
        patch_type="tool_error_patch",
        target=card_id or "",
        payload=payload,
    )


def build_tool_confirmation_patch(
    *,
    run_id: str,
    run_version: int,
    patch_seq: int,
    check_response: ToolCheckResponse,
    card_id: str | None = None,
) -> dict[str, Any]:
    """需要用户确认时的 patch。"""
    from app.services.studio_patch_bus import build_patch_envelope
    return build_patch_envelope(
        run_id=run_id,
        run_version=run_version,
        patch_seq=patch_seq,
        patch_type="tool_confirmation_patch",
        target=card_id or "",
        payload={
            "tool_name": check_response.tool_name,
            "category": check_response.category.value if check_response.category else None,
            "confirmation_prompt": check_response.confirmation_prompt,
            "reason": check_response.reason,
        },
    )


def execution_record_to_harness_step(record: ToolExecutionRecord) -> dict[str, Any]:
    """将 ToolExecutionRecord 转为 HarnessStep 兼容 dict。"""
    return {
        "step_id": record.step_id,
        "run_id": record.run_id,
        "step_type": "tool_call",
        "seq": record.step_seq,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "input_summary": record.input_summary,
        "output_summary": record.output_summary,
        "metadata": {
            "tool_name": record.tool_name,
            "category": record.category,
            "status": record.status,
        },
        "error": record.error,
    }
