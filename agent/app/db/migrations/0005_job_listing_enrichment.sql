-- Filter/sort fields for backlog browser (work_mode derived at insert when absent).
ALTER TABLE job_listings ADD COLUMN IF NOT EXISTS work_mode TEXT;
ALTER TABLE job_listings ADD COLUMN IF NOT EXISTS applicant_count INTEGER;
ALTER TABLE job_listings ADD COLUMN IF NOT EXISTS retrieved_at TIMESTAMPTZ;
