"""种子数据：组织架构 + 超管账号 + 默认模型配置 + 内置工具"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models.user import Department, User, Role
from app.models.skill import ModelConfig
from app.models.tool import ToolRegistry, ToolType
from passlib.hash import bcrypt


def seed():
    db = SessionLocal()

    # 清理已有数据（幂等）
    if db.query(User).filter(User.username == "admin").first():
        print("Seed data already exists, skipping.")
        db.close()
        return

    # ─── 组织架构（三级：事业部 → 一级部门 → 二级部门）───────────────────────

    # 事业部级（顶层）
    corp = Department(name="公司经营发展中心", category="后台", business_unit="公司经营发展中心")
    cid_bu = Department(name="国内电商广告事业部", category="前台", business_unit="国内电商广告事业部")
    dic_bu = Department(name="AI云浏览器事业部", category="前台", business_unit="AI云浏览器事业部")
    db.add_all([corp, cid_bu, dic_bu])
    db.flush()

    # 一级部门
    # 后台 - 公司经营发展中心
    d_ceo_office = Department(name="总裁办", category="后台", business_unit="公司经营发展中心", parent_id=corp.id)
    d_finance = Department(name="财务部", category="后台", business_unit="公司经营发展中心", parent_id=corp.id)
    d_hr_admin = Department(name="人力资源&行政", category="后台", business_unit="公司经营发展中心", parent_id=corp.id)
    # 前台 - CID
    d_cid_biz = Department(name="CID商业化", category="前台", business_unit="国内电商广告事业部", parent_id=cid_bu.id)
    d_mall_ops = Department(name="商城运营部", category="前台", business_unit="国内电商广告事业部", parent_id=cid_bu.id)
    d_ecom_ops = Department(name="电商投流运营部", category="前台", business_unit="国内电商广告事业部", parent_id=cid_bu.id)
    # 前台 - DIC
    d_dic_biz = Department(name="DIC商业化", category="前台", business_unit="AI云浏览器事业部", parent_id=dic_bu.id)
    # 中台 - CID产研
    d_cid_rd = Department(name="CID产研", category="中台", business_unit="国内电商广告事业部", parent_id=cid_bu.id)
    # 中台 - DIC产研
    d_dic_rd = Department(name="DIC产研", category="中台", business_unit="AI云浏览器事业部", parent_id=dic_bu.id)

    db.add_all([d_ceo_office, d_finance, d_hr_admin,
                d_cid_biz, d_mall_ops, d_ecom_ops,
                d_dic_biz, d_cid_rd, d_dic_rd])
    db.flush()

    # 二级部门
    sub_depts = [
        # CID商业化 下属
        Department(name="媒介服务部", category="前台", business_unit="国内电商广告事业部", parent_id=d_cid_biz.id),
        Department(name="商务拓展及客户管理部", category="前台", business_unit="国内电商广告事业部", parent_id=d_cid_biz.id),
        Department(name="技术产品交付部", category="前台", business_unit="国内电商广告事业部", parent_id=d_cid_biz.id),
        # 电商投流运营部 下属
        Department(name="创意组", category="前台", business_unit="国内电商广告事业部", parent_id=d_ecom_ops.id),
        Department(name="广州运营部", category="前台", business_unit="国内电商广告事业部", parent_id=d_ecom_ops.id),
        Department(name="厦门运营部", category="前台", business_unit="国内电商广告事业部", parent_id=d_ecom_ops.id),
        # CID产研 下属
        Department(name="灵眸独立项目", category="中台", business_unit="国内电商广告事业部", parent_id=d_cid_rd.id),
        Department(name="前端研发部", category="中台", business_unit="国内电商广告事业部", parent_id=d_cid_rd.id),
        Department(name="后端研发部", category="中台", business_unit="国内电商广告事业部", parent_id=d_cid_rd.id),
        Department(name="产品部", category="中台", business_unit="国内电商广告事业部", parent_id=d_cid_rd.id),
        Department(name="测试部", category="中台", business_unit="国内电商广告事业部", parent_id=d_cid_rd.id),
        # DIC商业化 下属
        Department(name="产品运营部", category="前台", business_unit="AI云浏览器事业部", parent_id=d_dic_biz.id),
        # DIC产研 下属
        # 注意：DIC产研的二级部门名与CID产研有重名，加前缀区分
        Department(name="DIC前端研发部", category="中台", business_unit="AI云浏览器事业部", parent_id=d_dic_rd.id),
        Department(name="DIC后端研发部", category="中台", business_unit="AI云浏览器事业部", parent_id=d_dic_rd.id),
        Department(name="DIC产品部", category="中台", business_unit="AI云浏览器事业部", parent_id=d_dic_rd.id),
        Department(name="DIC测试部", category="中台", business_unit="AI云浏览器事业部", parent_id=d_dic_rd.id),
    ]
    db.add_all(sub_depts)
    db.flush()

    # 超级管理员
    admin = User(
        username="admin",
        password_hash=bcrypt.hash("admin123"),
        display_name="超级管理员",
        role=Role.SUPER_ADMIN,
    )
    db.add(admin)

    # 默认模型配置
    default_model = ModelConfig(
        name="MiniMax-M2.5",
        provider="minimax",
        model_id="MiniMax-M2.5",
        api_base="https://api.minimax.io/v1",
        api_key_env="MINIMAX_API_KEY",
        max_tokens=4096,
        temperature="0.7",
        is_default=True,
    )
    db.add(default_model)

    # 内置工具注册
    _seed_tools(db)

    db.commit()
    db.close()
    print("Seed complete: org structure, admin user, default model config, builtin tools.")


def _seed_tools(db):
    """Register builtin tools if not already present."""
    builtin_tools = [
        {
            "name": "ppt_generator",
            "display_name": "PPT生成器",
            "description": "根据结构化内容生成PowerPoint演示文稿",
            "tool_type": ToolType.BUILTIN,
            "config": {"module": "app.tools.ppt_generator", "function": "execute"},
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "演示文稿标题"},
                    "slides": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "content": {"type": "string"},
                            },
                        },
                    },
                },
                "required": ["title", "slides"],
            },
            "output_format": "file",
        },
        {
            "name": "excel_generator",
            "display_name": "Excel生成器",
            "description": "根据表格数据生成Excel文件",
            "tool_type": ToolType.BUILTIN,
            "config": {"module": "app.tools.excel_generator", "function": "execute"},
            "input_schema": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名（不含扩展名）"},
                    "sheets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "headers": {"type": "array", "items": {"type": "string"}},
                                "rows": {"type": "array"},
                            },
                        },
                    },
                },
                "required": ["sheets"],
            },
            "output_format": "file",
        },
        {
            "name": "web_builder",
            "display_name": "Web小工具搭建",
            "description": "根据需求描述生成可分享的单页Web应用",
            "tool_type": ToolType.BUILTIN,
            "config": {"module": "app.tools.web_builder", "function": "execute"},
            "input_schema": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "小工具功能描述"},
                    "name": {"type": "string", "description": "小工具名称"},
                },
                "required": ["description"],
            },
            "output_format": "json",
        },
        {
            "name": "chart_generator",
            "display_name": "图表生成器",
            "description": "生成柱状图、折线图、饼图或双轴组合图，输出PNG文件",
            "tool_type": ToolType.BUILTIN,
            "config": {"module": "app.tools.chart_generator", "function": "execute"},
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "图表标题"},
                    "chart_type": {
                        "type": "string",
                        "enum": ["bar", "line", "pie", "bar_line"],
                        "description": "图表类型：bar柱状图 | line折线图 | pie饼图 | bar_line双轴组合",
                    },
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "X轴或饼图的标签列表",
                    },
                    "datasets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "values": {"type": "array", "items": {"type": "number"}},
                            },
                            "required": ["name", "values"],
                        },
                        "description": "数据集列表，每个数据集含name和values",
                    },
                    "x_label": {"type": "string", "description": "X轴标签（可选）"},
                    "y_label": {"type": "string", "description": "Y轴标签（可选）"},
                },
                "required": ["title", "chart_type", "labels", "datasets"],
            },
            "output_format": "file",
        },
        {
            "name": "doc_generator",
            "display_name": "Word文档生成器",
            "description": "生成Word文档，支持标题、段落、列表、表格等结构化内容",
            "tool_type": ToolType.BUILTIN,
            "config": {"module": "app.tools.doc_generator", "function": "execute"},
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "文档标题（封面大标题）"},
                    "sections": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["heading", "paragraph", "list", "table"],
                                    "description": "章节类型",
                                },
                                "level": {"type": "integer", "description": "标题级别1-3，仅heading使用"},
                                "text": {"type": "string", "description": "文本内容，heading/paragraph使用"},
                                "bold": {"type": "boolean", "description": "是否加粗，paragraph使用"},
                                "italic": {"type": "boolean", "description": "是否斜体，paragraph使用"},
                                "items": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "列表项，list使用",
                                },
                                "headers": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "表头，table使用",
                                },
                                "rows": {
                                    "type": "array",
                                    "description": "数据行，table使用",
                                },
                            },
                            "required": ["type"],
                        },
                        "description": "文档章节列表",
                    },
                },
                "required": ["title", "sections"],
            },
            "output_format": "file",
        },
        {
            "name": "task_batch_creator",
            "display_name": "批量创建任务",
            "description": "从Skill结构化输出批量创建团队待办任务，支持按姓名指派负责人",
            "tool_type": ToolType.BUILTIN,
            "config": {"module": "app.tools.task_batch_creator", "function": "execute"},
            "input_schema": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "任务标题"},
                                "description": {"type": "string", "description": "任务详情"},
                                "priority": {
                                    "type": "string",
                                    "enum": ["urgent_important", "important", "urgent", "neither"],
                                    "description": "优先级",
                                },
                                "assignee_name": {"type": "string", "description": "负责人姓名（可选）"},
                                "due_date": {"type": "string", "description": "截止日期，格式YYYY-MM-DD（可选）"},
                            },
                            "required": ["title"],
                        },
                        "description": "任务列表",
                    },
                    "source_skill_id": {"type": "integer", "description": "来源Skill ID（可选）"},
                    "batch_tag": {"type": "string", "description": "批次标签，用于筛选（可选）"},
                },
                "required": ["tasks"],
            },
            "output_format": "json",
        },
        {
            "name": "data_table_writer",
            "display_name": "业务表写入",
            "description": "将Skill结构化输出写入已注册的业务表，带安全校验和审计日志",
            "tool_type": ToolType.BUILTIN,
            "config": {"module": "app.tools.data_table_writer", "function": "execute"},
            "input_schema": {
                "type": "object",
                "properties": {
                    "table_name": {"type": "string", "description": "目标业务表名（需在business_tables注册）"},
                    "rows": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "要写入的行数据列表，每行为字段名→值的字典",
                    },
                },
                "required": ["table_name", "rows"],
            },
            "output_format": "json",
        },
        {
            "name": "lark_approval",
            "display_name": "飞书审批",
            "description": "发起飞书审批流程（请假、报销、采购等），审批人在飞书端操作，结果自动回调",
            "tool_type": ToolType.BUILTIN,
            "config": {
                "module": "app.tools.lark_approval",
                "function": "execute",
                "usage_hint": "当用户需要发起请假、报销、采购等审批流程时调用此工具",
            },
            "input_schema": {
                "type": "object",
                "properties": {
                    "approval_code": {
                        "type": "string",
                        "description": "飞书审批定义 code（审批模板标识，从审批管理获取）",
                    },
                    "title": {
                        "type": "string",
                        "description": "审批标题，如「张三请假申请」",
                    },
                    "form_data": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "表单控件 ID"},
                                "type": {"type": "string", "description": "控件类型，如 input / textarea / date / number"},
                                "value": {"type": "string", "description": "控件值"},
                            },
                            "required": ["id", "type", "value"],
                        },
                        "description": "飞书审批表单数据",
                    },
                    "urgency": {
                        "type": "string",
                        "enum": ["normal", "urgent"],
                        "description": "紧急程度（可选，默认 normal）",
                    },
                },
                "required": ["approval_code", "title", "form_data"],
            },
            "output_format": "json",
        },
    ]

    for tool_data in builtin_tools:
        if db.query(ToolRegistry).filter(ToolRegistry.name == tool_data["name"]).first():
            continue
        tool = ToolRegistry(**tool_data)
        db.add(tool)
    db.flush()


if __name__ == "__main__":
    seed()
