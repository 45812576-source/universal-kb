"""Data asset visibility and ownership helpers.

The data-assets surface must not rely on legacy ``validation_rules`` as the
source of truth for table visibility.  These helpers centralize table/folder
scope decisions so list/detail/row/view endpoints cannot drift apart.
"""
from __future__ import annotations

from typing import Literal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.business import BusinessTable, DataFolder
from app.models.user import Role, User


VisibilityBucket = Literal["mine", "shared", "all"]


def is_data_admin(user: User) -> bool:
    return user.role in (Role.SUPER_ADMIN, Role.DEPT_ADMIN)


def folder_scope(folder: DataFolder | None) -> str:
    return (folder.workspace_scope if folder else None) or "personal"


def get_table_folder(db: Session, table: BusinessTable) -> DataFolder | None:
    if table.folder_id is None:
        return None
    return db.get(DataFolder, table.folder_id)


def is_data_asset_table(table: BusinessTable) -> bool:
    """Return whether a table should be treated as a governed data asset.

    We intentionally recognize both the new persisted fields and the legacy
    ``validation_rules["folder_id"]`` marker so partially migrated tables do
    not silently fall back to legacy row-scope behavior.
    """
    rules = table.validation_rules or {}
    legacy_folder_id = rules.get("folder_id")
    return any((
        table.folder_id is not None,
        legacy_folder_id not in (None, 0, "", "0"),
        (table.source_type or "blank") != "blank",
        (table.publish_status or "draft") != "draft",
        (table.field_profile_status or "pending") != "pending",
        table.last_sync_job_id is not None,
    ))


def should_use_asset_safe_default(
    user: User,
    table: BusinessTable,
    *,
    has_new_policy: bool,
) -> bool:
    """Return whether reads should prefer safe-empty over legacy fallback."""
    if is_data_admin(user):
        return False
    if table.owner_id == user.id:
        return False
    return not has_new_policy and is_data_asset_table(table)


def can_view_table(user: User, table: BusinessTable, folder: DataFolder | None = None) -> bool:
    """Return whether ``user`` can discover/read a table asset.

    Defaults are intentionally conservative:
    - admins can see all active tables;
    - owners can always see their own tables;
    - unfiled tables are owner-only;
    - personal folders are owner-only;
    - department folders are department-scoped;
    - company folders are company-visible.
    """
    if is_data_admin(user):
        return True
    if table.owner_id == user.id:
        return True

    if folder is None:
        return False

    scope = folder_scope(folder)
    if scope == "personal":
        return folder.owner_id == user.id
    if scope == "department":
        return bool(user.department_id and folder.department_id == user.department_id)
    if scope == "company":
        return True
    return False


def can_manage_table(user: User, table: BusinessTable, folder: DataFolder | None = None) -> bool:
    if is_data_admin(user):
        return True
    return table.owner_id == user.id


def can_manage_folder(user: User, folder: DataFolder | None) -> bool:
    if is_data_admin(user):
        return True
    if folder is None:
        return False
    return folder.workspace_scope == "personal" and folder.owner_id == user.id


def can_use_folder_as_target(user: User, folder: DataFolder | None) -> bool:
    """Return whether a table may be moved into ``folder`` by ``user``."""
    if folder is None:
        return True
    if is_data_admin(user):
        return True
    return folder.workspace_scope == "personal" and folder.owner_id == user.id


def visibility_bucket_for(user: User, table: BusinessTable, folder: DataFolder | None = None) -> VisibilityBucket:
    if table.owner_id == user.id:
        return "mine"
    if can_view_table(user, table, folder):
        return "shared"
    return "all"


def filter_visible_tables(
    db: Session,
    user: User,
    tables: list[BusinessTable],
    bucket: str | None = None,
) -> list[BusinessTable]:
    """Filter table list by current-user visibility and optional UI bucket."""
    folder_ids = {t.folder_id for t in tables if t.folder_id is not None}
    folders = {
        f.id: f
        for f in db.query(DataFolder).filter(DataFolder.id.in_(folder_ids)).all()
    } if folder_ids else {}

    result: list[BusinessTable] = []
    for table in tables:
        folder = folders.get(table.folder_id) if table.folder_id is not None else None
        if not can_view_table(user, table, folder):
            continue
        if bucket in ("mine", "my_tables") and table.owner_id != user.id:
            continue
        if bucket == "shared" and table.owner_id == user.id:
            continue
        result.append(table)
    return result


def require_table_view_access(db: Session, table: BusinessTable, user: User) -> DataFolder | None:
    folder = get_table_folder(db, table)
    if not can_view_table(user, table, folder):
        raise HTTPException(403, "无权访问此数据表")
    return folder


def require_table_manage_access(db: Session, table: BusinessTable, user: User) -> DataFolder | None:
    folder = get_table_folder(db, table)
    if not can_manage_table(user, table, folder):
        raise HTTPException(403, "无权操作此数据表，仅表的创建者或管理员可操作")
    return folder
