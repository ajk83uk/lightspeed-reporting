-- Lets the pocket dashboard (ingest/dashboard.py, role dashboard_ro, SELECT only)
-- read Nory daily labour. Run once, as the database owner. Safe to re-run.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_ro') THEN
        GRANT SELECT ON nory_labour_daily TO dashboard_ro;
    END IF;
END $$;
