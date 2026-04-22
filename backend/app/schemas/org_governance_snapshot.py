from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


SnapshotTabKey = Literal["organization", "department", "role", "person", "okr", "process"]
SnapshotScope = Literal["all", "organization", "department", "role", "person", "okr", "process", "active_tab"]
SnapshotEventType = Literal[
    "snapshot.generate",
    "snapshot.update",
    "snapshot.analyze_sources",
    "snapshot.sync_from_markdown",
    "snapshot.append_sources",
    "snapshot.resolve_questions",
]


class WorkspaceSnapshotWorkspace(BaseModel):
    app: str = "universal-kb"
    workspace_id: str
    workspace_type: str = "workspace"


class WorkspaceSnapshotSpec(BaseModel):
    scope: SnapshotScope = "all"
    active_tab: Optional[SnapshotTabKey] = None
    snapshot_id: Optional[int] = None
    base_snapshot_id: Optional[int] = None
    source_snapshot_id: Optional[int] = None
    title: Optional[str] = None


class WorkspaceSnapshotSources(BaseModel):
    source_ids: list[int] = Field(default_factory=list)
    knowledge_entry_ids: list[int] = Field(default_factory=list)
    uploaded_file_ids: list[str] = Field(default_factory=list)
    conversation_id: Optional[int] = None
    lark_doc_urls: list[str] = Field(default_factory=list)
    pasted_materials: list[str] = Field(default_factory=list)


class WorkspaceSnapshotEditor(BaseModel):
    existing_markdown_by_tab: dict[str, str] = Field(default_factory=dict)
    existing_structured_by_tab: dict[str, Any] = Field(default_factory=dict)
    tab_key: Optional[SnapshotTabKey] = None
    markdown: Optional[str] = None


class WorkspaceSnapshotOptions(BaseModel):
    output_format: str = "markdown_and_yaml"
    allow_missing_items: bool = True
    require_evidence_for_key_facts: bool = True
    preserve_existing_structured_on_parse_failure: bool = True


class WorkspaceSnapshotEventRequest(BaseModel):
    event_type: SnapshotEventType
    workspace: WorkspaceSnapshotWorkspace
    snapshot: WorkspaceSnapshotSpec = Field(default_factory=WorkspaceSnapshotSpec)
    sources: WorkspaceSnapshotSources = Field(default_factory=WorkspaceSnapshotSources)
    editor: WorkspaceSnapshotEditor = Field(default_factory=WorkspaceSnapshotEditor)
    form: dict[str, Any] = Field(default_factory=dict)
    options: WorkspaceSnapshotOptions = Field(default_factory=WorkspaceSnapshotOptions)


class SaveTabMarkdownRequest(BaseModel):
    markdown: str
    preserve_existing_structured_on_parse_failure: bool = True


class WorkspaceSnapshotRunResponse(BaseModel):
    run_id: str
    event_type: str
    status: str
    workspace_id: str
    workspace_type: str
    app: str
    response_summary: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    completed_at: Optional[str] = None
