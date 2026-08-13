-- Remove the Svix "Send Example" test order (placeholder venue_id 100) so it
-- stops polluting the StoreKit dashboards as a bogus Dine-in / £0 line.
DELETE FROM storekit_orders WHERE venue_id = 100;
DELETE FROM storekit_webhook_events WHERE order_id = 'some string';
