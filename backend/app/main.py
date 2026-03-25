import asyncio
import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Universal KB API", version="0.1.0")

_default_origins = ["http://localhost:3000", "http://localhost:5173", "http://localhost:5023"]
_extra = os.getenv("FRONTEND_ORIGIN", "")
_allowed_origins = _default_origins + [o.strip() for o in _extra.split(",") if o.strip()]

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


@app.on_event("startup")
async def startup_event():
    """Start background schedulers on app startup."""
    try:
        from app.services.intel_scheduler import start_intel_scheduler
        start_intel_scheduler()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Intel scheduler startup failed: {e}")

    # 后台预热 ASR 模型，避免首次语音输入等待
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

        upstream_scheduler = BackgroundScheduler()
        upstream_scheduler.add_job(check_all_imported_skills, "cron", hour=3, minute=0)
        # 每 12 小时统计一次 OpenCode 用量（0点 和 12点）
        upstream_scheduler.add_job(_run_opencode_usage_job, "cron", hour="0,12", minute=5)
        upstream_scheduler.add_job(_run_daily_project_summary, "cron", hour=23, minute=0)
        upstream_scheduler.add_job(_run_todo_reminder, "cron", hour=9, minute=0)
        upstream_scheduler.start()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Upstream checker scheduler failed: {e}")


# Register new models with Base.metadata
from app.models import raw_input, draft, opportunity, feedback_item  # noqa: F401
from app.models import permission  # noqa: F401
from app.models import opencode  # noqa: F401

from app.routers import auth, admin, skills, knowledge, conversations  # noqa: E402
from app.routers import business_tables, data_tables, audit, skill_suggestions, contributions  # noqa: E402
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
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(skills.router)
app.include_router(knowledge.router)
app.include_router(conversations.router)
app.include_router(business_tables.router)
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

# 头像静态文件服务
_avatar_dir = Path("./uploads/avatars")
_avatar_dir.mkdir(parents=True, exist_ok=True)
app.mount("/api/avatars", StaticFiles(directory=str(_avatar_dir)), name="avatars")
