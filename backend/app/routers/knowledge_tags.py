"""知识标签治理 API

标签主数据 CRUD + 语义关系管理 + 语义闭包查询。
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.knowledge_tag import (
    KnowledgeTag,
    KnowledgeTagRelation,
    TagCategory,
    TagRelationType,
)
from app.models.user import Role, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge/tags", tags=["knowledge-tags"])


def _require_super_admin(user: User) -> None:
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="需要超管权限")


# ── Pydantic Schemas ─────────────────────────────────────────────────────────

class TagCreate(BaseModel):
    name: str = Field(..., max_length=100)
    code: str = Field(..., max_length=50)
    category: str  # industry/platform/topic/scenario/custom
    parent_id: Optional[int] = None
    description: Optional[str] = None
    sort_order: int = 0


class TagUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[int] = None


class RelationCreate(BaseModel):
    source_tag_id: int
    target_tag_id: int
    relation_type: str  # synonym/broader/narrower/related
    confidence: float = 1.0


# ── 标签 CRUD ────────────────────────────────────────────────────────────────

def _tag_to_dict(tag: KnowledgeTag) -> dict:
    return {
        "id": tag.id,
        "name": tag.name,
        "code": tag.code,
        "category": tag.category.value if tag.category else None,
        "parent_id": tag.parent_id,
        "description": tag.description,
        "sort_order": tag.sort_order,
        "is_active": tag.is_active,
        "created_at": tag.created_at.isoformat() if tag.created_at else None,
    }


def _tag_tree_node(tag: KnowledgeTag) -> dict:
    node = _tag_to_dict(tag)
    node["children"] = [_tag_tree_node(c) for c in (tag.children or [])]
    return node


@router.get("")
def list_tags(
    category: Optional[str] = None,
    q: Optional[str] = None,
    tree: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """标签列表。tree=true 返回树形结构。"""
    query = db.query(KnowledgeTag)
    if category:
        query = query.filter(KnowledgeTag.category == category)
    if q:
        query = query.filter(KnowledgeTag.name.contains(q))

    if tree:
        # 返回树形：只取根节点，靠 relationship 自动加载子节点
        query = query.filter(KnowledgeTag.parent_id.is_(None))
        tags = query.order_by(KnowledgeTag.sort_order, KnowledgeTag.id).all()
        return [_tag_tree_node(t) for t in tags]
    else:
        tags = query.order_by(KnowledgeTag.sort_order, KnowledgeTag.id).all()
        return [_tag_to_dict(t) for t in tags]


@router.post("")
def create_tag(
    body: TagCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """新增标签。"""
    _require_super_admin(user)

    existing = db.query(KnowledgeTag).filter(KnowledgeTag.code == body.code).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"标签编码 {body.code} 已存在")

    if body.parent_id:
        parent = db.get(KnowledgeTag, body.parent_id)
        if not parent:
            raise HTTPException(status_code=404, detail="父标签不存在")

    tag = KnowledgeTag(
        name=body.name,
        code=body.code,
        category=body.category,
        parent_id=body.parent_id,
        description=body.description,
        sort_order=body.sort_order,
        created_by=user.id,
    )
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return _tag_to_dict(tag)


# ── 搜索召回扩展（必须在 /{tag_id} 之前注册） ──────────────────────────────

@router.get("/expand-for-search")
def expand_tags_for_search(
    tags: str = Query(..., description="逗号分隔的标签名或编码"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """搜索召回扩展：输入标签列表，返回扩展后的标签集（含同义词和下位词）。"""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    if not tag_list:
        return {"expanded": []}

    matched_tags = db.query(KnowledgeTag).filter(
        (KnowledgeTag.name.in_(tag_list)) | (KnowledgeTag.code.in_(tag_list))
    ).all()

    expanded = set()
    for tag in matched_tags:
        expanded.add(tag.name)
        syns_out = db.query(KnowledgeTagRelation).filter(
            KnowledgeTagRelation.source_tag_id == tag.id,
            KnowledgeTagRelation.relation_type == TagRelationType.SYNONYM,
        ).all()
        syns_in = db.query(KnowledgeTagRelation).filter(
            KnowledgeTagRelation.target_tag_id == tag.id,
            KnowledgeTagRelation.relation_type == TagRelationType.SYNONYM,
        ).all()
        for r in syns_out:
            t = db.get(KnowledgeTag, r.target_tag_id)
            if t:
                expanded.add(t.name)
        for r in syns_in:
            t = db.get(KnowledgeTag, r.source_tag_id)
            if t:
                expanded.add(t.name)
        narrs = db.query(KnowledgeTagRelation).filter(
            KnowledgeTagRelation.source_tag_id == tag.id,
            KnowledgeTagRelation.relation_type == TagRelationType.NARROWER,
        ).all()
        for r in narrs:
            t = db.get(KnowledgeTag, r.target_tag_id)
            if t:
                expanded.add(t.name)

    return {
        "input": tag_list,
        "expanded": sorted(expanded),
        "added_count": len(expanded) - len(tag_list),
    }


# ── 语义关系 CRUD（必须在 /{tag_id} 之前注册） ──────────────────────────────

@router.get("/relations/list")
def list_relations(
    tag_id: Optional[int] = None,
    relation_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """关系列表。"""
    query = db.query(KnowledgeTagRelation)
    if tag_id:
        query = query.filter(
            (KnowledgeTagRelation.source_tag_id == tag_id)
            | (KnowledgeTagRelation.target_tag_id == tag_id)
        )
    if relation_type:
        query = query.filter(KnowledgeTagRelation.relation_type == relation_type)

    total = query.count()
    rels = query.order_by(KnowledgeTagRelation.id.desc()).offset(offset).limit(limit).all()

    items = []
    for r in rels:
        source = db.get(KnowledgeTag, r.source_tag_id)
        target = db.get(KnowledgeTag, r.target_tag_id)
        items.append({
            "id": r.id,
            "source_tag_id": r.source_tag_id,
            "source_tag_name": source.name if source else None,
            "source_tag_code": source.code if source else None,
            "target_tag_id": r.target_tag_id,
            "target_tag_name": target.name if target else None,
            "target_tag_code": target.code if target else None,
            "relation_type": r.relation_type.value if r.relation_type else None,
            "confidence": r.confidence,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return {"total": total, "items": items}


@router.post("/relations")
def create_relation(
    body: RelationCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """新增语义关系。"""
    _require_super_admin(user)

    if body.source_tag_id == body.target_tag_id:
        raise HTTPException(status_code=400, detail="不能创建自指向关系")

    source = db.get(KnowledgeTag, body.source_tag_id)
    target = db.get(KnowledgeTag, body.target_tag_id)
    if not source or not target:
        raise HTTPException(status_code=404, detail="源标签或目标标签不存在")

    existing = db.query(KnowledgeTagRelation).filter(
        KnowledgeTagRelation.source_tag_id == body.source_tag_id,
        KnowledgeTagRelation.target_tag_id == body.target_tag_id,
        KnowledgeTagRelation.relation_type == body.relation_type,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="该关系已存在")

    rel = KnowledgeTagRelation(
        source_tag_id=body.source_tag_id,
        target_tag_id=body.target_tag_id,
        relation_type=body.relation_type,
        confidence=body.confidence,
        created_by=user.id,
    )
    db.add(rel)
    db.commit()
    db.refresh(rel)
    return {"id": rel.id}


@router.delete("/relations/{relation_id}")
def delete_relation(
    relation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """删除语义关系。"""
    _require_super_admin(user)
    rel = db.get(KnowledgeTagRelation, relation_id)
    if not rel:
        raise HTTPException(status_code=404, detail="关系不存在")
    db.delete(rel)
    db.commit()
    return {"ok": True}


# ── 标签详情（含关系） ──────────────────────────────────────────────────────

@router.get("/{tag_id}")
def get_tag(
    tag_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """标签详情（含关系列表）。"""
    tag = db.get(KnowledgeTag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="标签不存在")

    result = _tag_to_dict(tag)

    # 获取该标签的所有关系
    rels_as_source = db.query(KnowledgeTagRelation).filter(
        KnowledgeTagRelation.source_tag_id == tag_id
    ).all()
    rels_as_target = db.query(KnowledgeTagRelation).filter(
        KnowledgeTagRelation.target_tag_id == tag_id
    ).all()

    relations = []
    for r in rels_as_source:
        target = db.get(KnowledgeTag, r.target_tag_id)
        relations.append({
            "id": r.id,
            "direction": "outgoing",
            "relation_type": r.relation_type.value if r.relation_type else None,
            "related_tag_id": r.target_tag_id,
            "related_tag_name": target.name if target else None,
            "related_tag_code": target.code if target else None,
            "confidence": r.confidence,
        })
    for r in rels_as_target:
        source = db.get(KnowledgeTag, r.source_tag_id)
        # 对双向关系（synonym/related），方向不重要
        relations.append({
            "id": r.id,
            "direction": "incoming",
            "relation_type": r.relation_type.value if r.relation_type else None,
            "related_tag_id": r.source_tag_id,
            "related_tag_name": source.name if source else None,
            "related_tag_code": source.code if source else None,
            "confidence": r.confidence,
        })

    result["relations"] = relations
    return result


@router.patch("/{tag_id}")
def update_tag(
    tag_id: int,
    body: TagUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """修改标签。"""
    _require_super_admin(user)
    tag = db.get(KnowledgeTag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="标签不存在")

    if body.name is not None:
        tag.name = body.name
    if body.description is not None:
        tag.description = body.description
    if body.sort_order is not None:
        tag.sort_order = body.sort_order
    if body.is_active is not None:
        tag.is_active = body.is_active

    db.commit()
    return _tag_to_dict(tag)


@router.delete("/{tag_id}")
def delete_tag(
    tag_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """删除标签（含所有关系）。"""
    _require_super_admin(user)
    tag = db.get(KnowledgeTag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="标签不存在")

    # 删除关联关系
    db.query(KnowledgeTagRelation).filter(
        (KnowledgeTagRelation.source_tag_id == tag_id)
        | (KnowledgeTagRelation.target_tag_id == tag_id)
    ).delete(synchronize_session="fetch")

    db.delete(tag)
    db.commit()
    return {"ok": True}


# ── 语义闭包查询 ─────────────────────────────────────────────────────────────

@router.get("/{tag_id}/closure")
def get_tag_closure(
    tag_id: int,
    relation_types: str = Query("synonym,broader,narrower,related", description="逗号分隔的关系类型"),
    max_depth: int = Query(3, ge=1, le=10),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """语义闭包查询：从 tag_id 出发，按指定关系类型展开到 max_depth 层。"""
    tag = db.get(KnowledgeTag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="标签不存在")

    allowed_types = set(relation_types.split(","))
    visited: dict[int, dict] = {}
    queue: list[tuple[int, int, list[str]]] = [(tag_id, 0, [])]

    while queue:
        current_id, depth, path = queue.pop(0)
        if current_id in visited:
            continue
        current = db.get(KnowledgeTag, current_id)
        if not current:
            continue
        visited[current_id] = {
            "id": current_id,
            "name": current.name,
            "code": current.code,
            "category": current.category.value if current.category else None,
            "depth": depth,
            "path": path,
        }
        if depth >= max_depth:
            continue

        for r in db.query(KnowledgeTagRelation).filter(KnowledgeTagRelation.source_tag_id == current_id).all():
            rtype = r.relation_type.value if r.relation_type else ""
            if rtype in allowed_types and r.target_tag_id not in visited:
                queue.append((r.target_tag_id, depth + 1, path + [f"{rtype}→"]))

        for r in db.query(KnowledgeTagRelation).filter(KnowledgeTagRelation.target_tag_id == current_id).all():
            rtype = r.relation_type.value if r.relation_type else ""
            reverse_type = {"broader": "narrower", "narrower": "broader"}.get(rtype, rtype)
            if reverse_type in allowed_types and r.source_tag_id not in visited:
                queue.append((r.source_tag_id, depth + 1, path + [f"←{reverse_type}"]))

    result = [v for k, v in visited.items() if k != tag_id]
    return {
        "origin": {"id": tag_id, "name": tag.name, "code": tag.code},
        "closure": sorted(result, key=lambda x: (x["depth"], x["name"])),
        "total": len(result),
    }
