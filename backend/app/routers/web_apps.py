"""Web apps CRUD + preview + public share."""
import os
import secrets
import subprocess
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.web_app import WebApp
from app.models.user import User, Role
from app.models.permission import ApprovalRequest, ApprovalRequestType, ApprovalStatus


def _require_app_access(app: WebApp, user: User):
    """owner 或管理员可访问；已发布 app 所有登录用户可读（按 publish_scope 过滤由 market 端点处理）。"""
    is_admin = user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN)
    if app.created_by == user.id or is_admin:
        return
    raise HTTPException(status_code=403, detail="无权访问此 Web App")

router = APIRouter(tags=["web-apps"])

# app_id -> subprocess.Popen（每个 app 独立进程）
_backend_procs: dict[int, subprocess.Popen] = {}

# 端口分配：app_id -> port (9200 + app_id)
# 与旧的 user_id 方案区分，base 从 9200 开始
BASE_PORT = 9200

def _user_port(user_id: int) -> int:
    """兼容旧调用，实际已改用 _app_port。"""
    return BASE_PORT + user_id

def _app_port(app_id: int) -> int:
    return BASE_PORT + app_id


def _ensure_node_deps(cwd: str):
    """如果 package.json 存在但 node_modules 缺失或原生模块需要 rebuild，自动处理。"""
    pkg_json = os.path.join(cwd, "package.json")
    if not os.path.exists(pkg_json):
        return
    node_modules = os.path.join(cwd, "node_modules")
    if not os.path.isdir(node_modules):
        subprocess.run("npm install", shell=True, cwd=cwd,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        # 检查是否有原生模块（.node 文件），尝试 rebuild
        has_native = any(
            fname.endswith(".node")
            for _, _, files in os.walk(node_modules)
            for fname in files
        )
        if has_native:
            subprocess.run("npm rebuild", shell=True, cwd=cwd,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class WebAppCreate(BaseModel):
    name: str
    description: Optional[str] = None
    html_content: str
    is_public: bool = False


class WebAppUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    html_content: Optional[str] = None
    is_public: Optional[bool] = None


def _app_dict(app: WebApp, include_html: bool = False) -> dict:
    d = {
        "id": app.id,
        "name": app.name,
        "description": app.description,
        "created_by": app.created_by,
        "is_public": app.is_public,
        "share_token": app.share_token,
        "preview_url": f"/api/web-apps/{app.id}/preview",
        "share_url": f"/share/{app.share_token}" if app.share_token else None,
        "created_at": app.created_at.isoformat() if app.created_at else None,
        "status": app.status or "draft",
        "backend_port": app.backend_port,
        "backend_cmd": app.backend_cmd,
        "has_backend": bool(app.backend_cmd),
        "publish_scope": app.publish_scope or "personal",
        "publish_department_ids": app.publish_department_ids or [],
        "publish_user_ids": app.publish_user_ids or [],
    }
    if include_html:
        d["html_content"] = app.html_content
    return d


class WebAppStatusUpdate(BaseModel):
    status: str  # "reviewing" only (user can submit for approval)
    publish_scope: str = "company"  # company / dept / personal
    publish_department_ids: list[int] = []  # 指定部门
    publish_user_ids: list[int] = []  # 指定个人


@router.get("/api/web-apps")
def list_web_apps(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    apps = (
        db.query(WebApp)
        .filter(WebApp.created_by == user.id)
        .order_by(WebApp.created_at.desc())
        .all()
    )
    return [_app_dict(a) for a in apps]


@router.get("/api/web-apps/market")
def list_web_apps_market(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """应用市场：仅返回用户有权访问的已发布 WebApp（按 publish_scope 过滤）。"""
    from app.models.user import User as UserModel
    rows = (
        db.query(WebApp, UserModel.display_name)
        .join(UserModel, WebApp.created_by == UserModel.id, isouter=True)
        .filter(WebApp.status == "published")
        .order_by(WebApp.created_at.desc())
        .all()
    )
    seen: set[tuple] = set()
    result = []
    for app, display_name in rows:
        key = (app.created_by, app.name)
        if key in seen:
            continue
        seen.add(key)
        # 按 publish_scope 过滤可见性
        scope = app.publish_scope or "company"
        if scope == "company":
            pass  # 全部可见
        elif scope == "dept":
            dept_ids = app.publish_department_ids or []
            if user.department_id not in dept_ids and app.created_by != user.id:
                continue
        elif scope == "personal":
            user_ids = app.publish_user_ids or []
            if user.id not in user_ids and app.created_by != user.id:
                continue
        d = _app_dict(app)
        d["creator_name"] = display_name or "未知"
        d["is_mine"] = app.created_by == user.id
        result.append(d)
    return result


@router.post("/api/web-apps")
def create_web_app(
    body: WebAppCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    share_token = secrets.token_urlsafe(16) if body.is_public else secrets.token_urlsafe(16)
    app = WebApp(
        name=body.name,
        description=body.description,
        html_content=body.html_content,
        created_by=user.id,
        is_public=body.is_public,
        share_token=share_token,
    )
    db.add(app)
    db.commit()
    db.refresh(app)
    return _app_dict(app, include_html=True)


@router.get("/api/web-apps/{app_id}")
def get_web_app(
    app_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    app = db.get(WebApp, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="Web app not found")
    _require_app_access(app, user)
    return _app_dict(app, include_html=True)


@router.put("/api/web-apps/{app_id}")
def update_web_app(
    app_id: int,
    body: WebAppUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    app = db.get(WebApp, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="Web app not found")
    if app.created_by != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(app, field, value)
    db.commit()
    db.refresh(app)
    return _app_dict(app, include_html=True)


@router.patch("/api/web-apps/{app_id}/status")
def update_web_app_status(
    app_id: int,
    body: WebAppStatusUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """用户提交发布申请（status=reviewing），创建审批流。"""
    app = db.get(WebApp, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="Web app not found")
    if app.created_by != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    if body.status != "reviewing":
        raise HTTPException(status_code=400, detail="仅支持提交审批（reviewing）")
    if app.status == "reviewing":
        raise HTTPException(status_code=400, detail="已在审批中")
    if app.status == "published":
        raise HTTPException(status_code=400, detail="已发布")

    app.status = "reviewing"
    app.publish_scope = body.publish_scope
    app.publish_department_ids = body.publish_department_ids
    app.publish_user_ids = body.publish_user_ids

    # Fix 6: 创建审批申请 + 自动采集证据
    try:
        from app.services.approval_templates import get_auto_evidence
        auto_ep = get_auto_evidence("webapp_publish", "webapp", app.id, db)
    except Exception:
        auto_ep = None
    req = ApprovalRequest(
        request_type=ApprovalRequestType.WEBAPP_PUBLISH,
        target_id=app.id,
        target_type="webapp",
        requester_id=user.id,
        status=ApprovalStatus.PENDING,
        stage="dept_pending",
        conditions=[{"publish_scope": body.publish_scope,
                     "publish_department_ids": body.publish_department_ids,
                     "publish_user_ids": body.publish_user_ids}],
        evidence_pack=auto_ep if auto_ep else None,
    )
    db.add(req)
    db.commit()
    db.refresh(app)
    return _app_dict(app)


@router.delete("/api/web-apps/{app_id}")
def delete_web_app(
    app_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    app = db.get(WebApp, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="Web app not found")
    if app.created_by != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    db.delete(app)
    db.commit()
    return {"ok": True}


@router.post("/api/web-apps/{app_id}/start-backend")
def start_backend(
    app_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """启动 web app 对应的后端进程（如果还没在跑）。"""
    app = db.get(WebApp, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="Web app not found")
    _require_app_access(app, user)
    if not app.backend_cmd or not app.backend_cwd:
        return {"ok": True, "message": "无后端服务"}

    port = app.backend_port or _app_port(app.id)

    # 检查进程是否还活着
    proc = _backend_procs.get(app.id)
    if proc and proc.poll() is None:
        return {"ok": True, "port": port, "message": "后端已在运行"}

    # 启动新进程
    _ensure_node_deps(app.backend_cwd)
    env = {**os.environ, "PORT": str(port)}
    try:
        proc = subprocess.Popen(
            app.backend_cmd,
            shell=True,
            cwd=app.backend_cwd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _backend_procs[app.id] = proc
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"启动失败：{e}")

    return {"ok": True, "port": port, "pid": proc.pid, "message": "后端已启动"}


@router.get("/api/web-apps/{app_id}/preview", response_class=HTMLResponse)
def preview_web_app(
    app_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    app = db.get(WebApp, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="Web app not found")
    _require_app_access(app, user)

    # 有后端配置则自动启动
    if app.backend_cmd and app.backend_cwd:
        port = app.backend_port or _app_port(app.id)
        proc = _backend_procs.get(app.id)
        if not proc or proc.poll() is not None:
            _ensure_node_deps(app.backend_cwd)
            env = {**os.environ, "PORT": str(port)}
            try:
                proc = subprocess.Popen(
                    app.backend_cmd,
                    shell=True,
                    cwd=app.backend_cwd,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                _backend_procs[app.id] = proc
            except Exception:
                pass

    return HTMLResponse(content=app.html_content or "<html><body>空内容</body></html>")


@router.get("/share/{share_token}", response_class=HTMLResponse)
def public_share(share_token: str, db: Session = Depends(get_db)):
    """Public access via share token — no login required."""
    app = db.query(WebApp).filter(WebApp.share_token == share_token).first()
    if not app:
        raise HTTPException(status_code=404, detail="Not found")
    return HTMLResponse(content=app.html_content or "<html><body>空内容</body></html>")
