CREATE TABLE IF NOT EXISTS governance_objectives (
  id INTEGER PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(200) NOT NULL,
  code VARCHAR(100) NOT NULL,
  description TEXT NULL,
  level VARCHAR(30) NULL,
  parent_id INTEGER NULL,
  department_id INTEGER NULL,
  business_line VARCHAR(100) NULL,
  objective_role VARCHAR(50) NULL,
  sort_order INTEGER DEFAULT 0,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  created_by INTEGER NULL,
  created_at DATETIME NULL,
  updated_at DATETIME NULL,
  CONSTRAINT uq_governance_objective_parent_code UNIQUE (parent_id, code)
);

CREATE TABLE IF NOT EXISTS governance_object_types (
  id INTEGER PRIMARY KEY AUTO_INCREMENT,
  code VARCHAR(100) NOT NULL UNIQUE,
  name VARCHAR(200) NOT NULL,
  description TEXT NULL,
  dimension_schema JSON NULL,
  baseline_fields JSON NULL,
  default_consumption_modes JSON NULL,
  created_at DATETIME NULL,
  updated_at DATETIME NULL
);

CREATE TABLE IF NOT EXISTS governance_resource_libraries (
  id INTEGER PRIMARY KEY AUTO_INCREMENT,
  objective_id INTEGER NOT NULL,
  name VARCHAR(200) NOT NULL,
  code VARCHAR(100) NOT NULL,
  description TEXT NULL,
  library_type VARCHAR(50) NULL,
  object_type VARCHAR(50) NOT NULL,
  governance_mode VARCHAR(20) NULL,
  default_visibility VARCHAR(20) NULL,
  default_update_cycle VARCHAR(30) NULL,
  field_schema JSON NULL,
  consumption_scenarios JSON NULL,
  collaboration_baseline JSON NULL,
  classification_hints JSON NULL,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  created_by INTEGER NULL,
  created_at DATETIME NULL,
  updated_at DATETIME NULL,
  CONSTRAINT uq_governance_library_objective_code UNIQUE (objective_id, code)
);

CREATE TABLE IF NOT EXISTS governance_suggestion_tasks (
  id INTEGER PRIMARY KEY AUTO_INCREMENT,
  subject_type VARCHAR(50) NOT NULL,
  subject_id INTEGER NOT NULL,
  task_type VARCHAR(50) NOT NULL,
  status VARCHAR(20) DEFAULT 'pending',
  objective_id INTEGER NULL,
  resource_library_id INTEGER NULL,
  object_type_id INTEGER NULL,
  suggested_payload JSON NULL,
  reason TEXT NULL,
  confidence INTEGER DEFAULT 0,
  created_by INTEGER NULL,
  resolved_by INTEGER NULL,
  resolved_note TEXT NULL,
  created_at DATETIME NULL,
  resolved_at DATETIME NULL
);

CREATE TABLE IF NOT EXISTS governance_feedback_events (
  id INTEGER PRIMARY KEY AUTO_INCREMENT,
  suggestion_id INTEGER NULL,
  subject_type VARCHAR(50) NOT NULL,
  subject_id INTEGER NOT NULL,
  strategy_key VARCHAR(200) NOT NULL,
  event_type VARCHAR(50) NOT NULL,
  reward_score INTEGER DEFAULT 0,
  from_objective_id INTEGER NULL,
  from_resource_library_id INTEGER NULL,
  to_objective_id INTEGER NULL,
  to_resource_library_id INTEGER NULL,
  note TEXT NULL,
  created_by INTEGER NULL,
  created_at DATETIME NULL
);

CREATE TABLE IF NOT EXISTS governance_strategy_stats (
  id INTEGER PRIMARY KEY AUTO_INCREMENT,
  strategy_key VARCHAR(200) NOT NULL,
  strategy_group VARCHAR(100) NOT NULL,
  subject_type VARCHAR(50) NULL,
  objective_code VARCHAR(100) NULL,
  library_code VARCHAR(100) NULL,
  total_count INTEGER DEFAULT 0,
  success_count INTEGER DEFAULT 0,
  reject_count INTEGER DEFAULT 0,
  cumulative_reward INTEGER DEFAULT 0,
  last_reward INTEGER DEFAULT 0,
  last_event_at DATETIME NULL,
  created_at DATETIME NULL,
  updated_at DATETIME NULL,
  CONSTRAINT uq_governance_strategy_key UNIQUE (strategy_key)
);

ALTER TABLE knowledge_entries
  ADD COLUMN governance_objective_id INTEGER NULL,
  ADD COLUMN resource_library_id INTEGER NULL,
  ADD COLUMN object_type_id INTEGER NULL,
  ADD COLUMN governance_status VARCHAR(20) NULL DEFAULT 'ungoverned',
  ADD COLUMN governance_confidence FLOAT NULL,
  ADD COLUMN governance_note TEXT NULL;

ALTER TABLE business_tables
  ADD COLUMN governance_objective_id INTEGER NULL,
  ADD COLUMN resource_library_id INTEGER NULL,
  ADD COLUMN object_type_id INTEGER NULL,
  ADD COLUMN governance_status VARCHAR(20) NULL DEFAULT 'ungoverned',
  ADD COLUMN governance_note TEXT NULL;

ALTER TABLE projects
  ADD COLUMN governance_objective_id INTEGER NULL,
  ADD COLUMN resource_library_ids JSON NULL,
  ADD COLUMN governance_note TEXT NULL;

ALTER TABLE tasks
  ADD COLUMN governance_objective_id INTEGER NULL,
  ADD COLUMN resource_library_id INTEGER NULL,
  ADD COLUMN object_anchor VARCHAR(100) NULL;
