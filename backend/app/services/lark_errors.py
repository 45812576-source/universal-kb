"""Structured errors for Feishu/Lark knowledge import."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LarkImportError(Exception):
    """Base error that can be serialized to API clients."""

    code: str
    message: str
    status_code: int = 400
    action_hint: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        super().__init__(self.message)

    def to_detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": {
                **self.details,
                "action_hint": self.action_hint,
            },
        }


class LarkAppNotConfiguredError(LarkImportError):
    def __init__(self, message: str = "飞书应用未配置，请联系管理员"):
        super().__init__(
            code="LARK_APP_NOT_CONFIGURED",
            message=message,
            status_code=501,
            action_hint="请管理员在后端配置 LARK_APP_ID / LARK_APP_SECRET，并确认飞书开放平台应用已启用。",
            details={"auth_mode": "app_only"},
        )


class LarkAppDocumentPermissionError(LarkImportError):
    def __init__(self, message: str = "Le Desk 飞书应用没有该文档的读取权限"):
        super().__init__(
            code="LARK_APP_NO_DOCUMENT_ACCESS",
            message=message,
            status_code=403,
            action_hint=(
                "请在飞书文档、多维表或知识库空间中，将 Le Desk 飞书应用添加为可访问应用；"
                "如果是知识库空间，请在空间设置中添加应用为成员。"
            ),
            details={"auth_mode": "app_only"},
        )


class LarkAppScopeMissingError(LarkImportError):
    def __init__(self, message: str = "Le Desk 飞书应用缺少必要的开放平台权限"):
        super().__init__(
            code="LARK_APP_SCOPE_MISSING",
            message=message,
            status_code=403,
            action_hint="请管理员在飞书开放平台为 Le Desk 应用补充文档、云空间、知识库或多维表读取权限并发布生效。",
            details={"auth_mode": "app_only"},
        )


class LarkUnsupportedLinkError(LarkImportError):
    def __init__(self, message: str = "飞书链接类型暂不支持"):
        super().__init__(
            code="LARK_LINK_UNSUPPORTED",
            message=message,
            status_code=400,
            action_hint="请粘贴具体飞书文档、知识库节点、表格、多维表或云空间文件链接，不要粘贴文件夹链接。",
            details={"auth_mode": "app_only"},
        )


class LarkApiCallError(LarkImportError):
    def __init__(self, message: str = "飞书 API 调用失败"):
        super().__init__(
            code="LARK_API_ERROR",
            message=message,
            status_code=502,
            action_hint="请稍后重试；如果持续失败，请联系管理员检查飞书应用配置、权限范围和接口日志。",
            details={"auth_mode": "app_only"},
        )


def normalize_lark_import_error(error: Exception) -> LarkImportError:
    """Convert legacy exceptions into structured import errors."""
    if isinstance(error, LarkImportError):
        return error

    from app.services.lark_client import LarkAuthError, LarkConfigError, LarkPermissionError

    message = str(error)
    if isinstance(error, ValueError):
        return LarkUnsupportedLinkError(message)
    if isinstance(error, LarkConfigError):
        return LarkAppNotConfiguredError(f"飞书应用未配置: {message}")
    if isinstance(error, LarkAuthError):
        return LarkApiCallError(f"飞书认证失败: {message}")
    if isinstance(error, (LarkPermissionError, PermissionError)):
        if "bitable:app:readonly" in message or "开通" in message:
            return LarkAppScopeMissingError(message)
        return LarkAppDocumentPermissionError(message)
    if isinstance(error, RuntimeError):
        return LarkApiCallError(f"飞书 API 调用失败: {message}")
    return LarkApiCallError(message or "飞书导入失败")
