import asyncio
import logging
import os
import uuid
from pathlib import Path
from contextvars import ContextVar

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from app.api_envelope import ApiEnvelopeException, api_envelope_exception_handler

# ── L1: 请求追踪 ID（线程/协程安全） ─────────────────────────────────────────
request_id_var: ContextVar[str] = ContextVar("request_id", default="")


class _RequestIdFilter(logging.Filter):
    """L1: 将 request_id 注入每条日志记录，便于结构化追踪。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get("")  # type: ignore[attr-defined]
        return True


# 配置根 logger：注入 request_id
_handler = logging.StreamHandler()
_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] [rid=%(request_id)s] %(message)s"
    )
)
_handler.addFilter(_RequestIdFilter())
logging.root.addHandler(_handler)
logging.root.setLevel(logging.INFO)

app = FastAPI(title="Universal KB API", version="0.1.0")
app.add_exception_handler(ApiEnvelopeException, api_envelope_exception_handler)
app.add_exception_handler(HTTPException, api_envelope_exception_handler)
app.add_exception_handler(StarletteHTTPException, api_envelope_exception_handler)

_default_origins = ["http://localhost:3000", "http://localhost:5173", "http://localhost:5023"]
_extra = os.getenv("FRONTEND_ORIGIN", "")
_allowed_origins = _default_origins + [o.strip() for o in _extra.split(",") if o.strip()]


class RequestIdMiddleware(BaseHTTPMiddleware):
    """为每个请求生成/传播 X-Request-ID，存入 ContextVar 供下游日志使用。"""

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        request_id_var.set(rid)
        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response


app.add_middleware(RequestIdMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.on_event("shutdown")
async def shutdown_event():
    """关闭时终止所有 opencode 子进程，防止游离进程。"""
    try:
        from app.services.runtime_process_manager import shutdown_all_instances
        await shutdown_all_instances()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Shutdown cleanup failed: {e}")


@app.on_event("startup")
async def startup_event():
    """Start background schedulers on app startup."""
    try:
        from app.services.runtime_process_manager import _kill_orphan_opencode_procs
        _kill_orphan_opencode_procs()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"OpenCode orphan cleanup failed: {e}")

    try:
        from app.services.intel_scheduler import start_intel_scheduler
        start_intel_scheduler()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Intel scheduler startup failed: {e}")

    # 后台预热 ASR 模型，避免首次语音输入等待（DISABLE_ASR_PRELOAD=1 可跳过，用于无 GPU/无网络环境）
    if not os.getenv("DISABLE_ASR_PRELOAD"):
        try:
            from app.routers.asr import preload_engine
            asyncio.create_task(preload_engine())
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"ASR preload failed: {e}")

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from app.services.upstream_checker import check_all_imported_skills
        from app.routers.contributions import compute_and_store_opencode_usage
        from app.database import SessionLocal

        def _run_opencode_usage_job():
            db = SessionLocal()
            try:
                compute_and_store_opencode_usage(db)
            except Exception as ex:
                import logging
                logging.getLogger(__name__).warning(f"OpenCode usage job failed: {ex}")
            finally:
                db.close()

        def _run_daily_project_summary():
            import asyncio
            from app.models.project import Project, ProjectStatus
            from app.services.project_engine import project_engine
            db = SessionLocal()
            try:
                projects = db.query(Project).filter(Project.status == ProjectStatus.ACTIVE).all()
                for p in projects:
                    try:
                        asyncio.run(project_engine.daily_project_summary(p, db))
                    except Exception as ex:
                        import logging
                        logging.getLogger(__name__).warning(f"daily_summary failed project {p.id}: {ex}")
            finally:
                db.close()

        def _run_todo_reminder():
            import asyncio
            from app.models.project import Project, ProjectStatus
            from app.services.project_engine import project_engine
            db = SessionLocal()
            try:
                projects = db.query(Project).filter(Project.status == ProjectStatus.ACTIVE).all()
                for p in projects:
                    try:
                        asyncio.run(project_engine.inject_todo_reminder(p, db))
                    except Exception as ex:
                        import logging
                        logging.getLogger(__name__).warning(f"todo_reminder failed project {p.id}: {ex}")
            finally:
                db.close()

        def _run_bitable_sync():
            """定时同步飞书多维表格：扫描所有配置了 sync_interval 的表。"""
            import asyncio as _asyncio
            db = SessionLocal()
            try:
                from app.models.business import BusinessTable as BT
                from app.services.bitable_sync import bitable_sync
                tables = db.query(BT).all()
                now_ts = int(time.time()) if "time" in dir() else __import__("time").time()
                for bt in tables:
                    rules = bt.validation_rules or {}
                    interval = rules.get("sync_interval", 0)
                    if not interval or not rules.get("bitable_app_token"):
                        continue
                    last = rules.get("last_synced_at", 0)
                    if now_ts - last < interval * 60:
                        continue  # 未到同步时间
                    try:
                        _asyncio.run(bitable_sync.incremental_sync(db, bt))
                        logging.getLogger(__name__).info(
                            f"Bitable sync done: {bt.table_name}"
                        )
                    except Exception as ex:
                        logging.getLogger(__name__).warning(
                            f"Bitable sync failed for {bt.table_name}: {ex}"
                        )
            finally:
                db.close()

        def _run_lark_doc_sync():
            """定时同步飞书文档：扫描所有配置了 lark_sync_interval 的知识条目。"""
            import asyncio as _asyncio
            import time as _time
            db = SessionLocal()
            try:
                from app.models.knowledge import KnowledgeEntry
                from app.services.lark_doc_importer import lark_doc_importer
                entries = db.query(KnowledgeEntry).filter(
                    KnowledgeEntry.lark_sync_interval > 0,
                    KnowledgeEntry.lark_doc_token.isnot(None),
                ).all()
                now_ts = int(_time.time())
                for entry in entries:
                    if now_ts - (entry.lark_last_synced_at or 0) < entry.lark_sync_interval * 60:
                        continue
                    try:
                        result = _asyncio.run(lark_doc_importer.sync_doc(db, entry))
                        logging.getLogger(__name__).info(
                            f"Lark doc sync done: entry {entry.id}, changed={result.get('content_changed')}"
                        )
                    except Exception as ex:
                        logging.getLogger(__name__).warning(
                            f"Lark doc sync failed for entry {entry.id}: {ex}"
                        )
            finally:
                db.close()

        upstream_scheduler = BackgroundScheduler()
        upstream_scheduler.add_job(check_all_imported_skills, "cron", hour=3, minute=0)
        # 每 10 分钟扫描一次需要同步的飞书表
        upstream_scheduler.add_job(_run_bitable_sync, "interval", minutes=10)
        # 每 10 分钟扫描一次需要同步的飞书文档
        upstream_scheduler.add_job(_run_lark_doc_sync, "interval", minutes=10)
        # 每 12 小时统计一次 OpenCode 用量（0点 和 12点）
        upstream_scheduler.add_job(_run_opencode_usage_job, "cron", hour="0,12", minute=5)
        upstream_scheduler.add_job(_run_daily_project_summary, "cron", hour=23, minute=0)
        upstream_scheduler.add_job(_run_todo_reminder, "cron", hour=9, minute=0)

        def _run_workdir_kb_sync():
            """每 30 分钟扫描用户 workdir，把新产出文档沉淀到知识库开发工地文件夹。"""
            db = SessionLocal()
            try:
                from app.services.workdir_kb_sync import run_workdir_kb_sync
                run_workdir_kb_sync(db)
            except Exception as ex:
                import logging
                logging.getLogger(__name__).warning(f"WorkdirKbSync job failed: {ex}")
            finally:
                db.close()

        upstream_scheduler.add_job(_run_workdir_kb_sync, "interval", minutes=30)

        # 知识处理 Job Worker：每 30 秒扫一次 queued 的 render/classify 任务
        from app.services.knowledge_worker import (
            process_knowledge_jobs,
            recover_stuck_jobs,
            backfill_unclassified,
            backfill_ungoverned,
            backfill_ungoverned_tables,
            backfill_failed_renders,
            backfill_missing_ai_notes,
            backfill_ununderstood,
        )
        from app.services.skill_governance_jobs import process_queued_governance_jobs
        # 每 2 分钟回收超时 stuck job（必须在 process 之前执行）
        upstream_scheduler.add_job(recover_stuck_jobs, "interval", minutes=2, id="knowledge_recover_stuck")
        upstream_scheduler.add_job(process_knowledge_jobs, "interval", seconds=30, id="knowledge_job_worker")
        upstream_scheduler.add_job(process_queued_governance_jobs, "interval", seconds=5, id="skill_governance_job_worker")
        # 每 10 分钟补偿未分类条目
        upstream_scheduler.add_job(backfill_unclassified, "interval", minutes=10, id="knowledge_backfill_classify")
        # 每 10 分钟补偿渲染失败条目
        upstream_scheduler.add_job(backfill_failed_renders, "interval", minutes=10, id="knowledge_backfill_render")
        # 每 10 分钟补偿缺失 AI 笔记的条目
        upstream_scheduler.add_job(backfill_missing_ai_notes, "interval", minutes=10, id="knowledge_backfill_ai_notes")
        # 每 10 分钟补偿未理解条目
        upstream_scheduler.add_job(backfill_ununderstood, "interval", minutes=10, id="knowledge_backfill_understand")
        # 每 10 分钟补齐未治理条目
        upstream_scheduler.add_job(backfill_ungoverned, "interval", minutes=10, id="knowledge_backfill_governance")
        # 每 10 分钟补齐未治理数据表
        upstream_scheduler.add_job(backfill_ungoverned_tables, "interval", minutes=10, id="knowledge_backfill_governance_tables")

        # 基线自动快照（每日一次）：当日 ≥10 条 auto-apply 时创建
        def _run_auto_snapshot():
            db = SessionLocal()
            try:
                from app.services.governance_engine import auto_snapshot_on_round
                auto_snapshot_on_round(db)
            except Exception as ex:
                import logging
                logging.getLogger(__name__).warning(f"Auto snapshot failed: {ex}")
            finally:
                db.close()

        # 基线偏离检测（每日一次）
        def _run_deviation_check():
            db = SessionLocal()
            try:
                from app.services.governance_engine import detect_baseline_deviation
                detect_baseline_deviation(db)
            except Exception as ex:
                import logging
                logging.getLogger(__name__).warning(f"Baseline deviation check failed: {ex}")
            finally:
                db.close()

        # 缺口检测（每日一次）
        def _run_gap_detection():
            db = SessionLocal()
            try:
                from app.services.governance_gap_detector import run_gap_detection
                run_gap_detection(db)
            except Exception as ex:
                import logging
                logging.getLogger(__name__).warning(f"Gap detection failed: {ex}")
            finally:
                db.close()

        upstream_scheduler.add_job(_run_gap_detection, "cron", hour=4, minute=30, id="governance_gap_detection")
        upstream_scheduler.add_job(_run_auto_snapshot, "cron", hour=2, minute=30, id="governance_auto_snapshot")
        upstream_scheduler.add_job(_run_deviation_check, "cron", hour=3, minute=30, id="governance_deviation_check")

        upstream_scheduler.start()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Upstream checker scheduler failed: {e}")


# Register new models with Base.metadata
from app.models import raw_input, draft, opportunity, feedback_item  # noqa: F401
from app.models import permission  # noqa: F401
from app.models import opencode  # noqa: F401
from app.models import sandbox as sandbox_models  # noqa: F401
from app.models import knowledge_job  # noqa: F401
from app.models import skill_knowledge_ref  # noqa: F401

from app.routers import auth, admin, skills, knowledge, conversations  # noqa: E402
from app.routers import business_tables, data_tables, audit, skill_suggestions, contributions  # noqa: E402
from app.routers import table_views  # noqa: E402
from app.routers import tools, files, intel, lark  # noqa: E402
from app.routers import web_apps, workspaces  # noqa: E402
from app.routers import skill_market  # noqa: E402
from app.routers import mcp_server, mcp_tokens  # noqa: E402
from app.routers import drafts  # noqa: E402
from app.routers import tasks  # noqa: E402
from app.routers import projects  # noqa: E402
from app.routers import asr  # noqa: E402
from app.routers import permissions  # noqa: E402
from app.routers import skill_policies, approvals, handoff, output_schemas  # noqa: E402
from app.routers import dev_studio  # noqa: E402
from app.routers import sandbox  # noqa: E402
from app.routers import sandbox_interactive  # noqa: E402
from app.routers import onlyoffice  # noqa: E402
from app.routers import user_workspace_config  # noqa: E402
from app.routers import skill_memos  # noqa: E402
from app.routers import data_assets  # noqa: E402
from app.routers import collab  # noqa: E402
from app.routers import knowledge_governance  # noqa: E402
from app.routers import knowledge_admin  # noqa: E402
from app.routers import knowledge_tags  # noqa: E402
from app.routers import knowledge_health  # noqa: E402
from app.routers import knowledge_mask_feedback  # noqa: E402
from app.routers import knowledge_permissions  # noqa: E402
from app.routers import permission_changes  # noqa: E402
from app.routers import user_capabilities  # noqa: E402
from app.routers import events  # noqa: E402
from app.routers import org_management  # noqa: E402
from app.routers import org_memory  # noqa: E402
from app.routers import skill_governance  # noqa: E402
from app.routers import sandbox_case_plans  # noqa: E402
from app.routers import test_flow  # noqa: E402
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(skills.router)
app.include_router(knowledge.router)
app.include_router(conversations.router)
app.include_router(business_tables.router)
app.include_router(table_views.router)
app.include_router(data_tables.router)
app.include_router(audit.router)
app.include_router(skill_suggestions.router)
app.include_router(contributions.router)
app.include_router(tools.router)
app.include_router(files.router)
app.include_router(intel.router)
app.include_router(lark.router)
app.include_router(web_apps.router)
app.include_router(workspaces.router)
app.include_router(skill_market.router)
app.include_router(mcp_server.router)
app.include_router(mcp_tokens.router)
app.include_router(drafts.router)
app.include_router(tasks.router)
app.include_router(projects.router)
app.include_router(asr.router)
app.include_router(permissions.router)
app.include_router(skill_policies.router)
app.include_router(approvals.router)
app.include_router(handoff.router)
app.include_router(output_schemas.router)
app.include_router(dev_studio.router)
app.include_router(sandbox.router)
app.include_router(sandbox_interactive.router)
app.include_router(onlyoffice.router)
app.include_router(user_workspace_config.router)
app.include_router(skill_memos.router)
app.include_router(data_assets.router)
app.include_router(collab.router)
app.include_router(knowledge_governance.router)
app.include_router(knowledge_admin.router)
app.include_router(knowledge_tags.router)
app.include_router(knowledge_health.router)
app.include_router(knowledge_mask_feedback.router)
app.include_router(knowledge_permissions.router)
app.include_router(permission_changes.router)
app.include_router(user_capabilities.router)
app.include_router(events.router)
app.include_router(org_management.router)
app.include_router(org_memory.router)
app.include_router(skill_governance.router)
app.include_router(sandbox_case_plans.router)
app.include_router(test_flow.router)

# 头像静态文件服务
_avatar_dir = Path("./uploads/avatars")
_avatar_dir.mkdir(parents=True, exist_ok=True)
app.mount("/api/avatars", StaticFiles(directory=str(_avatar_dir)), name="avatars")
