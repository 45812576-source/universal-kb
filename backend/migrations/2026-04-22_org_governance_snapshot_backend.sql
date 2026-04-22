CREATE TABLE IF NOT EXISTS org_governance_snapshot_runs (
  id INTEGER PRIMARY KEY AUTO_INCREMENT,
  run_id VARCHAR(64) NOT NULL UNIQUE,
  event_type VARCHAR(80) NOT NULL,
  workspace_id VARCHAR(255) NOT NULL,
  workspace_type VARCHAR(80) NOT NULL DEFAULT 'workspace',
  app VARCHAR(80) NOT NULL DEFAULT 'universal-kb',
  user_id INTEGER NULL,
  status VARCHAR(50) NOT NULL DEFAULT 'running',
  request_payload_json JSON NULL,
  response_summary_json JSON NULL,
  error_message TEXT NULL,
  created_at DATETIME NULL,
  updated_at DATETIME NULL,
  completed_at DATETIME NULL
);

CREATE INDEX ix_org_gov_snapshot_runs_run_id
  ON org_governance_snapshot_runs (run_id);

CREATE INDEX ix_org_gov_snapshot_runs_workspace
  ON org_governance_snapshot_runs (workspace_id, app, created_at);

CREATE TABLE IF NOT EXISTS org_governance_snapshots (
  id INTEGER PRIMARY KEY AUTO_INCREMENT,
  workspace_id VARCHAR(255) NOT NULL,
  workspace_type VARCHAR(80) NOT NULL DEFAULT 'workspace',
  app VARCHAR(80) NOT NULL DEFAULT 'universal-kb',
  title VARCHAR(255) NOT NULL,
  version VARCHAR(100) NOT NULL,
  status VARCHAR(50) NOT NULL DEFAULT 'draft',
  scope VARCHAR(50) NOT NULL DEFAULT 'all',
  source_snapshot_id INTEGER NULL,
  base_snapshot_id INTEGER NULL,
  confidence_score FLOAT NOT NULL DEFAULT 0,
  markdown_by_tab_json JSON NULL,
  structured_by_tab_json JSON NULL,
  governance_outputs_json JSON NULL,
  missing_items_json JSON NULL,
  conflicts_json JSON NULL,
  low_confidence_items_json JSON NULL,
  separation_of_duty_risks_json JSON NULL,
  change_summary_json JSON NULL,
  created_by INTEGER NULL,
  created_at DATETIME NULL,
  updated_at DATETIME NULL
);

CREATE INDEX ix_org_gov_snapshots_workspace
  ON org_governance_snapshots (workspace_id, app, created_at);

CREATE TABLE IF NOT EXISTS org_governance_snapshot_source_links (
  id INTEGER PRIMARY KEY AUTO_INCREMENT,
  snapshot_id INTEGER NOT NULL,
  source_type VARCHAR(80) NOT NULL DEFAULT 'org_memory_source',
  source_id VARCHAR(255) NULL,
  source_uri VARCHAR(1024) NULL,
  title VARCHAR(255) NULL,
  evidence_refs_json JSON NULL,
  created_at DATETIME NULL
);

CREATE TABLE IF NOT EXISTS org_governance_snapshot_tabs (
  id INTEGER PRIMARY KEY AUTO_INCREMENT,
  snapshot_id INTEGER NOT NULL,
  tab_key VARCHAR(50) NOT NULL,
  markdown TEXT NOT NULL,
  structured_json JSON NULL,
  sync_status_json JSON NULL,
  parser_warnings_json JSON NULL,
  updated_by INTEGER NULL,
  updated_at DATETIME NULL,
  CONSTRAINT uq_org_gov_snapshot_tabs_snapshot_tab UNIQUE (snapshot_id, tab_key)
);

CREATE INDEX ix_org_gov_snapshot_tabs_snapshot_tab
  ON org_governance_snapshot_tabs (snapshot_id, tab_key);
