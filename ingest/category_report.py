"""How many of each tracked category sold YESTERDAY (Europe/London).

    python -m ingest.category_report

Shows poppadoms, 241 cocktails and desserts: total units + net sales, a per-site
split, and the menu items counted (so you can confirm the matching is right).
Run `python -m ingest.migrate` first if you've just changed the category rules.
"""
from __future__ import annotations

import psycopg2

from .config import settings

CATEGORIES = ["poppadoms", "241 cocktails", "desserts"]

_JOIN = """
FROM v_fact_lines fl
JOIN v_line_item_category ic
  ON ic.business_location_id = fl.business_location_id
 AND ic.account_reference    = fl.account_reference
 AND ic.line_id              = fl.line_id
CROSS JOIN (SELECT ((now() AT TIME ZONE 'Europe/London')::date - 1) AS d) y
WHERE ic.category = %s
  AND fl.business_date = y.d
"""

SQL_TOTAL = f"SELECT COALESCE(SUM(fl.quantity),0), ROUND(COALESCE(SUM(fl.net_ex_vat),0),2) {_JOIN}"
SQL_BY_SITE = f"SELECT fl.site, COALESCE(SUM(fl.quantity),0) {_JOIN} GROUP BY fl.site ORDER BY 2 DESC"
SQL_NAMES = f"SELECT DISTINCT fl.name {_JOIN} ORDER BY 1"


def main() -> None:
    conn = psycopg2.connect(settings.database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT ((now() AT TIME ZONE 'Europe/London')::date - 1)")
            yday = cur.fetchone()[0]
            print(f"\nYesterday ({yday}) — category report\n" + "=" * 40)
            for cat in CATEGORIES:
                cur.execute(SQL_TOTAL, (cat,))
                units, net = cur.fetchone()
                print(f"\n{cat.title()}: {int(units or 0)} units   £{net or 0:,.2f}")

                cur.execute(SQL_BY_SITE, (cat,))
                sites = cur.fetchall()
                if sites:
                    print("   " + "   ".join(f"{s}: {int(u or 0)}" for s, u in sites))

                cur.execute(SQL_NAMES, (cat,))
                names = [r[0] for r in cur.fetchall()]
                if names:
                    print("   items counted: " + ", ".join(n for n in names if n))
                else:
                    print("   (no items matched — naming may need a tweak)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
