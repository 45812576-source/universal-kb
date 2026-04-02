CREATE TABLE IF NOT EXISTS knowledge_share_links (
  id INT AUTO_INCREMENT PRIMARY KEY,
  knowledge_id INT NOT NULL,
  share_token VARCHAR(120) NOT NULL UNIQUE,
  created_by INT NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT 1,
  access_scope VARCHAR(50) NOT NULL DEFAULT 'public_readonly',
  expires_at DATETIME NULL,
  last_accessed_at DATETIME NULL,
  access_count INT NOT NULL DEFAULT 0,
  note TEXT NULL,
  created_at DATETIME NULL,
  updated_at DATETIME NULL,
  CONSTRAINT fk_knowledge_share_links_knowledge FOREIGN KEY (knowledge_id) REFERENCES knowledge_entries(id),
  CONSTRAINT fk_knowledge_share_links_user FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE INDEX ix_knowledge_share_links_knowledge_id ON knowledge_share_links (knowledge_id);
