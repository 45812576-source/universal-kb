"""SQL 标识符安全校验，防止 SQL 注入。

所有动态拼入 SQL 的表名、列名必须经过此模块校验。
"""
import re

# 合法 SQL 标识符：字母/下划线开头，后跟字母/数字/下划线/中文
_IDENT_RE = re.compile(r'^[a-zA-Z_\u4e00-\u9fff][a-zA-Z0-9_\u4e00-\u9fff]*$')


def check_identifier(name: str, label: str = "标识符") -> str:
    """校验 SQL 标识符合法性，合法则原样返回，否则抛 ValueError。"""
    if not name or not _IDENT_RE.match(name):
        raise ValueError(f"非法{label}: {name!r}")
    return name


def qi(name: str, label: str = "标识符") -> str:
    """Quote Identifier — 校验后返回反引号包裹的标识符，可直接拼入 SQL。

    用法: f"SELECT {qi(col, '列名')} FROM {qi(table, '表名')}"
    """
    check_identifier(name, label)
    return f"`{name}`"
