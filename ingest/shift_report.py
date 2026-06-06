"""What sold by SHIFT yesterday (Europe/London).

    python -m ingest.shift_report            # yesterday, top 10 per shift
    python -m ingest.shift_report 15         # yesterday, top 15 per shift

Shifts: Lunch (12-5) and Dinner (5-10). Shows each shift's net sales total and
its top-selling items. Run `python -m ingest.migrate --no-seed` first if you've
just updated the views.
"""
from __future__ import annotations

import sys

import psycopg2

from .config import settings

SHIFTS = ["Lunch (12-5)", "Dinner (5-10)"]

SQL_YESTERDAY = "SELECT ((now() AT TIME ZONE 'Europe/London')::date - 1)"

SQL_SHIFT_TOTAL = """
SELECT COALESCE(SUM(net_ex_vat), 0)
FROM v_fact_lines
WHERE business_date = ((now() AT TIME ZONE 'Europe/London')::date - 1)
  AND shift = %s;
"""

SQL_TOP_ITEMS = """
SELECT name,
       SUM(quantity)   AS units,
       ROUND(SUM(net_ex_vat), 2) AS net
FROM v_fact_lines
WHERE business_date = ((now() AT TIME ZONE 'Europe/London')::date - 1)
  AND shift = %s
GROUP BY name
ORDER BY units DESC
LIMIT %s;
"""


def main() -> None:
    top_n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    conn = psycopg2.connect(settings.database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(SQL_YESTERDAY)
            yday = cur.fetchone()[0]
            print(f"\nWhat sold by shift yesterday ({yday})\n" + "=" * 44)
            for shift in SHIFTS:
                cur.execute(SQL_SHIFT_TOTAL, (shift,))
                total = cur.fetchone()[0]
                print(f"\n{shift}   —   net sales £{total:,.2f}")
                cur.execute(SQL_TOP_ITEMS, (shift, top_n))
                rows = cur.fetchall()
                if not rows:
                    print("   (no sales)")
                    continue
                print(f"   {'#':>2}  {'item':<34} {'units':>6}  {'net £':>9}")
                for i, (name, units, net) in enumerate(rows, 1):
                    print(f"   {i:>2}  {(name or '?')[:34]:<34} {int(units or 0):>6}  {float(net or 0):>9.2f}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
