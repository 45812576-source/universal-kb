-- 项目主表
CREATE TABLE IF NOT EXISTS projects (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(200) NOT NULL,
  description TEXT,
  status ENUM('draft','active','completed','archived') DEFAULT 'draft',
  owner_id INT NOT NULL,
  department_id INT,
  max_members INT DEFAULT 5,
  llm_generated_plan JSON,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  FOREIGN KEY (owner_id) REFERENCES users(id),
  FOREIGN KEY (department_id) REFERENCES departments(id)
);

-- 项目成员
CREATE TABLE IF NOT EXISTS project_members (
  id INT AUTO_INCREMENT PRIMARY KEY,
  project_id INT NOT NULL,
  user_id INT NOT NULL,
  role_desc TEXT,
  workspace_id INT,
  task_order INT DEFAULT 0,
  joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (project_id) REFERENCES projects(id),
  FOREIGN KEY (user_id) REFERENCES users(id),
  FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);

-- workspace 关联项目
ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS project_id INT DEFAULT NULL;
ALTER TABLE workspaces ADD CONSTRAINT IF NOT EXISTS fk_workspaces_project_id
  FOREIGN KEY (project_id) REFERENCES projects(id);

-- 项目知识共享
CREATE TABLE IF NOT EXISTS project_knowledge_shares (
  id INT AUTO_INCREMENT PRIMARY KEY,
  project_id INT NOT NULL,
  user_id INT NOT NULL,
  knowledge_id INT NOT NULL,
  shared_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (project_id) REFERENCES projects(id),
  FOREIGN KEY (user_id) REFERENCES users(id),
  FOREIGN KEY (knowledge_id) REFERENCES knowledge_entries(id)
);

-- 项目报告
CREATE TABLE IF NOT EXISTS project_reports (
  id INT AUTO_INCREMENT PRIMARY KEY,
  project_id INT NOT NULL,
  report_type ENUM('daily','weekly') NOT NULL,
  content TEXT,
  period_start DATE,
  period_end DATE,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (project_id) REFERENCES projects(id)
);

-- workspace 压缩上下文
CREATE TABLE IF NOT EXISTS project_contexts (
  id INT AUTO_INCREMENT PRIMARY KEY,
  project_id INT NOT NULL,
  workspace_id INT NOT NULL,
  summary TEXT,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  FOREIGN KEY (project_id) REFERENCES projects(id),
  FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);
