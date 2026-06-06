"""Shift sales by SITE, yesterday vs the SAME DAY LAST WEEK (Europe/London).

    python -m ingest.shift_compare

For each shift (Lunch 12-5, Dinner 5-10, and All day) it prints a per-site
table comparing yesterday's net sales to the same weekday a week earlier, with
the change in £ and %, plus a collated TOTAL row.

NOTE: needs at least a week of history for the "last week" column to be
meaningful. Until then those columns will show as '-'.
"""
from __future__ import annotations

import psycopg2

from .config import settings

# shift label -> SQL filter fragment (None = all shifts / whole day)
SHIFT_VIEWS = [
    ("Lunch (12-5)", "AND shift = 'Lunch (12-5)'"),
    ("Dinner (5-10)", "AND shift = 'Dinner (5-10)'"),
    ("All day", ""),
]

SQL = """
SELECT COALESCE(site, '(unknown)') AS site,
       ROUND(COALESCE(SUM(net_ex_vat) FILTER (WHERE business_date = %(d0)s), 0), 2) AS this_net,
       ROUND(COALESCE(SUM(net_ex_vat) FILTER (WHERE business_date = %(d1)s), 0), 2) AS last_net,
       COALESCE(SUM(quantity)     FILTER (WHERE business_date = %(d0)s), 0)         AS this_units
FROM v_fact_lines
WHERE business_date IN (%(d0)s, %(d1)s)
  {shift_clause}
GROUP BY site
ORDER BY this_net DESC;
"""


def _fmt_delta(this_net, last_net):
    if last_net and last_net != 0:
        pct = (this_net - last_net) / last_net * 100
        return f"{this_net - last_net:+,.2f}", f"{pct:+.0f}%"
    return ("-", "-")


def main() -> None:
    conn = psycopg2.connect(settings.database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT ((now() AT TIME ZONE 'Europe/London')::date - 1)")
            d0 = cur.fetchone()[0]
            cur.execute("SELECT (%s::date - 7)", (d0,))
            d1 = cur.fetchone()[0]

            print(f"\nShift sales by site — {d0} vs same day last week ({d1})")
            for label, clause in SHIFT_VIEWS:
                cur.execute(SQL.format(shift_clause=clause), {"d0": d0, "d1": d1})
                rows = cur.fetchall()
                print(f"\n{label}")
                print(f"  {'site':<16}{'yest £':>11}{'last wk £':>12}{'Δ £':>12}{'Δ %':>7}{'units':>8}")
                t_this = t_last = t_units = 0.0
                for site, this_net, last_net, units in rows:
                    this_net = float(this_net or 0); last_net = float(last_net or 0)
                    units = int(units or 0)
                    t_this += this_net; t_last += last_net; t_units += units
                    dlt, pct = _fmt_delta(this_net, last_net)
                    print(f"  {site[:16]:<16}{this_net:>11,.2f}{last_net:>12,.2f}{dlt:>12}{pct:>7}{units:>8}")
                dlt, pct = _fmt_delta(t_this, t_last)
                print(f"  {'-'*60}")
                print(f"  {'TOTAL':<16}{t_this:>11,.2f}{t_last:>12,.2f}{dlt:>12}{pct:>7}{int(t_units):>8}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
