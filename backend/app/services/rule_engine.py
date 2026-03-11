"""Rule engine for structured Skill mode: formula / lookup / conditional evaluation.

Rules are stored in Skill.tools JSON field as a list of rule objects:
[
  {
    "type": "formula",
    "name": "rebate_calc",
    "trigger_keywords": ["返点", "佣金", "计算"],
    "formula": "spend * rebate_rate",
    "param_map": {"spend": "投放金额", "rebate_rate": "返点比例"}
  },
  {
    "type": "lookup",
    "name": "tier_lookup",
    "trigger_keywords": ["阶梯", "等级"],
    "table": [[0, 100000, 0.05], [100000, 500000, 0.08], [500000, None, 0.1]],
    "input_param": "spend",
    "output_label": "返点比例"
  },
  {
    "type": "conditional",
    "name": "budget_check",
    "trigger_keywords": ["预算", "够不够"],
    "conditions": [
      {"if": "budget >= target", "then": "预算充足"},
      {"else": "预算不足，缺口为 {target - budget}"}
    ]
  }
]
"""
from __future__ import annotations

import ast
import logging
import operator
import re
from typing import Any

from sqlalchemy.orm import Session

from app.models.skill import Skill

logger = logging.getLogger(__name__)

# Safe math operators
_SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.Mod: operator.mod,
}

_PARAM_EXTRACT_PROMPT = """从用户消息中提取以下参数的数值。若某参数无法确定，返回 null。
只返回JSON对象，不要任何其他内容。

参数列表（参数名: 中文描述）:
{param_list}

用户消息: {user_message}"""


def _safe_eval(expr: str, variables: dict) -> float:
    """Safely evaluate a math expression with variables."""
    tree = ast.parse(expr.strip(), mode="eval")

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        elif isinstance(node, ast.Constant):
            return node.value
        elif isinstance(node, ast.Name):
            if node.id not in variables:
                raise ValueError(f"Unknown variable: {node.id}")
            return variables[node.id]
        elif isinstance(node, ast.BinOp):
            op_cls = type(node.op)
            if op_cls not in _SAFE_OPS:
                raise ValueError(f"Unsupported operator: {op_cls}")
            return _SAFE_OPS[op_cls](_eval(node.left), _eval(node.right))
        elif isinstance(node, ast.UnaryOp):
            op_cls = type(node.op)
            if op_cls not in _SAFE_OPS:
                raise ValueError(f"Unsupported operator: {op_cls}")
            return _SAFE_OPS[op_cls](_eval(node.operand))
        else:
            raise ValueError(f"Unsupported AST node: {type(node)}")

    return _eval(tree)


class RuleEngine:

    async def try_evaluate(
        self,
        db: Session,
        skill: Skill,
        user_message: str,
        model_config: dict,
    ) -> str | None:
        """Try to match a rule and evaluate it. Returns result string or None if no match."""
        rules = skill.tools  # reuse tools JSON field for rule storage when mode=structured
        if not rules or not isinstance(rules, list):
            return None

        # Find matching rule by trigger keywords
        matched_rule = None
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            keywords = rule.get("trigger_keywords", [])
            if any(kw in user_message for kw in keywords):
                matched_rule = rule
                break

        if not matched_rule:
            return None

        rule_type = matched_rule.get("type")
        try:
            if rule_type == "formula":
                return await self._eval_formula(matched_rule, user_message, model_config)
            elif rule_type == "lookup":
                return await self._eval_lookup(matched_rule, user_message, model_config)
            elif rule_type == "conditional":
                return await self._eval_conditional(matched_rule, user_message, model_config)
        except Exception as e:
            logger.warning(f"Rule evaluation failed for '{matched_rule.get('name')}': {e}")
            return None

        return None

    async def _extract_params(
        self,
        param_map: dict,
        user_message: str,
        model_config: dict,
    ) -> dict:
        """Use LLM to extract parameter values from user message."""
        from app.services.llm_gateway import llm_gateway

        param_list = "\n".join(f"- {k}: {v}" for k, v in param_map.items())
        prompt = _PARAM_EXTRACT_PROMPT.format(
            param_list=param_list, user_message=user_message
        )
        result, _ = await llm_gateway.chat(
            model_config=model_config,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=200,
        )
        import json
        try:
            cleaned = result.strip()
            if cleaned.startswith("```"):
                import re
                cleaned = re.sub(r"^```[a-z]*\n?", "", cleaned)
                cleaned = re.sub(r"\n?```$", "", cleaned)
            return json.loads(cleaned)
        except Exception:
            return {}

    async def _eval_formula(self, rule: dict, user_message: str, model_config: dict) -> str:
        formula = rule.get("formula", "")
        param_map = rule.get("param_map", {})
        output_label = rule.get("output_label", "计算结果")

        params = await self._extract_params(param_map, user_message, model_config)

        # Check all params are available
        missing = [k for k in param_map if params.get(k) is None]
        if missing:
            missing_labels = [param_map[k] for k in missing]
            return f"需要以下参数才能计算：{', '.join(missing_labels)}，请提供。"

        # Convert to float
        variables = {}
        for k in param_map:
            try:
                variables[k] = float(params[k])
            except (TypeError, ValueError):
                return f"参数 {param_map[k]} 的值无效，请输入数字。"

        result = _safe_eval(formula, variables)

        # Format result
        if isinstance(result, float) and result == int(result):
            formatted = f"{int(result):,}"
        else:
            formatted = f"{result:,.4f}".rstrip("0").rstrip(".")

        return f"{output_label}：**{formatted}**\n\n计算公式：`{formula}`\n参数：{', '.join(f'{param_map[k]}={variables[k]:g}' for k in param_map)}"

    async def _eval_lookup(self, rule: dict, user_message: str, model_config: dict) -> str:
        table = rule.get("table", [])  # [[low, high, value], ...]
        input_param = rule.get("input_param", "value")
        output_label = rule.get("output_label", "查询结果")
        param_map = {input_param: rule.get("input_label", input_param)}

        params = await self._extract_params(param_map, user_message, model_config)
        if params.get(input_param) is None:
            return f"请提供{param_map[input_param]}以便查询阶梯。"

        try:
            value = float(params[input_param])
        except (TypeError, ValueError):
            return "输入值无效，请提供数字。"

        for row in table:
            low, high, result = row[0], row[1], row[2]
            if (low is None or value >= low) and (high is None or value < high):
                return f"根据阶梯规则，{param_map[input_param]} = {value:g} 时，{output_label}为 **{result}**"

        return f"输入值 {value} 超出阶梯范围，请检查。"

    async def _eval_conditional(self, rule: dict, user_message: str, model_config: dict) -> str:
        conditions = rule.get("conditions", [])
        param_map = rule.get("param_map", {})

        params = await self._extract_params(param_map, user_message, model_config)
        missing = [k for k in param_map if params.get(k) is None]
        if missing:
            return f"需要以下参数：{', '.join(param_map[k] for k in missing)}，请提供。"

        variables = {k: float(params[k]) for k in param_map if params.get(k) is not None}

        for cond in conditions:
            if "else" in cond:
                template = cond["else"]
                # Simple template substitution for expressions like {target - budget}
                result = _render_template(template, variables)
                return result
            elif "if" in cond and "then" in cond:
                try:
                    if _safe_eval(cond["if"], variables):
                        return _render_template(cond["then"], variables)
                except Exception:
                    continue

        return "条件判断无结果，请检查规则配置。"


def _render_template(template: str, variables: dict) -> str:
    """Replace {expr} patterns in template with evaluated values."""
    def replacer(match):
        expr = match.group(1)
        try:
            val = _safe_eval(expr, variables)
            if isinstance(val, float) and val == int(val):
                return str(int(val))
            return f"{val:g}"
        except Exception:
            return match.group(0)

    return re.sub(r"\{([^}]+)\}", replacer, template)


rule_engine = RuleEngine()
