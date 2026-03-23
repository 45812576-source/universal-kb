#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
商家入驻资质智能判断系统 - 工具函数格式
根据营业执照经营范围和产品类型自动匹配所需资质
"""

from typing import Dict, List, Optional


def judge_qualification(
    business_scope: str,
    registered_address: str = "",
    enterprise_type: str = "",
    visual_features: List[str] = None,
    text_features: List[str] = None,
    approval_numbers: List[str] = None
) -> Dict:
    """
    判断商家入驻所需资质（核心工具函数）

    Args:
        business_scope: 营业执照经营范围（必填）
        registered_address: 注册地址（选填）
        enterprise_type: 企业类型（选填）
        visual_features: 产品视觉特征列表，如["蓝帽子标志", "械字号标识"]
        text_features: 产品文字特征列表（OCR识别结果）
        approval_numbers: 产品批准文号列表，如["国食健字G20231234"]

    Returns:
        Dict: 判断结果，包含以下字段：
            - result: 需要的资质类型
            - confidence: 置信度（高/中/低）
            - reason: 判断依据说明
            - suggested_next_step: 建议下一步操作
    """
    # 默认值处理
    if visual_features is None:
        visual_features = []
    if text_features is None:
        text_features = []
    if approval_numbers is None:
        approval_numbers = []

    # 合并营业执照文本用于匹配
    combined_license_text = f"{business_scope} {registered_address} {enterprise_type}".lower()

    # 资质判断规则（按优先级从高到低）

    # 1. 医疗器械资质（优先级最高）
    if _contains_keywords(combined_license_text, ["医疗器械经营", "医疗器械销售"]):
        return {
            "result": "医疗器械资质",
            "confidence": "高",
            "reason": "营业执照经营范围包含'医疗器械经营'相关关键词",
            "suggested_next_step": "请准备医疗器械经营许可证进行入驻"
        }

    if "械字号标识" in visual_features:
        return {
            "result": "医疗器械资质",
            "confidence": "高",
            "reason": "检测到产品包装含械字号标识",
            "suggested_next_step": "请准备医疗器械经营许可证进行入驻"
        }

    # 2. 保健食品资质
    if "蓝帽子标志" in visual_features:
        if _contains_keywords(combined_license_text, ["保健食品销售"]):
            # 判断是否进口
            is_import = ("进口商品中文标签" in visual_features or
                        any("国食健注J" in num for num in approval_numbers))

            if is_import:
                return {
                    "result": "进口保健食品资质",
                    "confidence": "高",
                    "reason": "检测到产品包装含蓝帽子标志且为进口产品（含中文标签或批准文号'国食健注J'），营业执照经营范围包含'保健食品销售'",
                    "suggested_next_step": "请准备进口保健食品相关资质及海外开户准备材料"
                }
            else:
                return {
                    "result": "保健食品资质",
                    "confidence": "高",
                    "reason": "检测到产品包装含蓝帽子标志（国食健字），营业执照经营范围包含'保健食品销售'",
                    "suggested_next_step": "请准备保健食品经营许可证进行入驻"
                }
        else:
            return {
                "result": "食品类产品(含保健食品)",
                "confidence": "中",
                "reason": "检测到产品包装含蓝帽子标志，但营业执照经营范围未包含'保健食品销售'",
                "suggested_next_step": "建议咨询客服确认具体资质要求"
            }

    # 3. 药品资质
    if _contains_keywords(combined_license_text, ["药品经营"]):
        return {
            "result": "药品企业资质",
            "confidence": "高",
            "reason": "营业执照经营范围包含'药品经营'相关关键词",
            "suggested_next_step": "请准备药品经营许可证进行入驻"
        }

    for approval_num in approval_numbers:
        if "药准字" in approval_num:
            return {
                "result": "药品企业资质",
                "confidence": "高",
                "reason": f"检测到产品含药准字号批准文号：{approval_num}",
                "suggested_next_step": "请准备药品经营许可证进行入驻"
            }

    for text in text_features:
        if _contains_keywords(text, ["药", "治疗"]):
            return {
                "result": "药品企业资质",
                "confidence": "中",
                "reason": "产品名称或描述含'药''治疗'等敏感词",
                "suggested_next_step": "请准备药品经营许可证，如非药品请提供相关证明"
            }

    # 4. 消字号资质
    if "消字号标识" in visual_features:
        is_import = ("进口商品中文标签" in visual_features or
                    any("国食健注J" in num for num in approval_numbers))

        if is_import:
            return {
                "result": "进口消字号产品企业资质",
                "confidence": "高",
                "reason": "检测到产品包装含消字号标识（卫消证字）且为进口产品",
                "suggested_next_step": "请准备进口消字号产品企业资质及相关证明"
            }
        else:
            return {
                "result": "消字号产品企业资质",
                "confidence": "高",
                "reason": "检测到产品包装含消字号标识（卫消证字）",
                "suggested_next_step": "请准备消字号产品企业资质进行入驻"
            }

    # 5. 化妆品资质
    if "妆字号标识" in visual_features:
        return {
            "result": "化妆品资质",
            "confidence": "高",
            "reason": "检测到产品包装含妆字号标识（化妆品生产许可证编号）",
            "suggested_next_step": "请准备化妆品资质进行入驻"
        }

    for text in text_features:
        if _contains_keywords(text, ["化妆品", "护肤", "彩妆"]):
            return {
                "result": "化妆品资质",
                "confidence": "中",
                "reason": "产品描述含化妆品相关特征",
                "suggested_next_step": "请准备化妆品资质进行入驻"
            }

    if _contains_keywords(combined_license_text, ["化妆品销售"]):
        return {
            "result": "化妆品资质",
            "confidence": "低",
            "reason": "营业执照经营范围包含'化妆品销售'，但产品特征不明显",
            "suggested_next_step": "建议确认产品类型并准备相应资质"
        }

    # 6. 预包装食品资质
    if _contains_keywords(combined_license_text, ["食品销售", "预包装食品"]):
        return {
            "result": "预包装食品资质",
            "confidence": "高",
            "reason": "营业执照经营范围包含'食品销售'或'预包装食品'",
            "suggested_next_step": "请准备食品经营许可证进行入驻"
        }

    # 7. 无法判断
    return {
        "result": "无法判断，提示人工复核",
        "confidence": "低",
        "reason": "根据营业执照经营范围和产品特征，无法自动判断所需资质",
        "suggested_next_step": "请转人工审核进行资质确认"
    }


def _contains_keywords(text: str, keywords: List[str]) -> bool:
    """
    检查文本中是否包含任意关键词（辅助函数）

    Args:
        text: 待检查的文本
        keywords: 关键词列表

    Returns:
        bool: 是否包含任意关键词
    """
    text_lower = text.lower()
    return any(keyword.lower() in text_lower for keyword in keywords)


# ============================================
# 简单使用示例
# ============================================

if __name__ == "__main__":
    # 示例1：保健食品产品
    result1 = judge_qualification(
        business_scope="保健食品销售、预包装食品销售",
        registered_address="北京市朝阳区",
        enterprise_type="有限责任公司",
        visual_features=["蓝帽子标志"],
        text_features=["维生素C片"],
        approval_numbers=["国食健字G20231234"]
    )
    print("示例1结果:", result1)

    # 示例2：医疗器械产品
    result2 = judge_qualification(
        business_scope="医疗器械经营",
        visual_features=["械字号标识"],
        text_features=["医用口罩"],
        approval_numbers=["X械备20231234"]
    )
    print("示例2结果:", result2)

    # 示例3：进口保健食品
    result3 = judge_qualification(
        business_scope="保健食品销售、进出口贸易",
        visual_features=["蓝帽子标志", "进口商品中文标签"],
        text_features=["进口鱼油"],
        approval_numbers=["国食健注J20231234"]
    )
    print("示例3结果:", result3)

    # 示例4：化妆品
    result4 = judge_qualification(
        business_scope="化妆品销售",
        visual_features=["妆字号标识"],
        text_features=["爽肤水"]
    )
    print("示例4结果:", result4)

    # 示例5：预包装食品
    result5 = judge_qualification(
        business_scope="食品销售、预包装食品销售",
        text_features=["饼干", "零食"]
    )
    print("示例5结果:", result5)