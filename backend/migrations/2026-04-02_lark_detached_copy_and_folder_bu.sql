ALTER TABLE knowledge_folders
  ADD COLUMN IF NOT EXISTS business_unit VARCHAR(100) NULL;

ALTER TABLE knowledge_entries
  ADD COLUMN IF NOT EXISTS external_edit_mode VARCHAR(50) NULL;
