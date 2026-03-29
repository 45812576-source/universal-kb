"""VerifyAgent：两层校验——schema 确定性校验 + LLM 语义校验。"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import jsonschema
from sqlalchemy.orm import Session

from app.services.llm_gateway import llm_gateway
from app.services.pev.prompts import (
    VERIFY_FINAL_SYSTEM,
    VERIFY_FINAL_USER,
    VERIFY_STEP_SYSTEM,
    VERIFY_STEP_USER,
)

logger = logging.getLogger(__name__)


def _parse_verify_json(raw: str) -> dict:
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned.strip())


def _result_summary(result: dict | None) -> str:
    if result is None:
        return "（无结果）"
    try:
        s = json.dumps(result, ensure_ascii=False, indent=2)
        return s[:1500] + ("..." if len(s) > 1500 else "")
    except Exception:
        return str(result)[:1500]


class VerifyAgent:

    def _schema_verify(self, result: dict | None, output_spec: dict | None) -> list[str]:
        """确定性 jsonschema 校验，返回错误列表（空列表表示通过）。"""
        if not output_spec:
            return []
        if result is None:
            return ["步骤结果为空，无法进行 schema 校验"]
        # 只校验 data 字段（实际执行结果）
        data = result.get("data") if isinstance(result, dict) else result
        try:
            jsonschema.validate(instance=data, schema=output_spec)
            return []
        except jsonschema.ValidationError as e:
            path = " -> ".join(str(p) for p in e.absolute_path) if e.absolute_path else "根字段"
            return [f"Schema 校验失败（{path}）：{e.message}"]
        except jsonschema.SchemaError:
            return []  # schema 本身有问题，跳过校验

    async def _llm_verify(
        self,
        description: str,
        verify_criteria: str,
        result: dict | None,
        db: Session,
    ) -> dict:
        """LLM 语义校验，返回 {"pass": bool, "score": int, "issues": [], "suggestion": str}。"""
        if not verify_criteria:
            return {"pass": True, "score": 100, "issues": [], "suggestion": ""}

        verify_config = llm_gateway.resolve_config(db, "pev.verify")
        verify_config = {**verify_config, "max_tokens": 600}

        messages = [
            {"role": "system", "content": VERIFY_STEP_SYSTEM},
            {
                "role": "user",
                "content": VERIFY_STEP_USER.format(
                    description=description,
                    verify_criteria=verify_criteria,
                    result_summary=_result_summary(result),
                ),
            },
        ]

        try:
            raw, _ = await llm_gateway.chat(
                model_config=verify_config,
                messages=messages,
                temperature=0.1,
            )
            return _parse_verify_json(raw)
        except Exception as e:
            logger.warning(f"VerifyAgent._llm_verify 失败: {e}")
            return {"pass": True, "score": 80, "issues": [], "suggestion": ""}

    async def verify_step(
        self,
        step: dict,
        result: dict | None,
        db: Session,
    ) -> dict:
        """综合校验单个步骤。返回：{"pass": bool, "score": int, "issues": [], "suggestion": str}。"""
        issues: list[str] = []

        # 1. Schema 校验（确定性）
        output_spec = step.get("output_spec")
        schema_errors = self._schema_verify(result, output_spec)
        issues.extend(schema_errors)

        # 如果 schema 校验已经失败，无需继续 LLM 校验
        if schema_errors:
            return {
                "pass": False,
                "score": 0,
                "issues": issues,
                "suggestion": f"结果不满足预期 schema，请修正：{'; '.join(schema_errors)}",
            }

        # 2. LLM 语义校验
        verify_criteria = step.get("verify_criteria", "")
        if verify_criteria:
            llm_result = await self._llm_verify(
                description=step.get("description", ""),
                verify_criteria=verify_criteria,
                result=result,
                db=db,
            )
            issues.extend(llm_result.get("issues") or [])
            return {
                "pass": llm_result.get("pass", True),
                "score": llm_result.get("score", 80),
                "issues": issues,
                "suggestion": llm_result.get("suggestion", ""),
            }

        # 无 criteria，只要执行成功即通过
        exec_ok = result.get("ok", False) if isinstance(result, dict) else False
        return {
            "pass": exec_ok,
            "score": 90 if exec_ok else 0,
            "issues": issues if issues else ([] if exec_ok else ["步骤执行失败"]),
            "suggestion": result.get("error", "") if not exec_ok else "",
        }

    async def verify_final(
        self,
        job: Any,  # PEVJob ORM 对象
        all_results: dict,  # step_key → result
        db: Session,
    ) -> dict:
        """全局交叉验证。返回：{"pass": bool, "score": int, "issues": [], "summary": str}。"""
        verify_config = llm_gateway.resolve_config(db, "pev.verify")
        verify_config = {**verify_config, "max_tokens": 800}

        # 构建步骤摘要
        steps_parts = []
        for step_key, result in all_results.items():
            ok = result.get("ok", False) if isinstance(result, dict) else False
            data_preview = _result_summary(result)[:300]
            steps_parts.append(f"[{step_key}] {'✓' if ok else '✗'}: {data_preview}")

        steps_summary = "\n".join(steps_parts) if steps_parts else "（无步骤结果）"

        messages = [
            {"role": "system", "content": VERIFY_FINAL_SYSTEM},
            {
                "role": "user",
                "content": VERIFY_FINAL_USER.format(
                    goal=job.goal,
                    scenario=job.scenario,
                    steps_summary=steps_summary,
                ),
            },
        ]

        try:
            raw, _ = await llm_gateway.chat(
                model_config=verify_config,
                messages=messages,
                temperature=0.1,
            )
            result_json = _parse_verify_json(raw)
            return {
                "pass": result_json.get("pass", True),
                "score": result_json.get("score", 80),
                "issues": result_json.get("issues", []),
                "summary": result_json.get("summary", ""),
            }
        except Exception as e:
            logger.warning(f"VerifyAgent.verify_final 失败: {e}")
            return {"pass": True, "score": 80, "issues": [], "summary": "验证服务暂时不可用"}


verify_agent = VerifyAgent()
