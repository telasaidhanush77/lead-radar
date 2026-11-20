-- rules-v2 adds job-posting evidence rules: re-score every company once (runs on deploy).
INSERT INTO jobs (kind, payload) SELECT 'score', jsonb_build_object('company_id', id) FROM companies;
