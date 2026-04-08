export type Role = "super_admin" | "dept_admin" | "employee";

export interface User {
  id: number;
  username: string;
  display_name: string;
  role: Role;
  department_id: number | null;
  department_name?: string | null;
  position_id?: number | null;
  position_name?: string | null;
  is_active?: boolean;
}

export interface Position {
  id: number;
  name: string;
  department_id: number | null;
  department_name: string | null;
  description: string | null;
  created_at: string;
}

export interface DataDomainField {
  name: string;
  label: string;
  sensitive: boolean;
}

export interface DataDomain {
  id: number;
  name: string;
  display_name: string;
  description: string | null;
  fields: DataDomainField[];
  created_at: string;
}

export type PolicyTargetType = "position" | "role";
export type PolicyResourceType = "business_table" | "data_domain";
export type VisibilityScope = "own" | "dept" | "all";

export interface DataScopePolicy {
  id: number;
  target_type: PolicyTargetType;
  target_position_id: number | null;
  target_position_name: string | null;
  target_role: string | null;
  resource_type: PolicyResourceType;
  business_table_id: number | null;
  business_table_name: string | null;
  data_domain_id: number | null;
  data_domain_name: string | null;
  visibility_level: VisibilityScope;
  output_mask: string[];
  created_at: string;
}

export interface Conversation {
  id: number;
  title: string;
  skill_id: number | null;
  workspace_id: number | null;
  workspace?: { name: string; icon: string; color: string } | null;
  updated_at: string;
}

export type WorkspaceStatus = "draft" | "reviewing" | "published" | "archived";

export interface Workspace {
  id: number;
  name: string;
  description: string;
  icon: string;
  color: string;
  category: string;
  status: WorkspaceStatus;
  created_by: number | null;
  department_id: number | null;
  visibility: "all" | "department";
  welcome_message: string;
  system_context?: string;
  sort_order: number;
  skills: Skill[];
  tools: { id: number; name: string; display_name: string }[];
}

export interface Message {
  id: number;
  role: "user" | "assistant" | "system";
  content: string;
  metadata?: Record<string, unknown>;
  created_at: string;
}

export interface Skill {
  id: number;
  name: string;
  description: string;
  mode: "structured" | "unstructured" | "hybrid";
  status: "draft" | "reviewing" | "published" | "archived";
  knowledge_tags: string[];
  current_version: number;
  department_id?: number | null;
}

export type ReviewStage =
  | "auto_approved"
  | "pending_dept"
  | "dept_approved_pending_super"
  | "approved"
  | "rejected";

export interface KnowledgeEntry {
  id: number;
  title: string;
  content: string;
  category: string;
  status: "pending" | "approved" | "rejected" | "archived";
  industry_tags: string[];
  platform_tags: string[];
  topic_tags: string[];
  source_type: string;
  source_file: string | null;
  capture_mode: string;
  review_level: number;
  review_level_label: string;
  review_stage: ReviewStage;
  review_stage_label: string;
  sensitivity_flags: string[];
  auto_review_note: string | null;
  created_at: string;
  // 文件/OSS
  oss_key: string | null;
  file_type: string | null;
  file_ext: string | null;
  file_size: number | null;
  // 文档渲染
  doc_render_status: "pending" | "processing" | "ready" | "failed" | null;
  doc_render_error: string | null;
  doc_render_mode: string | null;
  can_retry_render: boolean;
  // AI
  ai_title: string | null;
  ai_summary: string | null;
  ai_notes_status: string | null;
  ai_notes_error: string | null;
  // 飞书
  lark_doc_url: string | null;
  lark_doc_token: string | null;
  external_edit_mode: string | null;
  source_origin_label: string | null;
  can_refresh_from_source: boolean;
  // 文件夹/可见性
  folder_id: number | null;
  folder_name: string | null;
  folder_missing: boolean;
  is_in_my_knowledge: boolean;
  visibility_scope: { scope: string; reason: string } | null;
  // OnlyOffice
  can_open_onlyoffice: boolean;
}

export interface ModelConfig {
  id: number;
  name: string;
  provider: string;
  model_id: string;
  api_base: string;
  api_key_env: string;
  max_tokens: number;
  temperature: string;
  is_default: boolean;
}

export interface Department {
  id: number;
  name: string;
  parent_id: number | null;
  category: string;
  business_unit: string;
}

export interface BusinessTable {
  id: number;
  table_name: string;
  display_name: string;
  description: string;
  department_id: number | null;
  owner_id: number | null;
  ddl_sql: string;
  validation_rules: Record<string, { max?: number; min?: number; enum?: string[] }>;
  workflow: { stages?: string[]; field?: string };
  governance_confidence?: number | null;
  created_at: string;
  columns?: { name: string; type: string; nullable: boolean; comment: string }[];
}

export interface AuditLog {
  id: number;
  user_id: number;
  table_name: string;
  operation: string;
  row_id: string;
  old_values: Record<string, unknown> | null;
  new_values: Record<string, unknown> | null;
  sql_executed: string;
  created_at: string;
}

export interface SkillDataQuery {
  id: number;
  skill_id: number;
  query_name: string;
  query_type: "read" | "write" | "compute";
  table_name: string;
  description: string;
  template_sql: string;
}

export interface UpstreamDiff {
  has_upstream: boolean;
  source_type?: string;
  upstream_version?: string;
  upstream_synced_at?: string;
  is_customized?: boolean;
  upstream_content?: string;
  local_content?: string;
  has_new_upstream?: boolean;
  new_upstream_version?: string;
  diff_summary?: string;
  check_action?: string;
}
