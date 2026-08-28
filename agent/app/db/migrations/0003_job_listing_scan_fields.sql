-- db/migrations/0003_job_listing_scan_fields.sql — adds the two fields
-- career-ops's real `scan` mode actually reports per offer (see
-- data/scan-history.tsv columns 7 and 9) that 0001's job_listings table
-- didn't have: a free-text location string and the ATS-reported posting
-- date. Additive, separate from 0001/0002 — see 0002's header note on
-- keeping migrations incremental.
--
-- The `contacts` table is untouched: scan never populated it (career-ops's
-- scan mode doesn't extract contacts either), and it stays available for a
-- future `contacto`-mode integration.

ALTER TABLE job_listings ADD COLUMN IF NOT EXISTS location TEXT;
ALTER TABLE job_listings ADD COLUMN IF NOT EXISTS posted_at TIMESTAMPTZ;
