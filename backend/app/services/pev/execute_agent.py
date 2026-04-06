"""ExecuteAgent：按 step_type 分发执行各类步骤。"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.services.llm_gateway import llm_gateway

logger = logging.getLogger(__name__)


class ExecuteAgent:

    async def execute_step(
        self,
        step: dict,
        resolved_inputs: dict,
        job: Any,  # PEVJob ORM 对象
        db: Session,
    ) -> dict:
        """按 step_type 分发执行，返回结果 dict。

        结果格式：{"ok": bool, "data": Any, "error": str | None}
        """
        step_type = step.get("step_type", "")
        try:
            if step_type == "llm_generate":
                return await self._execute_llm_generate(step, resolved_inputs, job, db)
            elif step_type == "tool_call":
                return await self._execute_tool_call(step, resolved_inputs, job, db)
            elif step_type == "crawl":
                return await self._execute_crawl(step, resolved_inputs, job, db)
            elif step_type == "sub_task":
                return await self._execute_sub_task(step, resolved_inputs, job, db)
            elif step_type == "skill_execute":
                return await self._execute_skill(step, resolved_inputs, job, db)
            else:
                return {"ok": False, "data": None, "error": f"未知步骤类型: {step_type}"}
        except Exception as e:
            logger.error(f"ExecuteAgent.execute_step [{step.get('step_key')}] 失败: {e}")
            return {"ok": False, "data": None, "error": str(e)}

    # ── llm_generate ──────────────────────────────────────────────────────────

    async def _execute_llm_generate(
        self,
        step: dict,
        resolved_inputs: dict,
        job: Any,
        db: Session,
    ) -> dict:
        """调用全量模型生成文本内容。"""
        prompt = resolved_inputs.get("prompt") or step.get("description") or ""
        system = resolved_inputs.get("system_prompt", "")
        suggestion = resolved_inputs.get("_retry_suggestion", "")

        if suggestion:
            prompt = f"{prompt}\n\n[修正要求] {suggestion}"

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        model_config = llm_gateway.resolve_config(db, "pev.execute")

        content, usage = await llm_gateway.chat(
            model_config=model_config,
            messages=messages,
            temperature=float(resolved_inputs.get("temperature", 0.7)),
        )

        return {
            "ok": True,
            "data": {"content": content, "usage": usage},
            "error": None,
        }

    # ── tool_call ─────────────────────────────────────────────────────────────

    async def _execute_tool_call(
        self,
        step: dict,
        resolved_inputs: dict,
        job: Any,
        db: Session,
    ) -> dict:
        """调用已注册工具。"""
        from app.services.tool_executor import tool_executor

        tool_name = resolved_inputs.get("tool_name") or step.get("step_key")
        params = resolved_inputs.get("params") or {}
        user_id = job.user_id if job else None

        result = await tool_executor.execute_tool(
            db=db,
            tool_name=tool_name,
            params=params,
            user_id=user_id,
        )
        return {
            "ok": result.get("ok", False),
            "data": result.get("result"),
            "error": result.get("error"),
        }

    # ── crawl ────────────────────────────────────────────────────────────────

    async def _execute_crawl(
        self,
        step: dict,
        resolved_inputs: dict,
        job: Any,
        db: Session,
    ) -> dict:
        """执行网页爬取/情报采集。"""
        from app.models.intel import IntelSource, IntelTask, IntelTaskStatus
        from app.services.intel_collector import intel_collector
        import datetime

        source_id = resolved_inputs.get("source_id")
        if source_id:
            # 使用已有情报源
            source = db.get(IntelSource, source_id)
            if not source:
                return {"ok": False, "data": None, "error": f"情报源 {source_id} 不存在"}

            intel_task = IntelTask(
                source_id=source.id,
                status=IntelTaskStatus.QUEUED,
            )
            db.add(intel_task)
            db.commit()
            db.refresh(intel_task)

            count = await intel_collector.run_source(db, source, task=intel_task)
            return {
                "ok": True,
                "data": {"new_entries": count, "intel_task_id": intel_task.id},
                "error": None,
            }
        else:
            # 临时 URL 爬取（创建临时 source）
            url = resolved_inputs.get("url", "")
            if not url:
                return {"ok": False, "data": None, "error": "crawl 步骤缺少 url 或 source_id"}

            from app.models.intel import IntelSourceType
            temp_source = IntelSource(
                name=f"PEV临时爬取_{step.get('step_key', '')}",
                source_type=IntelSourceType.CRAWLER,
                config={"url": url},
                is_active=True,
            )
            db.add(temp_source)
            db.commit()
            db.refresh(temp_source)

            count = await intel_collector.collect_crawler(db, temp_source)

            # M25: 清理临时 IntelSource
            try:
                db.delete(temp_source)
                db.commit()
            except Exception:
                db.rollback()
                logger.warning(f"M25: 清理临时 IntelSource {temp_source.id} 失败")

            return {
                "ok": True,
                "data": {"new_entries": count, "url": url},
                "error": None,
            }

    # ── sub_task ──────────────────────────────────────────────────────────────

    async def _execute_sub_task(
        self,
        step: dict,
        resolved_inputs: dict,
        job: Any,
        db: Session,
    ) -> dict:
        """创建子任务记录，关联到父 PEVJob。"""
        from app.models.task import Task, TaskStatus, TaskPriority

        title = resolved_inputs.get("title") or step.get("description", "子任务")[:200]
        description = resolved_inputs.get("description", "")
        assignee_id = resolved_inputs.get("assignee_id") or job.user_id

        task = Task(
            title=title[:200],
            description=description,
            priority=TaskPriority.NEITHER,
            status=TaskStatus.PENDING,
            assignee_id=assignee_id,
            created_by_id=job.user_id,
            source_type="pev_job",
            source_id=job.id,
            pev_job_id=job.id,
        )
        db.add(task)
        db.commit()
        db.refresh(task)

        return {
            "ok": True,
            "data": {"task_id": task.id, "title": task.title},
            "error": None,
        }

    # ── skill_execute ─────────────────────────────────────────────────────────

    async def _execute_skill(
        self,
        step: dict,
        resolved_inputs: dict,
        job: Any,
        db: Session,
    ) -> dict:
        """执行指定 Skill，返回生成结果。"""
        from app.models.conversation import Conversation
        from app.services.skill_engine import skill_engine

        skill_name = resolved_inputs.get("skill_name", "")
        user_message = resolved_inputs.get("user_message") or step.get("description", "")
        suggestion = resolved_inputs.get("_retry_suggestion", "")

        if suggestion:
            user_message = f"{user_message}\n\n[修正要求] {suggestion}"

        # H9: 使用独立 Session，避免 _execute_skill 的 rollback 影响 orchestrator 状态
        from app.database import SessionLocal
        skill_db = SessionLocal()
        try:
            temp_conv = Conversation(
                user_id=job.user_id,
                skill_id=None,
            )
            if skill_name:
                from app.models.skill import Skill, SkillStatus
                skill_obj = (
                    skill_db.query(Skill)
                    .filter(Skill.name == skill_name, Skill.status == SkillStatus.PUBLISHED)
                    .first()
                )
                if skill_obj:
                    temp_conv.skill_id = skill_obj.id

            skill_db.add(temp_conv)
            skill_db.flush()

            try:
                response, meta = await skill_engine.execute(
                    skill_db, temp_conv, user_message,
                    user_id=job.user_id,
                )
                return {
                    "ok": True,
                    "data": {"content": response, "meta": meta},
                    "error": None,
                }
            finally:
                try:
                    skill_db.delete(temp_conv)
                    skill_db.commit()
                except Exception:
                    skill_db.rollback()
        finally:
            skill_db.close()


execute_agent = ExecuteAgent()
