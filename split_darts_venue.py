"""One-off: split Favourite Table venue 2102 (Darts & Shuffleboard) out of
Bournemouth into its own reporting line, 'Bournemouth (Darts)'.

- Relabels the ft_site_map entry + every existing bookings row for FT code 2102.
- Clears its business_location_id: Darts shares the Bournemouth till, so its
  sales stay on the main Bournemouth line and the Darts line is bookings-only
  (this keeps Bournemouth's revenue whole and avoids double-counting sales).

Run once from the lightspeed-reporting folder:

    python split_darts_venue.py

Idempotent (safe to re-run). Safe to delete afterwards.
"""
import re
import psycopg2
from ingest.config import settings

url = settings.database_url
print("Connecting to:", re.sub(r"://[^@]+@", "://***@", url))

conn = psycopg2.connect(url)
cur = conn.cursor()

cur.execute(
    "UPDATE ft_site_map "
    "SET site_name = %s, business_location_id = NULL, updated_at = now() "
    "WHERE ft_site_code = 2102",
    ("Bournemouth (Darts)",),
)
n_map = cur.rowcount

cur.execute(
    "UPDATE bookings "
    "SET site_name = %s, business_location_id = NULL "
    "WHERE ft_site_code = 2102",
    ("Bournemouth (Darts)",),
)
n_bookings = cur.rowcount

conn.commit()
cur.close()
conn.close()

print(f"Relabelled {n_map} site-map row and {n_bookings} booking row(s) "
      f"to 'Bournemouth (Darts)'.")
