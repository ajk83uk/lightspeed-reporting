-- One-off: clear the dedupe-log rows for the order.created events that were
-- ignored (blank event_type) before the bare-payload fix. Removing them lets
-- StoreKit "Resend" those deliveries so they get reprocessed properly.
-- Only targets the ignored rows; the lifecycle (ready_for_pickup, ...) dedupe
-- rows are left intact.
DELETE FROM storekit_webhook_events WHERE event_type = '' OR event_type IS NULL;
