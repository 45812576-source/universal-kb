-- 知识处理 Job 表
CREATE TABLE IF NOT EXISTS knowledge_jobs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    knowledge_id INT NOT NULL,
    job_type VARCHAR(20) NOT NULL COMMENT 'render | classify',
    status VARCHAR(20) NOT NULL DEFAULT 'queued' COMMENT 'queued | running | success | failed | partial_success',
    attempt_count INT DEFAULT 0,
    max_attempts INT DEFAULT 3,
    error_type VARCHAR(50) NULL,
    error_message TEXT NULL,
    trigger_source VARCHAR(20) DEFAULT 'upload' COMMENT 'upload | retry | scheduled',
    payload JSON NULL,
    started_at DATETIME NULL,
    finished_at DATETIME NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_kj_status (status),
    INDEX idx_kj_knowledge_id (knowledge_id),
    INDEX idx_kj_job_type (job_type),
    CONSTRAINT fk_kj_knowledge FOREIGN KEY (knowledge_id) REFERENCES knowledge_entries(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- KnowledgeEntry 新增分类状态字段
ALTER TABLE knowledge_entries
    ADD COLUMN classification_status VARCHAR(20) DEFAULT 'pending' NULL COMMENT 'pending/success/failed/needs_review' AFTER classification_confidence,
    ADD COLUMN classification_error TEXT NULL AFTER classification_status,
    ADD COLUMN classified_at DATETIME NULL AFTER classification_error,
    ADD COLUMN classification_source VARCHAR(50) NULL COMMENT 'keyword/llm/vector_assisted_llm/keyword_fallback' AFTER classified_at;

-- 回填现有数据：有 taxonomy_code 的标记为 success，其余保持 pending
UPDATE knowledge_entries
SET classification_status = 'success',
    classification_source = 'llm',
    classified_at = updated_at
WHERE taxonomy_code IS NOT NULL AND classification_status IS NULL;

UPDATE knowledge_entries
SET classification_status = 'pending'
WHERE taxonomy_code IS NULL AND classification_status IS NULL;
