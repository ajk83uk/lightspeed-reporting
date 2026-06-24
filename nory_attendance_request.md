# Draft request to Nory — per-employee rota & attendance export

**To:** [Nory account/engineering contact]
**Subject:** S3 export — can we add employee-level rota vs actual hours?

Hi [name],

Thanks for getting the S3 export set up — the daily `labour_insights.json` files
are coming through fine and we're already pulling the site-level numbers
(cost of labour, planned vs actual hours, labour %, SPLH) into our reporting.

To finish the picture we'd like to add an **employee-level** view, and I think
the export is most of the way there already. A couple of asks:

1. **Populate the per-employee detail in the export.** The current files include
   `shiftTypes` and `contributions` arrays, but in our bucket they come through
   empty. If those (or an equivalent per-employee section) can be populated, that
   would give us what we need.

2. **Both the scheduled rota AND the actual clock times, per employee, per day —
   from Nory.** For each employee/shift we'd like: employee id + name, role/
   department, site, the **scheduled** start/end, and the **actual** clock-in/
   clock-out. We need *both* sides from Nory rather than taking actuals from our
   EPOS, because only our floor, bar and management staff clock in/out on
   Lightspeed — kitchen staff don't, so the EPOS punches don't cover everyone.
   Nory is our single source that covers all staff. Our goal is to automatically
   flag anyone who started early or clocked out late versus their rota, for the
   duty manager to review the next morning.

One quick question so we set it up right:

- Is per-employee rota + attendance data available through this same S3 export
  (ideally same `Tap & Tandoor/{date}/{site}/...` layout), or would it come via a
  different feed or the Nory API?

Happy to jump on a quick call if that's easier.

Thanks,
Ajay
```

> Internal note: we deliberately are NOT using Lightspeed's clock-in/out
> (staff_shifts) for this — it only covers floor, bar and management (kitchen
> staff don't punch on the EPOS), so it under-counts. Nory must supply BOTH the
> scheduled rota and actual times so coverage is complete across all roles.
