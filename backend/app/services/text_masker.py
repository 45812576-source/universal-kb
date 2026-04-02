"""文本脱敏引擎 — 基于 DATA_TYPE_REGISTRY 对非结构化文本执行精准脱敏。

供 Skill 知识注入和索引预脱敏使用：
  - mask_text(): 按脱敏级别 + 数据类型对原文做文本替换
  - _apply_text_mask(): 单条匹配的文本替换逻辑
"""
from __future__ import annotations

import re
import logging
from typing import Any

from app.data.sensitivity_rules import DATA_TYPE_REGISTRY, detect_data_types

logger = logging.getLogger(__name__)

_LEVEL_ORDER = {"D0": 0, "D1": 1, "D2": 2, "D3": 3, "D4": 4}

# 每种 mask_action 在文本场景下的替换逻辑
_ABSTRACT_LABELS: dict[str, str] = {
    "person_name": "某某",
    "company_name": "某公司",
    "customer_name": "某客户",
    "customer_list": "若干客户",
    "lead_name": "某线索",
    "address": "某地",
    "strategic_info": "[战略信息]",
    "pricing_policy": "[定价策略]",
    "negotiation_record": "[谈判记录]",
    "internal_conclusion": "[内部结论]",
    "media_plan_detail": "[媒介计划]",
}


def mask_text(
    text: str,
    level: str = "D1",
    data_type_hits: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """对非结构化文本执行基于 DATA_TYPE_REGISTRY 的精准脱敏。

    Args:
        text: 原文
        level: 脱敏级别 D0~D4
        data_type_hits: 可选的预识别数据类型命中列表（来自文档理解）

    Returns:
        (脱敏后文本, 替换记录 [{type, original, masked, position}])

    级别策略：
        D0: 不脱敏，原样返回
        D1: 只脱敏 default_desensitization_level >= D2 的类型
        D2: 脱敏 default_desensitization_level >= D1 的类型
        D3: 脱敏所有非 keep 的类型
        D4: 全部脱敏
    """
    if not text or level == "D0":
        return text, []

    cur_level = _LEVEL_ORDER.get(level, 1)

    # 确定需要脱敏的数据类型
    types_to_mask = _get_types_to_mask(cur_level)
    if not types_to_mask:
        return text, []

    # 如果没有预识别命中，先做检测
    if data_type_hits is None:
        data_type_hits = detect_data_types(text)

    if not data_type_hits:
        return text, []

    # 只处理命中的且在待脱敏列表中的类型
    hit_types = {h["type"] for h in data_type_hits}
    active_types = types_to_mask & hit_types

    if not active_types:
        return text, []

    # 收集所有需要替换的 (start, end, masked_text, dtype) 并按位置倒序替换
    replacements: list[dict] = []
    masked_text = text

    # 按优先级排序：先处理有 pattern 的（精确匹配），再处理 keyword 的
    for dtype in active_types:
        spec = DATA_TYPE_REGISTRY.get(dtype)
        if not spec:
            continue

        action = spec.get("default_mask_action", "full_mask")
        pattern = spec.get("pattern")

        if pattern:
            for m in pattern.finditer(masked_text):
                original = m.group(0)
                masked_val = _apply_text_mask(original, action, dtype)
                if masked_val != original:
                    replacements.append({
                        "type": dtype,
                        "original": original,
                        "masked": masked_val,
                        "position": m.start(),
                    })

    # 按位置倒序替换（避免偏移）
    replacements.sort(key=lambda r: r["position"], reverse=True)
    for r in replacements:
        # 在当前 masked_text 中找到并替换第一个匹配
        masked_text = masked_text.replace(r["original"], r["masked"], 1)

    # 对只有 keywords 没有 pattern 的类型，不做文本替换（无法精准定位）
    # 但记录到替换记录中供审计
    return masked_text, replacements


def _get_types_to_mask(cur_level: int) -> set[str]:
    """根据当前脱敏级别，决定哪些数据类型需要被脱敏。

    策略：
    - D1 (cur_level=1): 脱敏 type_level >= D2 的（高敏类型）
    - D2 (cur_level=2): 脱敏 type_level >= D1 的
    - D3 (cur_level=3): 脱敏所有非 keep 的
    - D4 (cur_level=4): 全部脱敏
    """
    types: set[str] = set()
    for dtype, spec in DATA_TYPE_REGISTRY.items():
        action = spec.get("default_mask_action", "full_mask")
        if action == "keep":
            if cur_level < 4:  # D4 连 keep 也脱
                continue

        type_level = _LEVEL_ORDER.get(spec.get("default_desensitization_level", "D0"), 0)

        if cur_level >= 4:
            # D4: 全部脱敏
            types.add(dtype)
        elif cur_level >= 3:
            # D3: 脱敏所有非 keep
            types.add(dtype)
        elif cur_level >= 2:
            # D2: 脱敏 type_level >= D1
            if type_level >= 1:
                types.add(dtype)
        elif cur_level >= 1:
            # D1: 只脱敏 type_level >= D2（高敏类型）
            if type_level >= 2:
                types.add(dtype)

    return types


def _apply_text_mask(value: str, action: str, dtype: str) -> str:
    """对单个匹配值执行文本脱敏。

    | action       | 文本效果                |
    |-------------|------------------------|
    | partial_mask | 138****5678            |
    | full_mask    | [已脱敏]                |
    | range_mask   | 约XX万元               |
    | abstract     | 某客户/某公司           |
    | keep         | 原样                   |
    """
    if action == "keep":
        return value

    if action == "partial_mask":
        return _partial_mask(value, dtype)

    if action == "full_mask":
        return "[已脱敏]"

    if action == "range_mask":
        return _range_mask(value, dtype)

    if action == "abstract":
        return _ABSTRACT_LABELS.get(dtype, "[已脱敏]")

    return "[已脱敏]"


def _partial_mask(value: str, dtype: str) -> str:
    """部分掩码：保留首尾，中间用 * 替代。"""
    if dtype == "phone_number" and len(value) == 11:
        return value[:3] + "****" + value[7:]

    if dtype == "email":
        at_pos = value.find("@")
        if at_pos > 0:
            user_part = value[:at_pos]
            domain = value[at_pos:]
            if len(user_part) <= 2:
                return "*" * len(user_part) + domain
            return user_part[0] + "***" + domain
        return "[已脱敏]"

    # 通用 partial_mask
    if len(value) <= 4:
        return "****"
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def _range_mask(value: str, dtype: str) -> str:
    """数值范围化：提取数值后返回量级区间。"""
    # 提取数字
    nums = re.findall(r"[\d,]+\.?\d*", value)
    if not nums:
        return "约XX万元"

    try:
        num_str = nums[0].replace(",", "")
        num = float(num_str)
    except (ValueError, IndexError):
        return "约XX万元"

    # 识别单位
    unit = ""
    if "亿" in value:
        unit = "亿元"
    elif "万" in value:
        unit = "万元"
    elif "元" in value or "¥" in value or "$" in value:
        unit = "元"
    elif "%" in value:
        return "一定比例"

    # 按量级范围化
    if unit == "亿元":
        return f"约{int(num)}亿元"
    elif unit == "万元":
        step = max(int(num // 10) * 10, 10)
        low = int(num // step) * step
        return f"约{low}-{low + step}万元"
    elif unit == "元":
        if num >= 100_000_000:
            return f"约{num / 100_000_000:.0f}亿元"
        elif num >= 10_000:
            approx = int(num // 10000)
            return f"约{approx}万元"
        else:
            return "若干金额"
    else:
        return "若干金额"
