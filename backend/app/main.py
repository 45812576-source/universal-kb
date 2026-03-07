from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Universal KB API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
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


from app.routers import auth, admin, skills, knowledge, conversations  # noqa: E402
from app.routers import business_tables, data_tables, audit, skill_suggestions, contributions  # noqa: E402
from app.routers import tools, files, intel, lark  # noqa: E402
from app.routers import web_apps, workspaces  # noqa: E402
from app.routers import skill_market  # noqa: E402
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
