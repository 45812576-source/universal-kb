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
  updated_at: string;
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
  system_prompt_preview?: string;
}

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
