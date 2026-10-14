-- Metabase reads through the dashboard_ro role: it needs SELECT on every new
-- view before a dashboard query touches it. Guarded so fresh databases without
-- the role don't fail. Safe to re-run.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_ro') THEN
        GRANT SELECT ON v_como_tx, v_como_signups_daily, v_como_signups_weekly,
                        v_como_daily, v_como_redemptions_daily, v_como_capture_daily
            TO dashboard_ro;
    END IF;
END $$;
