"""PEVOrchestrator：协调 Plan-Execute-Verify 三阶段循环。"""
from __future__ import annotations

import datetime
import logging
from typing import AsyncIterator, Any

from sqlalchemy.orm import Session

from app.models.pev_job import PEVJob, PEVJobStatus, PEVStep, PEVStepStatus
from app.services.pev.plan_agent import plan_agent
from app.services.pev.execute_agent import execute_agent
from app.services.pev.verify_agent import verify_agent
from app.services.pev.ref_resolver import topological_sort, resolve_inputs
from app.services.pev.prompts import UPGRADE_CHECK_SYSTEM, UPGRADE_CHECK_USER
from app.services.llm_gateway import llm_gateway

logger = logging.getLogger(__name__)

_DEFAULT_MAX_RETRIES = 2
_DEFAULT_REPLAN_BUDGET = 1


class PEVOrchestrator:

    # ── 主编排循环 ────────────────────────────────────────────────────────────

    async def run(
        self,
        db: Session,
        job: PEVJob,
    ) -> AsyncIterator[dict]:
        """执行完整 PEV 循环，yield SSE 事件 dict。"""
        max_retries = (job.config or {}).get("max_retries", _DEFAULT_MAX_RETRIES)
        skip_verify = (job.config or {}).get("skip_verify", False)
        replan_budget = (job.config or {}).get("replan_budget", _DEFAULT_REPLAN_BUDGET)

        yield {"event": "pev_start", "data": {"job_id": job.id, "scenario": job.scenario, "goal": job.goal}}

        # ─── Phase 1: Planning ────────────────────────────────────────────────
        job.status = PEVJobStatus.PLANNING
        job.started_at = datetime.datetime.utcnow()
        db.commit()

        try:
            plan = await plan_agent.generate_plan(
                goal=job.goal,
                scenario=job.scenario,
                context=job.context or {},
                db=db,
            )
        except Exception as e:
            for ev in self._fail(db, job, f"计划生成失败: {e}"):
                yield ev
            return

        # 持久化 steps
        raw_steps = plan.get("steps") or []
        try:
            sorted_steps = topological_sort(raw_steps)
        except ValueError as e:
            for ev in self._fail(db, job, str(e)):
                yield ev
            return

        for idx, s in enumerate(sorted_steps):
            step = PEVStep(
                job_id=job.id,
                order_index=idx,
                step_key=s.get("step_key", f"step_{idx}"),
                step_type=s.get("step_type", "llm_generate"),
                description=s.get("description", ""),
                depends_on=s.get("depends_on") or [],
                input_spec=s.get("input_spec") or {},
                output_spec=s.get("output_spec") or {},
                verify_criteria=s.get("verify_criteria", ""),
            )
            db.add(step)

        job.plan = plan
        job.total_steps = len(sorted_steps)
        db.commit()

        yield {
            "event": "pev_plan_ready",
            "data": {
                "step_count": len(sorted_steps),
                "steps": [
                    {
                        "step_key": s.get("step_key"),
                        "description": s.get("description", ""),
                        "step_type": s.get("step_type", "llm_generate"),
                    }
                    for s in sorted_steps
                ],
            },
        }

        # ─── Phase 2: Executing ───────────────────────────────────────────────
        job.status = PEVJobStatus.EXECUTING
        db.commit()

        # 重新读取 steps（已持久化的 ORM 对象）
        steps = (
            db.query(PEVStep)
            .filter(PEVStep.job_id == job.id)
            .order_by(PEVStep.order_index)
            .all()
        )

        context: dict = dict(job.context or {})

        for step in steps:
            job.current_step_index = step.order_index
            db.commit()

            # 重试循环
            retry_count = 0
            step_passed = False
            last_verify_result = None

            while retry_count <= max_retries:
                # 解析 $ref 输入
                resolved_inputs = resolve_inputs(step.input_spec or {}, context)

                # 注入重试建议
                if retry_count > 0 and last_verify_result:
                    suggestion = last_verify_result.get("suggestion", "")
                    if suggestion:
                        resolved_inputs["_retry_suggestion"] = suggestion

                step.status = PEVStepStatus.RUNNING
                step.retry_count = retry_count
                db.commit()

                if retry_count == 0:
                    yield {
                        "event": "pev_step_start",
                        "data": {"step_key": step.step_key, "step_type": step.step_type},
                    }
                else:
                    yield {
                        "event": "pev_step_retry",
                        "data": {
                            "step_key": step.step_key,
                            "retry": retry_count,
                            "suggestion": resolved_inputs.get("_retry_suggestion", ""),
                        },
                    }

                # 执行
                step_dict = {
                    "step_key": step.step_key,
                    "step_type": step.step_type,
                    "description": step.description,
                    "output_spec": step.output_spec,
                    "verify_criteria": step.verify_criteria,
                }
                try:
                    result = await execute_agent.execute_step(step_dict, resolved_inputs, job, db)
                except Exception as e:
                    result = {"ok": False, "data": None, "error": str(e)}

                step.result = result
                db.commit()

                yield {
                    "event": "pev_step_result",
                    "data": {
                        "step_key": step.step_key,
                        "ok": result.get("ok"),
                        "error": result.get("error"),
                    },
                }

                # ─── Phase 3: Verifying（每步）──────────────────────────────
                # crawl/sub_task 类型：执行成功即视为通过，不做 LLM 语义校验
                _no_llm_verify = step.step_type in ("crawl", "sub_task")
                if skip_verify or _no_llm_verify or not result.get("ok"):
                    if result.get("ok"):
                        step.status = PEVStepStatus.PASSED
                        step_passed = True
                    else:
                        step.status = PEVStepStatus.FAILED
                        last_verify_result = {"pass": False, "suggestion": result.get("error", "")}
                    db.commit()
                else:
                    job.status = PEVJobStatus.VERIFYING
                    db.commit()

                    step_dict = {
                        "step_key": step.step_key,
                        "step_type": step.step_type,
                        "description": step.description,
                        "output_spec": step.output_spec,
                        "verify_criteria": step.verify_criteria,
                    }
                    verify_result = await verify_agent.verify_step(step_dict, result, db)
                    step.verify_result = verify_result
                    last_verify_result = verify_result

                    if verify_result.get("pass"):
                        step.status = PEVStepStatus.PASSED
                        step_passed = True
                    else:
                        step.status = PEVStepStatus.FAILED

                    db.commit()
                    job.status = PEVJobStatus.EXECUTING
                    db.commit()

                if step_passed:
                    break

                retry_count += 1

            # 步骤最终失败（重试耗尽）
            if not step_passed:
                # 尝试 replan
                if replan_budget > 0:
                    replan_budget -= 1
                    yield {
                        "event": "pev_replan",
                        "data": {
                            "step_key": step.step_key,
                            "remaining_replan_budget": replan_budget,
                        },
                    }
                    try:
                        new_plan = await plan_agent.replan(
                            original_plan=job.plan or {},
                            failed_step={"step_key": step.step_key, "description": step.description},
                            verify_feedback=(last_verify_result or {}).get("suggestion", "步骤失败"),
                            context=context,
                            db=db,
                        )
                        # 更新 job plan 并重置后续 steps（简化：直接追加新 steps）
                        job.plan = new_plan
                        db.commit()
                        # 标记当前步骤 skipped，继续后续新 steps
                        step.status = PEVStepStatus.SKIPPED
                        db.commit()
                        continue
                    except Exception as e:
                        logger.error(f"Replan 失败: {e}")

                # replan 耗尽或失败 → Job FAILED
                for ev in self._fail(db, job, f"步骤 '{step.step_key}' 执行失败且无法恢复"):
                    yield ev
                return

            # 步骤成功：写入 context
            context[step.step_key] = result
            job.context = dict(context)
            job.completed_steps = (job.completed_steps or 0) + 1
            db.commit()

        # ─── Phase 3: Final Verification ─────────────────────────────────────
        if not skip_verify:
            final_result = await verify_agent.verify_final(job, context, db)
        else:
            final_result = {"pass": True, "score": 100, "issues": [], "summary": "已跳过最终验证"}

        # 完成
        job.status = PEVJobStatus.COMPLETED if final_result.get("pass") else PEVJobStatus.FAILED
        job.finished_at = datetime.datetime.utcnow()
        db.commit()

        yield {
            "event": "pev_done",
            "data": {
                "job_id": job.id,
                "status": job.status.value,
                "score": final_result.get("score"),
                "summary": final_result.get("summary", ""),
                "issues": final_result.get("issues", []),
            },
        }

    # ── 辅助：失败终止 ────────────────────────────────────────────────────────

    def _fail(self, db: Session, job: PEVJob, message: str):
        """将 Job 标记为 FAILED 并 yield error 事件（生成器辅助）。"""
        job.status = PEVJobStatus.FAILED
        job.finished_at = datetime.datetime.utcnow()
        db.commit()
        return [{"event": "pev_error", "data": {"job_id": job.id, "message": message}}]

    # ── should_upgrade ────────────────────────────────────────────────────────

    async def should_upgrade(
        self,
        user_message: str,
        skill: Any | None,
        conv: Any,
        db: Session,
    ) -> str | None:
        """判断是否应升级到 PEV 引擎。返回 scenario 字符串或 None。"""
        # ── 规则短路：大部分消息无需 LLM 判断 ──
        msg = user_message.strip()
        if len(msg) < 50:
            return None

        # 多步骤关键词检查：无论有无 skill，都要先过关键词门槛再走 LLM
        _MULTI_STEP_KEYWORDS = ("然后", "接着", "分步", "依次", "第一步", "步骤",
                                 "再生成", "再制作", "串联", "多个步骤", "自动执行")
        if not any(kw in msg for kw in _MULTI_STEP_KEYWORDS):
            return None

        # 明确排除：情报采集关键词不存在时 intel 不可能成立
        _INTEL_KEYWORDS = ("采集", "调研", "情报", "多个信息源", "搜集", "汇总分析")
        # 如果没有明显的 intel 意图且没有 skill_chain/task_decomp 意图，也直接排除
        # （此处只做第一层过滤，最终由 LLM 判断）

        try:
            lite_config = llm_gateway.get_lite_config()
            skill_name = skill.name if skill else "（无）"
            # 安全获取 history_count，避免 lazy load 问题
            try:
                from app.models.conversation import Message
                history_count = db.query(Message).filter(
                    Message.conversation_id == conv.id
                ).count()
            except Exception:
                history_count = 0

            messages = [
                {"role": "system", "content": UPGRADE_CHECK_SYSTEM},
                {
                    "role": "user",
                    "content": UPGRADE_CHECK_USER.format(
                        user_message=user_message[:500],
                        skill_name=skill_name,
                        history_count=history_count,
                    ),
                },
            ]

            raw, _ = await llm_gateway.chat(
                model_config=lite_config,
                messages=messages,
                temperature=0.0,
                max_tokens=10,
            )
            scenario = raw.strip().lower()
            if scenario in ("intel", "skill_chain", "task_decomp"):
                return scenario
            return None
        except Exception as e:
            logger.warning(f"should_upgrade 判断失败，降级到普通路径: {e}")
            return None


pev_orchestrator = PEVOrchestrator()
