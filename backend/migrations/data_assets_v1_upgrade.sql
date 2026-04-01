-- ============================================================================
-- Data Assets v1 Upgrade — 4 new tables + field/view extensions
-- ============================================================================

-- 1. TableField 扩展
ALTER TABLE table_fields
  ADD COLUMN field_role_tags JSON DEFAULT NULL AFTER sort_order,
  ADD COLUMN is_enum TINYINT(1) NOT NULL DEFAULT 0 AFTER field_role_tags,
  ADD COLUMN is_free_text TINYINT(1) NOT NULL DEFAULT 0 AFTER is_enum,
  ADD COLUMN is_sensitive TINYINT(1) NOT NULL DEFAULT 0 AFTER is_free_text;

-- 2. TableView 扩展
ALTER TABLE table_views
  ADD COLUMN visible_field_ids JSON DEFAULT NULL AFTER created_by,
  ADD COLUMN field_layout_json JSON DEFAULT NULL AFTER visible_field_ids,
  ADD COLUMN filter_rule_json JSON DEFAULT NULL AFTER field_layout_json,
  ADD COLUMN group_rule_json JSON DEFAULT NULL AFTER filter_rule_json,
  ADD COLUMN sort_rule_json JSON DEFAULT NULL AFTER group_rule_json,
  ADD COLUMN aggregate_rule_json JSON DEFAULT NULL AFTER sort_rule_json,
  ADD COLUMN row_limit INT DEFAULT NULL AFTER aggregate_rule_json,
  ADD COLUMN disclosure_ceiling VARCHAR(5) DEFAULT NULL AFTER row_limit,
  ADD COLUMN allowed_role_group_ids JSON DEFAULT NULL AFTER disclosure_ceiling,
  ADD COLUMN allowed_skill_ids JSON DEFAULT NULL AFTER allowed_role_group_ids,
  ADD COLUMN view_kind VARCHAR(20) NOT NULL DEFAULT 'list' AFTER allowed_skill_ids;

-- 3. 角色组表
CREATE TABLE IF NOT EXISTS table_role_groups (
  id INT AUTO_INCREMENT PRIMARY KEY,
  table_id INT NOT NULL,
  name VARCHAR(100) NOT NULL,
  group_type VARCHAR(20) NOT NULL DEFAULT 'human_role',
  subject_scope VARCHAR(20) NOT NULL DEFAULT 'custom',
  user_ids JSON DEFAULT NULL,
  department_ids JSON DEFAULT NULL,
  role_keys JSON DEFAULT NULL,
  skill_ids JSON DEFAULT NULL,
  description TEXT,
  is_system TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT fk_role_group_table FOREIGN KEY (table_id) REFERENCES business_tables(id) ON DELETE CASCADE,
  INDEX idx_role_group_table (table_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 4. 权限策略表
CREATE TABLE IF NOT EXISTS table_permission_policies (
  id INT AUTO_INCREMENT PRIMARY KEY,
  table_id INT NOT NULL,
  view_id INT DEFAULT NULL,
  role_group_id INT NOT NULL,
  row_access_mode VARCHAR(20) NOT NULL DEFAULT 'none',
  row_rule_json JSON DEFAULT NULL,
  field_access_mode VARCHAR(20) NOT NULL DEFAULT 'all',
  allowed_field_ids JSON DEFAULT NULL,
  blocked_field_ids JSON DEFAULT NULL,
  disclosure_level VARCHAR(5) NOT NULL DEFAULT 'L0',
  masking_rule_json JSON DEFAULT NULL,
  tool_permission_mode VARCHAR(20) NOT NULL DEFAULT 'deny',
  export_permission TINYINT(1) NOT NULL DEFAULT 0,
  reason_template TEXT,
  created_by INT DEFAULT NULL,
  updated_by INT DEFAULT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT fk_perm_policy_table FOREIGN KEY (table_id) REFERENCES business_tables(id) ON DELETE CASCADE,
  CONSTRAINT fk_perm_policy_view FOREIGN KEY (view_id) REFERENCES table_views(id) ON DELETE SET NULL,
  CONSTRAINT fk_perm_policy_role_group FOREIGN KEY (role_group_id) REFERENCES table_role_groups(id) ON DELETE CASCADE,
  CONSTRAINT fk_perm_policy_created_by FOREIGN KEY (created_by) REFERENCES users(id),
  CONSTRAINT fk_perm_policy_updated_by FOREIGN KEY (updated_by) REFERENCES users(id),
  INDEX idx_perm_policy_table (table_id),
  INDEX idx_perm_policy_role_group (role_group_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 5. 字段枚举全集表
CREATE TABLE IF NOT EXISTS field_value_dictionary (
  id INT AUTO_INCREMENT PRIMARY KEY,
  field_id INT NOT NULL,
  value VARCHAR(500) NOT NULL,
  label VARCHAR(200) DEFAULT NULL,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  source VARCHAR(20) NOT NULL DEFAULT 'manual',
  sort_order INT NOT NULL DEFAULT 0,
  hit_count INT NOT NULL DEFAULT 0,
  last_seen_at DATETIME DEFAULT NULL,
  CONSTRAINT fk_dict_field FOREIGN KEY (field_id) REFERENCES table_fields(id) ON DELETE CASCADE,
  INDEX idx_dict_field (field_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 6. Skill 数据授权表
CREATE TABLE IF NOT EXISTS skill_data_grants (
  id INT AUTO_INCREMENT PRIMARY KEY,
  skill_id INT NOT NULL,
  table_id INT NOT NULL,
  view_id INT DEFAULT NULL,
  role_group_id INT DEFAULT NULL,
  grant_mode VARCHAR(10) NOT NULL DEFAULT 'allow',
  allowed_actions JSON DEFAULT NULL,
  max_disclosure_level VARCHAR(5) NOT NULL DEFAULT 'L2',
  row_rule_override_json JSON DEFAULT NULL,
  field_rule_override_json JSON DEFAULT NULL,
  approval_required TINYINT(1) NOT NULL DEFAULT 0,
  audit_level VARCHAR(10) NOT NULL DEFAULT 'basic',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT fk_grant_skill FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE CASCADE,
  CONSTRAINT fk_grant_table FOREIGN KEY (table_id) REFERENCES business_tables(id) ON DELETE CASCADE,
  CONSTRAINT fk_grant_view FOREIGN KEY (view_id) REFERENCES table_views(id) ON DELETE SET NULL,
  CONSTRAINT fk_grant_role_group FOREIGN KEY (role_group_id) REFERENCES table_role_groups(id) ON DELETE SET NULL,
  INDEX idx_grant_skill (skill_id),
  INDEX idx_grant_table (table_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
