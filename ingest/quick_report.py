"""Ad-hoc quick report: poppadoms sold YESTERDAY (Europe/London), by site.

    python -m ingest.quick_report

A tiny sanity check you can run from the command line without Metabase. It also
prints which menu items it counted as 'poppadoms', so you can see whether the
category rules are catching the right things.
"""
from __future__ import annotations

import psycopg2

from .config import settings

_JOIN = """
FROM v_fact_lines fl
JOIN v_line_item_category ic
  ON ic.business_location_id = fl.business_location_id
 AND ic.account_reference    = fl.account_reference
 AND ic.line_id              = fl.line_id
CROSS JOIN (SELECT ((now() AT TIME ZONE 'Europe/London')::date - 1) AS d) y
WHERE ic.category = 'poppadoms'
  AND fl.business_date = y.d
"""

SQL_YESTERDAY = "SELECT ((now() AT TIME ZONE 'Europe/London')::date - 1)"
SQL_BY_SITE = f"""
SELECT fl.site,
       COALESCE(SUM(fl.quantity), 0)            AS units,
       ROUND(COALESCE(SUM(fl.net_ex_vat), 0), 2) AS net
{_JOIN}
GROUP BY fl.site
ORDER BY units DESC;
"""
SQL_NAMES = f"SELECT DISTINCT fl.name {_JOIN} ORDER BY 1;"


def main() -> None:
    conn = psycopg2.connect(settings.database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(SQL_YESTERDAY)
            yday = cur.fetchone()[0]

            cur.execute(SQL_BY_SITE)
            rows = cur.fetchall()

            print(f"\nPoppadoms sold yesterday ({yday}):\n")
            if not rows:
                print("  (none recorded)")
            else:
                total = 0
                for site, units, net in rows:
                    units = int(units or 0)
                    total += units
                    print(f"  {site or '?':<22} {units:>6} units    £{net}")
                print(f"  {'-' * 22}")
                print(f"  {'TOTAL':<22} {total:>6} units")

            cur.execute(SQL_NAMES)
            names = [r[0] for r in cur.fetchall()]
            if names:
                print("\n  Counted these menu items as 'poppadoms':")
                for n in names:
                    print("   -", n)
            else:
                print("\n  (No items matched the 'poppadoms' rules yesterday — if you")
                print("   expected some, the name patterns may need tuning.)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
