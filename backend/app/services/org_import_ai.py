"""组织管理导入 AI 整理 — 10 种 import_type 的 prompt 模板 + LLM 调用 + 结果解析"""

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.user import Department, User
from app.models.permission import Position

logger = logging.getLogger(__name__)

# ── Prompt 模板 ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = "你是组织管理数据整理助手。用户会提供从表格中解析的原始数据，你需要整理为标准化的结构化 JSON。只输出 JSON，不要输出其他内容。不要发明数据，不确定的字段标注 \"_uncertain\": true。"

IMPORT_TYPE_PROMPTS: dict[str, str] = {
    "org_structure": """请将以下部门数据整理为标准格式。
输入：原始表格数据 JSON
现有部门列表：{existing_departments}

输出格式（JSON数组）：
[{{
  "name": "部门名称",
  "parent_name": "上级部门名称（如果是顶级部门则为null）",
  "category": "后台/前台/中台",
  "business_unit": "事业部/中心",
  "level": "集团/事业部/中心/部门/组",
  "headcount_budget": 数字或null,
  "lifecycle_status": "active/preparing/frozen/dissolved",
  "code": "部门编码（如原数据没有则根据名称生成拼音缩写）"
}}]

原始数据：
{raw_data}""",

    "roster": """请将以下花名册数据整理为标准格式。
输入：原始表格数据 JSON
现有部门列表：{existing_departments}
现有岗位列表：{existing_positions}

输出格式（JSON数组）：
[{{
  "username": "用户名（拼音或工号）",
  "display_name": "姓名",
  "employee_no": "工号",
  "department_name": "部门名称（需匹配现有部门）",
  "position_name": "岗位名称（需匹配现有岗位）",
  "job_title": "正式职称",
  "job_level": "职级（如P5/M3）",
  "employee_status": "active/probation/resigned/transferred",
  "entry_date": "入职日期（YYYY-MM-DD）",
  "exit_date": "离职日期或null",
  "report_to_name": "上级姓名"
}}]

原始数据：
{raw_data}""",

    "okr": """请将以下 OKR 数据整理为标准格式。
现有部门列表：{existing_departments}
现有用户列表：{existing_users}

输出格式：
{{
  "periods": [{{ "name": "2026 Q2", "period_type": "quarter", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD" }}],
  "objectives": [{{
    "period_name": "周期名称",
    "owner_type": "company/department/user",
    "owner_name": "公司/部门名/用户名",
    "parent_title": "上级目标标题（对齐用）或null",
    "title": "目标描述",
    "weight": 1.0,
    "key_results": [{{
      "title": "KR描述",
      "metric_type": "number/percentage/boolean/milestone",
      "target_value": "目标值",
      "current_value": "当前值",
      "unit": "单位",
      "weight": 1.0,
      "owner_name": "负责人姓名"
    }}]
  }}]
}}

原始数据：
{raw_data}""",

    "kpi": """请将以下 KPI 绩效数据整理为标准格式。
现有用户列表：{existing_users}
现有周期列表：{existing_periods}

输出格式（JSON数组，按人聚合）：
[{{
  "user_name": "姓名或工号",
  "period_name": "周期名称",
  "kpi_items": [{{
    "name": "KPI名称",
    "weight": 0.3,
    "target": "目标值",
    "actual": "实际值",
    "score": 85,
    "metric_type": "number/percentage/boolean",
    "unit": "单位"
  }}],
  "total_score": 85.5,
  "level": "S/A/B/C/D"
}}]

原始数据：
{raw_data}""",

    "dept_mission": """请将以下部门职责数据整理为标准格式。
现有部门列表：{existing_departments}

输出格式（JSON数组）：
[{{
  "department_name": "部门名称",
  "mission_summary": "部门使命一句话",
  "core_functions": [{{"name": "职能名", "description": "描述"}}],
  "upstream_deps": [{{"dept_name": "上游部门", "what_receive": "接收什么"}}],
  "downstream_deliveries": [{{"dept_name": "下游部门", "what_deliver": "交付什么"}}],
  "owned_data_types": ["数据类型1", "数据类型2"]
}}]

原始数据：
{raw_data}""",

    "biz_process": """请将以下业务流程数据整理为标准格式。
现有部门列表：{existing_departments}
现有岗位列表：{existing_positions}

输出格式（JSON数组）：
[{{
  "name": "流程名称",
  "code": "流程编码（拼音缩写）",
  "description": "流程描述",
  "process_nodes": [{{
    "order": 1,
    "name": "节点名称",
    "dept_name": "负责部门",
    "position_name": "负责岗位",
    "input_data": ["输入数据"],
    "output_data": ["输出数据"]
  }}]
}}]

原始数据：
{raw_data}""",

    "terminology": """请将以下业务术语数据整理为标准格式。

输出格式（JSON数组）：
[{{
  "term": "术语",
  "aliases": ["同义词1", "同义词2"],
  "definition": "释义",
  "resource_library_code": "归属资源库code或null",
  "department_name": "归属部门或null"
}}]

原始数据：
{raw_data}""",

    "data_asset": """请将以下数据资产数据整理为标准格式。
现有部门列表：{existing_departments}

输出格式（JSON数组）：
[{{
  "asset_name": "资产名称",
  "asset_code": "资产编码（拼音缩写）",
  "owner_department_name": "责任部门",
  "update_frequency": "realtime/daily/weekly/monthly/manual",
  "consumer_department_names": ["消费部门1", "消费部门2"],
  "resource_library_code": "关联的治理资源库code或null",
  "description": "描述"
}}]

原始数据：
{raw_data}""",

    "collab_matrix": """请将以下跨部门协作数据整理为标准格式。
现有部门列表：{existing_departments}

输出格式（JSON数组）：
[{{
  "dept_a_name": "部门A",
  "dept_b_name": "部门B",
  "frequency": "high/medium/low",
  "scenarios": ["典型协作场景1", "场景2"]
}}]

原始数据：
{raw_data}""",

    "access_matrix": """请将以下岗位数据域访问矩阵整理为标准格式。
现有岗位列表：{existing_positions}

输出格式（JSON数组）：
[{{
  "position_name": "岗位名称",
  "data_domain": "client/project/financial/creative/hr/knowledge",
  "access_range": "none/own/own_client/assigned/department/all",
  "excluded_fields": ["排除字段1"]
}}]

原始数据：
{raw_data}""",
}


def get_existing_context(db: Session) -> dict[str, str]:
    """获取现有组织数据作为 AI 整理的上下文参考"""
    departments = db.query(Department).all()
    dept_list = [{"id": d.id, "name": d.name, "parent_id": d.parent_id} for d in departments]

    positions = db.query(Position).all()
    pos_list = [{"id": p.id, "name": p.name, "department_id": p.department_id} for p in positions]

    users = db.query(User).filter(User.is_active == True).all()  # noqa: E712
    user_list = [{"id": u.id, "display_name": u.display_name, "department_id": u.department_id} for u in users]

    return {
        "existing_departments": json.dumps(dept_list, ensure_ascii=False),
        "existing_positions": json.dumps(pos_list, ensure_ascii=False),
        "existing_users": json.dumps(user_list, ensure_ascii=False),
        "existing_periods": "[]",
    }


async def ai_parse_import_data(
    db: Session,
    import_type: str,
    raw_data: Any,
    model_config: dict | None = None,
) -> tuple[Any, str]:
    """调用 LLM 整理导入数据

    Returns:
        (parsed_data, parse_note) — 结构化数据 + AI 整理说明
    """
    prompt_template = IMPORT_TYPE_PROMPTS.get(import_type)
    if not prompt_template:
        return raw_data, f"未知导入类型 {import_type}，跳过 AI 整理"

    context = get_existing_context(db)
    context["raw_data"] = json.dumps(raw_data, ensure_ascii=False, default=str)

    user_prompt = prompt_template.format(**context)

    # 尝试调用 LLM
    if model_config:
        try:
            from app.services.llm_gateway import LLMGateway
            gateway = LLMGateway()
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]
            content, _usage = await gateway.chat(model_config, messages, temperature=0.1, max_tokens=8192)

            # 提取 JSON
            parsed = _extract_json(content)
            if parsed is not None:
                note = f"AI 整理完成。使用模型: {model_config.get('model_id', 'unknown')}"
                return parsed, note
            else:
                return raw_data, f"AI 返回了非 JSON 内容，使用原始数据。AI 输出摘要: {content[:200]}"
        except Exception as e:
            logger.exception(f"AI parse failed for import_type={import_type}")
            return raw_data, f"AI 整理失败 ({e})，使用原始数据"
    else:
        return raw_data, "未配置 LLM，跳过 AI 整理，使用原始数据"


def _extract_json(text: str) -> Any:
    """从 LLM 输出中提取 JSON"""
    text = text.strip()

    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 尝试提取 ```json ... ``` 块
    import re
    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 尝试找到第一个 [ 或 { 开始的 JSON
    for i, ch in enumerate(text):
        if ch in ('[', '{'):
            try:
                return json.loads(text[i:])
            except json.JSONDecodeError:
                continue

    return None
