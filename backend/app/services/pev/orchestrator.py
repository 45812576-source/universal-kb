"""PEVOrchestrator：协调 Plan-Execute-Verify 三阶段循环。"""
from __future__ import annotations

import datetime
import logging
import time as _time
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
_PEV_GLOBAL_TIMEOUT = 600  # H8: PEV Job 全局超时 (秒)


class PEVOrchestrator:

    # ── 主编排循环 ────────────────────────────────────────────────────────────

    async def run(
        self,
        db: Session,
        job: PEVJob,
    ) -> AsyncIterator[dict]:
        """执行完整 PEV 循环，yield SSE 事件 dict。"""
        _deadline = _time.monotonic() + _PEV_GLOBAL_TIMEOUT
        max_retries = (job.config or {}).get("max_retries", _DEFAULT_MAX_RETRIES)
        skip_verify = (job.config or {}).get("skip_verify", False)
        replan_budget = (job.config or {}).get("replan_budget", _DEFAULT_REPLAN_BUDGET)

        yield {"event": "pev_start", "data": {"job_id": job.id, "scenario": job.scenario, "goal": job.goal}}

        # ─── Phase 1: Planning ────────────────────────────────────────────────
        job.status = PEVJobStatus.PLANNING
        job.started_at = datetime.datetime.now(datetime.UTC)
        db.commit()

        try:
            plan = await plan_agent.generate_plan(
                goal=job.goal,
                scenario=job.scenario,
                context=job.context or {},
                db=db,
            )
        except Exception as e:
            async for ev in self._fail(db, job, f"计划生成失败: {e}"):
                yield ev
            return

        # 持久化 steps
        raw_steps = plan.get("steps") or []
        try:
            sorted_steps = topological_sort(raw_steps)
        except ValueError as e:
            async for ev in self._fail(db, job, str(e)):
                yield ev
            return

        for idx, s in enumerate(sorted_steps):
            # Gap 3: 自动填充 compensation_spec（从工具 manifest 中查找）
            comp_spec = None
            if s.get("step_type") == "tool_call":
                tool_name = (s.get("input_spec") or {}).get("tool_name")
                if tool_name:
                    from app.models.tool import ToolRegistry as _TR
                    _tool = db.query(_TR).filter(_TR.name == tool_name).first()
                    if _tool and _tool.config:
                        manifest = (_tool.config or {}).get("manifest", {})
                        comp_spec = manifest.get("compensation")

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
                compensation_spec=comp_spec,
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

        context: dict = dict(job.context or {})
        _replan_triggered = True  # 首次进入也需要查询 steps

        while _replan_triggered:
            _replan_triggered = False
            # (重新)读取待执行 steps
            steps = (
                db.query(PEVStep)
                .filter(PEVStep.job_id == job.id, PEVStep.status.in_([PEVStepStatus.PENDING, PEVStepStatus.RUNNING]))
                .order_by(PEVStep.order_index)
                .all()
            )

            for step in steps:
                # H8: PEV 全局超时检查
                if _time.monotonic() > _deadline:
                    logger.warning("PEV Job %s 全局超时 (%ds)", job.id, _PEV_GLOBAL_TIMEOUT)
                    async for ev in self._fail(db, job, f"任务执行超时（{_PEV_GLOBAL_TIMEOUT}s）"):
                        yield ev
                    return

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

                    # ─── Verifying（每步）──────────────────────────────────
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
                            # C6 fix: 标记当前步骤 skipped，删除后续未执行的 steps
                            step.status = PEVStepStatus.SKIPPED
                            db.query(PEVStep).filter(
                                PEVStep.job_id == job.id,
                                PEVStep.order_index > step.order_index,
                                PEVStep.status == PEVStepStatus.PENDING,
                            ).delete(synchronize_session="fetch")

                            # 根据 new_plan 创建新 PEVStep ORM 记录
                            new_raw_steps = new_plan.get("steps") or []
                            try:
                                new_sorted = topological_sort(new_raw_steps)
                            except ValueError:
                                new_sorted = new_raw_steps

                            completed_keys = {s.step_key for s in steps if s.status == PEVStepStatus.PASSED}
                            from sqlalchemy import func as _sa_func
                            _max_idx = db.query(_sa_func.max(PEVStep.order_index)).filter(
                                PEVStep.job_id == job.id
                            ).scalar() or 0
                            base_index = _max_idx + 1
                            for idx, s in enumerate(new_sorted):
                                sk = s.get("step_key", f"replan_step_{idx}")
                                if sk in completed_keys:
                                    continue
                                new_step = PEVStep(
                                    job_id=job.id,
                                    order_index=base_index + idx,
                                    step_key=sk,
                                    step_type=s.get("step_type", "llm_generate"),
                                    description=s.get("description", ""),
                                    depends_on=s.get("depends_on") or [],
                                    input_spec=s.get("input_spec") or {},
                                    output_spec=s.get("output_spec") or {},
                                    verify_criteria=s.get("verify_criteria", ""),
                                )
                                db.add(new_step)

                            job.plan = new_plan
                            job.total_steps = (job.completed_steps or 0) + len(new_sorted)
                            db.commit()

                            # 设置标志，跳出 for 循环后 while 会重新查询 pending steps
                            _replan_triggered = True
                            break  # 跳出 for step in steps，while 循环会重新遍历
                        except Exception as e:
                            logger.error(f"Replan 失败: {e}")

                    if not _replan_triggered:
                        # replan 耗尽或失败 → Job FAILED
                        async for ev in self._fail(db, job, f"步骤 '{step.step_key}' 执行失败且无法恢复"):
                            yield ev
                        return

                else:
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
        job.finished_at = datetime.datetime.now(datetime.UTC)
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

    async def _fail(self, db: Session, job: PEVJob, message: str) -> AsyncIterator[dict]:
        """将 Job 标记为 FAILED，实际执行补偿操作，yield 相关事件。"""
        from app.services.tool_executor import tool_executor

        # Gap 3: 补偿已完成的步骤（逆序）
        passed_steps = (
            db.query(PEVStep)
            .filter(PEVStep.job_id == job.id, PEVStep.status == PEVStepStatus.PASSED)
            .order_by(PEVStep.order_index.desc())
            .all()
        )
        for step in passed_steps:
            if not step.compensation_spec:
                continue
            spec = step.compensation_spec
            undo_tool = spec.get("undo_tool")
            if not undo_tool:
                continue

            yield {"event": "pev_compensation_start", "data": {
                "job_id": job.id, "step_key": step.step_key, "undo_tool": undo_tool,
            }}

            # 解析 undo_params_template 中的 $input.X / $result.X 占位符
            undo_params = {}
            template = spec.get("undo_params_template", {})
            for k, v in template.items():
                if isinstance(v, str) and v.startswith("$input."):
                    field = v[7:]
                    undo_params[k] = (step.input_spec or {}).get(field)
                elif isinstance(v, str) and v.startswith("$result."):
                    field = v[8:]
                    undo_params[k] = (step.result or {}).get(field)
                else:
                    undo_params[k] = v

            try:
                comp_result = await tool_executor.execute_tool(
                    db=db,
                    tool_name=undo_tool,
                    params=undo_params,
                    user_id=job.user_id,
                )
            except Exception as e:
                comp_result = {"ok": False, "error": str(e)}

            step.status = PEVStepStatus.COMPENSATED
            yield {"event": "pev_compensation_result", "data": {
                "job_id": job.id, "step_key": step.step_key,
                "undo_tool": undo_tool, "result": comp_result,
            }}

        job.status = PEVJobStatus.FAILED
        job.finished_at = datetime.datetime.now(datetime.UTC)
        db.commit()
        yield {"event": "pev_error", "data": {"job_id": job.id, "message": message}}

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
            orch_config = llm_gateway.resolve_config(db, "pev.orchestrate")
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
                model_config=orch_config,
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
