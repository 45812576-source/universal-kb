"""Web app builder builtin tool.

Generates a single-page HTML app using Tailwind CDN + Alpine.js based on a description.
Stores the result in the web_apps table and returns a preview URL.

Input params:
{
  "description": "客户ROI计算器，输入投放金额和转化率，计算ROI和利润",
  "name": "ROI计算器"
}

Output: {"web_app_id": 1, "preview_url": "/api/web-apps/1/preview", "share_url": "/share/token"}
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_BUILD_PROMPT = """你是一个前端开发专家。根据以下需求，生成一个完整的单文件HTML页面。

需求：{description}

要求：
1. 使用 Tailwind CSS CDN（https://cdn.tailwindcss.com）做样式
2. 使用 Alpine.js CDN（https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js）做交互
3. 页面完全自包含，不依赖后端API
4. 包含清晰的标题、输入表单、结果展示
5. 代码简洁易读，注释中文
6. 支持手机端自适应
7. 只返回完整HTML代码，不要任何解释

直接输出完整HTML，从<!DOCTYPE html>开始。"""


async def execute(params: dict) -> dict:
    """Generate a web app and store it in the database."""
    description = params.get("description", "")
    name = params.get("name", "我的小工具")
    # db_session and user_id are injected by the tool executor context
    # Since builtin tools are called directly, we use the LLM gateway here
    from app.services.llm_gateway import llm_gateway
    from app.database import SessionLocal
    from app.models.web_app import WebApp
    import secrets

    # Generate HTML via LLM
    # We need a model config — use the default
    db = SessionLocal()
    try:
        model_config = llm_gateway.get_config(db)
        html_content = await llm_gateway.chat(
            model_config=model_config,
            messages=[
                {"role": "user", "content": _BUILD_PROMPT.format(description=description)},
            ],
            temperature=0.3,
            max_tokens=8000,
        )

        # Strip markdown code fences if LLM wrapped it
        html_content = html_content.strip()
        if html_content.startswith("```"):
            import re
            html_content = re.sub(r"^```[a-z]*\n?", "", html_content)
            html_content = re.sub(r"\n?```$", "", html_content).strip()

        share_token = secrets.token_urlsafe(16)
        web_app = WebApp(
            name=name,
            description=description,
            html_content=html_content,
            share_token=share_token,
            is_public=False,
        )
        db.add(web_app)
        db.commit()
        db.refresh(web_app)

        return {
            "web_app_id": web_app.id,
            "name": web_app.name,
            "preview_url": f"/api/web-apps/{web_app.id}/preview",
            "share_url": f"/share/{share_token}",
            "message": f"小工具「{name}」已生成，点击链接预览",
        }
    finally:
        db.close()
