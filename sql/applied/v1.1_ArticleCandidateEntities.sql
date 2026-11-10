-- Stage 1 stores the deduplicated GLiNER candidate entity list alongside the evidence bundle
-- so Stage 2 can resume from STAGE1_DONE without re-running GLiNER.
ALTER TABLE TB_Article ADD COLUMN IF NOT EXISTS candidate_entities TEXT[] DEFAULT '{}';
