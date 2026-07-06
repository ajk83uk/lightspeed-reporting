"""One-off: remove the R2 pipeline test review + its file-log entry.

Run from the lightspeed-reporting folder so it picks up the same DATABASE_URL
the ingest uses:

    python cleanup_test_review.py

Safe to delete this file afterwards.
"""
import re
import psycopg2
from ingest.config import settings

url = settings.database_url
print("Connecting to:", re.sub(r"://[^@]+@", "://***@", url))

conn = psycopg2.connect(url)
cur = conn.cursor()
cur.execute("DELETE FROM sentiment_reviews WHERE reviewer = %s", ("Pipeline Test",))
n_reviews = cur.rowcount
cur.execute("DELETE FROM sentiment_files WHERE filename = %s",
            ("TAP_Reviews_2026-07-02.csv",))
n_files = cur.rowcount
conn.commit()
cur.close()
conn.close()

print(f"Deleted {n_reviews} review row(s) and {n_files} file-log row(s).")
if n_reviews == 0:
    print("NOTE: 0 review rows deleted — check the 'Connecting to' host above is Neon, not localhost.")
