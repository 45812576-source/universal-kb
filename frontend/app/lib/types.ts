export type Role = "super_admin" | "dept_admin" | "employee";

export interface User {
  id: number;
  username: string;
  display_name: string;
  role: Role;
  department_id: number | null;
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
  capture_mode: string;
  review_level: number;
  review_level_label: string;
  review_stage: ReviewStage;
  review_stage_label: string;
  sensitivity_flags: string[];
  auto_review_note: string | null;
  created_at: string;
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
