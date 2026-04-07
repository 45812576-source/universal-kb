"""Skill Market: browse external sources, import skills, manage MCP sources."""
import datetime
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.mcp import McpSource
from app.models.skill import Skill, SkillVersion, SkillStatus
from app.models.user import User, Role
from app.services.mcp_client import list_remote_skills, fetch_remote_skill, McpClientError

router = APIRouter(prefix="/api/skill-market", tags=["skill-market"])


class McpSourceCreate(BaseModel):
    name: str
    url: str
    adapter_type: str = "mcp"
    auth_token: Optional[str] = None


@router.get("/sources")
def list_sources(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    sources = db.query(McpSource).order_by(McpSource.created_at.desc()).all()
    return [
        {
            "id": s.id, "name": s.name, "url": s.url,
            "adapter_type": s.adapter_type, "is_active": s.is_active,
            "last_synced_at": s.last_synced_at.isoformat() if s.last_synced_at else None,
        }
        for s in sources
    ]


@router.post("/sources")
def create_source(
    req: McpSourceCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    source = McpSource(
        name=req.name, url=req.url,
        adapter_type=req.adapter_type, auth_token=req.auth_token,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return {"id": source.id}


@router.delete("/sources/{source_id}")
def delete_source(
    source_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
):
    source = db.get(McpSource, source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    db.delete(source)
    db.commit()
    return {"ok": True}


@router.get("/search")
def search_market(
    source_id: int,
    q: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    source = db.get(McpSource, source_id)
    if not source or not source.is_active:
        raise HTTPException(404, "Source not found or inactive")
    try:
        skills = list_remote_skills(source, q, page)
    except McpClientError as e:
        raise HTTPException(502, f"Remote source error: {e}")
    return skills


@router.get("/preview")
def preview_skill(
    source_id: int,
    upstream_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    source = db.get(McpSource, source_id)
    if not source or not source.is_active:
        raise HTTPException(404, "Source not found")
    try:
        skill_data = fetch_remote_skill(source, upstream_id)
    except McpClientError as e:
        raise HTTPException(502, f"Fetch error: {e}")
    return skill_data


class ImportRequest(BaseModel):
    source_id: int
    upstream_id: str


@router.post("/import")
def import_skill(
    req: ImportRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    source = db.get(McpSource, req.source_id)
    if not source or not source.is_active:
        raise HTTPException(404, "Source not found")

    try:
        skill_data = fetch_remote_skill(source, req.upstream_id)
    except McpClientError as e:
        raise HTTPException(502, f"Fetch error: {e}")

    existing = (
        db.query(Skill)
        .filter(Skill.upstream_id == req.upstream_id, Skill.source_type.in_(["imported", "forked"]))
        .first()
    )
    if existing:
        raise HTTPException(409, f"Skill already imported (id={existing.id})")

    now = datetime.datetime.utcnow()
    skill = Skill(
        name=skill_data["name"],
        description=skill_data.get("description", ""),
        status=SkillStatus.DRAFT,
        source_type="imported",
        upstream_url=f"{source.url}/skills/{req.upstream_id}",
        upstream_id=req.upstream_id,
        upstream_version=skill_data.get("upstream_version", ""),
        upstream_content=skill_data.get("system_prompt", ""),
        upstream_synced_at=now,
        is_customized=False,
        created_by=user.id,
    )
    db.add(skill)
    db.flush()

    version = SkillVersion(
        skill_id=skill.id,
        version=1,
        system_prompt=skill_data.get("system_prompt", ""),
        variables=[],
        created_by=user.id,
        change_note=f"从 {source.name} 导入 (upstream_id={req.upstream_id})",
    )
    db.add(version)
    db.commit()
    db.refresh(skill)
    return {"id": skill.id, "name": skill.name}


# ─── GitHub import ────────────────────────────────────────────────────────────

def _to_raw_url(github_url: str) -> str:
    """Convert any GitHub URL pointing to a skill folder or SKILL.md into a raw SKILL.md URL."""
    github_url = github_url.strip().rstrip("/")

    # 补全 scheme
    if github_url.startswith("github.com"):
        github_url = "https://" + github_url

    # Already raw
    if "raw.githubusercontent.com" in github_url:
        if not github_url.endswith("SKILL.md"):
            github_url = github_url.rstrip("/") + "/SKILL.md"
        return github_url

    # https://github.com/owner/repo/tree/branch/path  →  raw
    m = re.match(
        r"https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+)", github_url
    )
    if m:
        owner, repo, branch, path = m.groups()
        path = path.rstrip("/")
        if not path.endswith("SKILL.md"):
            path = path + "/SKILL.md"
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"

    # https://github.com/owner/repo/blob/branch/path/SKILL.md  →  raw
    m = re.match(
        r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)", github_url
    )
    if m:
        owner, repo, branch, path = m.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"

    # https://github.com/owner/repo/tree/branch  (branch root, no path)
    m = re.match(
        r"https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)$", github_url
    )
    if m:
        owner, repo, branch = m.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/SKILL.md"

    # https://github.com/owner/repo  (bare repo, default to main branch root)
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)$", github_url)
    if m:
        owner, repo = m.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/main/SKILL.md"

    raise ValueError(
        "无法解析 GitHub URL，请提供 skill 文件夹路径，例如：\n"
        "https://github.com/mattpocock/skills/tree/main/write-a-skill"
    )


def _parse_skill_md(raw: str) -> dict:
    """Parse frontmatter + body from a SKILL.md file."""
    name = description = ""
    body = raw

    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", raw, re.DOTALL)
    if fm_match:
        fm_text, body = fm_match.group(1), fm_match.group(2)
        for line in fm_text.splitlines():
            if line.startswith("name:"):
                name = line[5:].strip()
            elif line.startswith("description:"):
                description = line[12:].strip()

    # Fallback: use first H1 as name
    if not name:
        h1 = re.search(r"^#\s+(.+)", body, re.MULTILINE)
        name = h1.group(1).strip() if h1 else "unnamed-skill"

    # Fallback: first non-empty paragraph as description
    if not description:
        paras = [p.strip() for p in re.split(r"\n{2,}", body) if p.strip() and not p.startswith("#")]
        description = paras[0][:300] if paras else ""

    return {"name": name, "description": description, "system_prompt": body.strip()}


def _github_api_contents(owner: str, repo: str, path: str, branch: str) -> list[dict]:
    """Call GitHub API to list contents of a folder."""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    try:
        resp = httpx.get(url, headers=headers, params={"ref": branch}, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise McpClientError(f"GitHub API error: {e}") from e


class GitHubImportRequest(BaseModel):
    github_url: str


@router.post("/import-github")
def import_from_github(
    req: GitHubImportRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    try:
        raw_url = _to_raw_url(req.github_url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    try:
        resp = httpx.get(raw_url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"GitHub 返回 {e.response.status_code}，请确认 URL 正确且仓库公开")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"网络错误: {e}")

    skill_data = _parse_skill_md(resp.text)

    existing = db.query(Skill).filter(Skill.upstream_url == raw_url).first()
    if existing:
        raise HTTPException(409, f"已导入过该 Skill (id={existing.id}, name={existing.name})")

    now = datetime.datetime.utcnow()
    skill = Skill(
        name=skill_data["name"],
        description=skill_data["description"],
        status=SkillStatus.DRAFT,
        source_type="imported",
        upstream_url=raw_url,
        upstream_id=raw_url,
        upstream_version="",
        upstream_content=skill_data["system_prompt"],
        upstream_synced_at=now,
        is_customized=False,
        created_by=user.id,
    )
    db.add(skill)
    db.flush()

    version = SkillVersion(
        skill_id=skill.id,
        version=1,
        system_prompt=skill_data["system_prompt"],
        variables=[],
        created_by=user.id,
        change_note=f"从 GitHub 导入: {req.github_url}",
    )
    db.add(version)
    db.commit()
    db.refresh(skill)
    return {"id": skill.id, "name": skill.name, "description": skill.description}


def _import_one(raw_url: str, source_url: str, user_id: int, db: Session) -> dict:
    """Import a single SKILL.md. Returns {name, status: 'ok'|'skipped'|'error', ...}"""
    try:
        resp = httpx.get(raw_url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        return {"raw_url": raw_url, "status": "error", "reason": str(e)}

    skill_data = _parse_skill_md(resp.text)

    existing = db.query(Skill).filter(Skill.upstream_url == raw_url).first()
    if existing:
        return {"name": existing.name, "status": "skipped", "reason": "already imported", "id": existing.id}

    now = datetime.datetime.utcnow()
    skill = Skill(
        name=skill_data["name"],
        description=skill_data["description"],
        status=SkillStatus.DRAFT,
        source_type="imported",
        upstream_url=raw_url,
        upstream_id=raw_url,
        upstream_version="",
        upstream_content=skill_data["system_prompt"],
        upstream_synced_at=now,
        is_customized=False,
        created_by=user_id,
    )
    db.add(skill)
    db.flush()
    db.add(SkillVersion(
        skill_id=skill.id,
        version=1,
        system_prompt=skill_data["system_prompt"],
        variables=[],
        created_by=user_id,
        change_note=f"从 GitHub 批量导入: {source_url}",
    ))
    db.commit()
    db.refresh(skill)
    return {"name": skill.name, "status": "ok", "id": skill.id}


class GitHubBatchImportRequest(BaseModel):
    github_url: str  # folder URL, e.g. https://github.com/obra/superpowers/tree/main/skills


@router.post("/import-github-batch")
def import_github_batch(
    req: GitHubBatchImportRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.SUPER_ADMIN, Role.DEPT_ADMIN)),
):
    """Import all skills found under a GitHub folder (each subfolder with SKILL.md)."""
    url = req.github_url.strip().rstrip("/")

    # Parse: https://github.com/owner/repo/tree/branch/path
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)(?:/(.+))?", url)
    if not m:
        # bare repo: https://github.com/owner/repo
        m2 = re.match(r"https://github\.com/([^/]+)/([^/]+)$", url)
        if not m2:
            raise HTTPException(400, "无法解析 GitHub 文件夹 URL")
        owner, repo, branch, folder_path = m2.group(1), m2.group(2), "main", ""
    else:
        owner, repo, branch, folder_path = m.group(1), m.group(2), m.group(3), m.group(4) or ""

    try:
        entries = _github_api_contents(owner, repo, folder_path, branch)
    except McpClientError as e:
        raise HTTPException(502, str(e))

    if not isinstance(entries, list):
        raise HTTPException(400, "该路径不是一个文件夹")

    results = []
    for entry in entries:
        if entry["type"] == "dir":
            # Check if SKILL.md exists in this subfolder
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{entry['path']}/SKILL.md"
            result = _import_one(raw_url, url, user.id, db)
            results.append(result)
        elif entry["type"] == "file" and entry["name"] == "SKILL.md":
            # The folder itself is a skill (url pointed directly at a skill folder)
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{entry['path']}"
            result = _import_one(raw_url, url, user.id, db)
            results.append(result)

    ok = [r for r in results if r["status"] == "ok"]
    skipped = [r for r in results if r["status"] == "skipped"]
    errors = [r for r in results if r["status"] == "error"]
    return {"imported": len(ok), "skipped": len(skipped), "errors": len(errors), "results": results}
