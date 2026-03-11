"""
自动阅读每个 Skill 的 system_prompt，推导 required_inputs。
运行: .venv/bin/python3 scripts/derive_required_inputs.py
"""
import sys, os, json, asyncio, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models.skill import Skill
from app.services.llm_gateway import llm_gateway

_DERIVE_PROMPT = """你是一个 AI Skill 分析师。

下面是一个 AI Skill 的完整系统提示词，它描述了这个 Skill 能做什么、需要用户提供什么信息才能输出高质量结果。

请分析：如果用户提供了哪些信息，这个 Skill 才能输出一份置信度 ≥60 的报告（即基本可用）？
把这些必要信息定义为 required_inputs。

要求：
- 每项信息用一个 JSON 对象描述，字段含义：
  - key: 英文小写标识，如 product / target_user / channel
  - label: 中文简短标签，如 具体产品 / 目标用户 / 销售渠道
  - desc: 一句话说明需要用户提供什么（不超过25字）
  - example: 一个具体示例（不超过20字）
  - score: 这项信息占总置信度的分值（所有项加起来必须 = 100）
- 只列真正影响输出质量的核心信息，不要列可推断或可选的信息
- 最少 3 项，最多 8 项

Skill 名称: {skill_name}
Skill 说明: {skill_desc}

系统提示词:
---
{system_prompt}
---

只返回 JSON 数组，格式：
[
  {{"key": "xxx", "label": "xxx", "desc": "xxx", "example": "xxx", "score": 数字}},
  ...
]"""


async def derive_for_skill(skill: Skill) -> list[dict]:
    sv = skill.versions[0] if skill.versions else None
    if not sv or not sv.system_prompt:
        print(f"  [skip] {skill.name} — no system_prompt")
        return []

    prompt = _DERIVE_PROMPT.format(
        skill_name=skill.name,
        skill_desc=skill.description or "",
        system_prompt=sv.system_prompt[:4000],
    )
    try:
        lite = llm_gateway.get_lite_config()
        raw = await llm_gateway.chat(
            model_config=lite,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=1000,
        )
        raw = re.sub(r"```json|```", "", raw).strip()
        result = json.loads(raw)
        # 校正 score 总和为 100
        total = sum(item.get("score", 0) for item in result)
        if total != 100 and total > 0:
            for item in result:
                item["score"] = round(item.get("score", 0) * 100 / total)
        return result
    except Exception as e:
        print(f"  [error] {skill.name}: {e}")
        return []


async def main():
    db = SessionLocal()
    skills = db.query(Skill).all()
    print(f"Processing {len(skills)} skills...\n")

    for skill in skills:
        print(f"→ {skill.name}")
        inputs = await derive_for_skill(skill)
        if inputs:
            sv = skill.versions[0]
            sv.required_inputs = inputs
            db.commit()
            for item in inputs:
                print(f"   [{item['score']}分] {item['label']}: {item['desc']}")
        print()

    db.close()
    print("Done.")

asyncio.run(main())
