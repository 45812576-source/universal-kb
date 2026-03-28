-- Add content_html column for cloud document editor
ALTER TABLE knowledge_entries ADD COLUMN content_html LONGTEXT NULL AFTER content;
