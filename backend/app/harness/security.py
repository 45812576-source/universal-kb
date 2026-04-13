"""SecurityPipeline — 统一安全管线，在工具/LLM 调用前执行 Guard 链。

Guard 链按顺序执行，任一 Guard 返回 deny 即终止。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.harness.contracts import SecurityDecisionStatus

logger = logging.getLogger(__name__)


@dataclass
class SecurityContext:
    """安全检查的上下文信息。"""
    db: Session
    user_id: int | None = None
    workspace_id: int | None = None
    skill_id: int | None = None
    tool_name: str | None = None
    tool_args: dict = field(default_factory=dict)
    model_config: dict = field(default_factory=dict)
    # 额外上下文
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SecurityDecision:
    """Guard 链的判定结果。"""
    status: SecurityDecisionStatus
    reason: str = ""
    guard_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def allow(guard_name: str = "") -> SecurityDecision:
        return SecurityDecision(status=SecurityDecisionStatus.ALLOW, guard_name=guard_name)

    @staticmethod
    def deny(reason: str, guard_name: str = "") -> SecurityDecision:
        return SecurityDecision(status=SecurityDecisionStatus.DENY, reason=reason, guard_name=guard_name)

    @staticmethod
    def needs_approval(reason: str, guard_name: str = "") -> SecurityDecision:
        return SecurityDecision(status=SecurityDecisionStatus.NEEDS_APPROVAL, reason=reason, guard_name=guard_name)


class Guard(ABC):
    """Guard 基类。"""
    name: str = "guard"

    @abstractmethod
    async def check(self, context: SecurityContext) -> SecurityDecision:
        ...


class AuthGuard(Guard):
    """用户身份认证检查。"""
    name = "auth"

    async def check(self, context: SecurityContext) -> SecurityDecision:
        if context.user_id is None:
            return SecurityDecision.deny("未认证用户", self.name)
        return SecurityDecision.allow(self.name)


class ModelGrantGuard(Guard):
    """模型授权检查 — 复用现有 _check_model_grant 逻辑。"""
    name = "model_grant"

    async def check(self, context: SecurityContext) -> SecurityDecision:
        if not context.model_config:
            return SecurityDecision.allow(self.name)
        try:
            from app.models.opencode import UserModelGrant
            model_id = context.model_config.get("model_id", "")
            any_grant = (
                context.db.query(UserModelGrant)
                .filter(
                    (UserModelGrant.model_key == model_id) |
                    UserModelGrant.model_key.like(f"%/{model_id}")
                )
                .first()
            )
            if any_grant is None:
                return SecurityDecision.allow(self.name)  # 不是受限模型
            if context.user_id is None:
                return SecurityDecision.deny(f"模型 {model_id} 需要授权才能使用", self.name)
            user_grant = (
                context.db.query(UserModelGrant)
                .filter(
                    UserModelGrant.user_id == context.user_id,
                    (UserModelGrant.model_key == model_id) |
                    UserModelGrant.model_key.like(f"%/{model_id}")
                )
                .first()
            )
            if user_grant is None:
                return SecurityDecision.deny(f"您没有使用模型 {model_id} 的权限", self.name)
            return SecurityDecision.allow(self.name)
        except Exception as e:
            logger.warning(f"ModelGrantGuard check failed: {e}")
            return SecurityDecision.allow(self.name)  # fail-open


class ScopeGuard(Guard):
    """Workspace 边界检查 — 工具/Skill 是否在当前 workspace 授权范围内。"""
    name = "scope"

    async def check(self, context: SecurityContext) -> SecurityDecision:
        if not context.skill_id or not context.user_id:
            return SecurityDecision.allow(self.name)
        try:
            from app.services.permission_engine import permission_engine
            from app.models.user import User
            caller = context.db.get(User, context.user_id)
            if caller and not permission_engine.check_skill_callable(caller, context.skill_id, context.db):
                return SecurityDecision.deny(
                    f"Skill {context.skill_id} 不在当前用户的可调用范围内", self.name
                )
        except Exception as e:
            logger.warning(f"ScopeGuard check failed: {e}")
        return SecurityDecision.allow(self.name)


class ToolPermissionGuard(Guard):
    """工具级权限检查 — 基于 SkillPolicy 的工具白名单。"""
    name = "tool_permission"

    async def check(self, context: SecurityContext) -> SecurityDecision:
        if not context.tool_name:
            return SecurityDecision.allow(self.name)
        # 如果有 workspace，检查工具是否在 workspace 的工具列表中
        if context.workspace_id:
            try:
                from app.models.workspace import Workspace, WorkspaceTool
                ws = context.db.get(Workspace, context.workspace_id)
                if ws and ws.workspace_tools:
                    from app.models.tool import ToolRegistry
                    ws_tool_names = set()
                    for wt in ws.workspace_tools:
                        tool = context.db.get(ToolRegistry, wt.tool_id)
                        if tool:
                            ws_tool_names.add(tool.name)
                    if ws_tool_names and context.tool_name not in ws_tool_names:
                        return SecurityDecision.deny(
                            f"工具 {context.tool_name} 不在工作台授权范围内", self.name
                        )
            except Exception as e:
                logger.warning(f"ToolPermissionGuard workspace check failed: {e}")
        return SecurityDecision.allow(self.name)


class ApprovalGuard(Guard):
    """审批检查 — 需人工审批时返回 needs_approval。

    当前为占位实现，后续可对接审批流。
    """
    name = "approval"

    async def check(self, context: SecurityContext) -> SecurityDecision:
        # TODO: 检查是否有需要审批的操作（如高危工具、敏感数据变更）
        return SecurityDecision.allow(self.name)


class OutputFilter:
    """输出侧过滤 — 复用现有 permission_engine.apply_output_masks。"""

    def apply(
        self,
        db: Session,
        user_id: int | None,
        skill_id: int | None,
        structured_output: dict | None,
    ) -> dict | None:
        """对结构化输出应用字段级脱敏。"""
        if not structured_output or not skill_id or not user_id:
            return structured_output
        try:
            from app.services.permission_engine import permission_engine
            from app.models.permission import SkillPolicy
            from app.models.user import User
            caller = db.get(User, user_id)
            if not caller:
                return structured_output

            policy = db.query(SkillPolicy).filter(SkillPolicy.skill_id == skill_id).first()
            if not policy or not policy.default_data_scope:
                return structured_output

            for domain_key, domain_conf in (policy.default_data_scope or {}).items():
                if isinstance(domain_conf, dict) and domain_conf.get("data_domain_id"):
                    structured_output = permission_engine.apply_output_masks(
                        user=caller,
                        data=structured_output,
                        data_domain_id=domain_conf["data_domain_id"],
                        db=db,
                    )
            return structured_output
        except Exception as e:
            logger.warning(f"OutputFilter apply failed: {e}")
            return structured_output


class SecurityPipeline:
    """统一安全管线，在工具/LLM 调用前执行 Guard 链。"""

    def __init__(self, guards: list[Guard] | None = None):
        self.guards = guards or [
            AuthGuard(),
            ModelGrantGuard(),
            ScopeGuard(),
            ToolPermissionGuard(),
            ApprovalGuard(),
        ]
        self.output_filter = OutputFilter()

    async def check(self, context: SecurityContext) -> SecurityDecision:
        """按顺序执行 Guard 链，返回第一个非 allow 的结果。"""
        for guard in self.guards:
            try:
                decision = await guard.check(context)
                if decision.status != SecurityDecisionStatus.ALLOW:
                    logger.info(
                        f"SecurityPipeline: guard={guard.name} status={decision.status.value} "
                        f"reason={decision.reason}"
                    )
                    return decision
            except Exception as e:
                logger.warning(f"SecurityPipeline: guard={guard.name} error={e}, skipping")
                continue
        return SecurityDecision.allow("pipeline")

    async def check_tool_call(
        self,
        db: Session,
        user_id: int | None,
        tool_name: str,
        tool_args: dict | None = None,
        workspace_id: int | None = None,
        skill_id: int | None = None,
    ) -> SecurityDecision:
        """检查单次工具调用是否被允许。"""
        context = SecurityContext(
            db=db,
            user_id=user_id,
            workspace_id=workspace_id,
            skill_id=skill_id,
            tool_name=tool_name,
            tool_args=tool_args or {},
        )
        return await self.check(context)

    def filter_output(
        self, db: Session, user_id: int | None, skill_id: int | None,
        structured_output: dict | None,
    ) -> dict | None:
        """对结构化输出应用输出侧过滤。"""
        return self.output_filter.apply(db, user_id, skill_id, structured_output)


# 全局实例
security_pipeline = SecurityPipeline()
