-- Guest feedback (post-meal survey) schema. Idempotent: safe to re-run.
-- Already applied to Neon on 27-28 Nov 2026; this file records it in the repo.

create table if not exists feedback_requests (
  token uuid primary key default gen_random_uuid(),
  business_location_id bigint not null,
  ft_site_code int,
  booking_ref_no text not null,
  booking_code text,
  visit_date date not null,
  covers int,
  status text not null default 'queued'
    check (status in ('queued','sent','opened','completed','suppressed','failed')),
  dry_run boolean not null default true,
  created_at timestamptz not null default now(),
  sent_at timestamptz,
  opened_at timestamptz,
  completed_at timestamptz,
  unique (business_location_id, booking_ref_no, visit_date)
);
create index if not exists idx_feedback_requests_status on feedback_requests (status, visit_date);

create table if not exists feedback_responses (
  token uuid primary key references feedback_requests(token),
  overall_rating smallint check (overall_rating between 1 and 5),
  comment text,
  submitted_at timestamptz not null default now(),
  flagged_health boolean not null default false,
  contact_ok boolean not null default false,
  other_text text
);

create table if not exists feedback_category_ratings (
  id bigserial primary key,
  token uuid not null references feedback_requests(token),
  category text not null check (category in ('food','drink','service','atmosphere')),
  rating smallint check (rating between 1 and 5),
  skipped boolean not null default false,
  tags text[] not null default '{}',
  comment text,
  rated_at timestamptz not null default now(),
  unique (token, category)
);
create index if not exists idx_feedback_cat_ratings_cat on feedback_category_ratings (category, rated_at);

create table if not exists feedback_dish_ratings (
  id bigserial primary key,
  token uuid not null references feedback_requests(token),
  sku text,
  dish_name text not null,
  quantity numeric,
  rating smallint check (rating between 1 and 5),
  comment text,
  rated_at timestamptz not null default now(),
  unique (token, dish_name)
);
create index if not exists idx_feedback_dish_ratings_dish on feedback_dish_ratings (dish_name, rated_at);

-- Head Office alert queue (low score of 3 stars or below, allergy/illness/hygiene flag).
create table if not exists feedback_alerts (
  id bigserial primary key,
  token uuid not null references feedback_requests(token),
  kind text not null check (kind in ('low_score','health')),
  summary text,
  created_at timestamptz not null default now(),
  sent_at timestamptz,
  unique (token, kind)
);

create table if not exists feedback_suppressions (
  email_lower text primary key,
  reason text not null default 'unsubscribed',
  created_at timestamptz not null default now()
);

-- Lightspeed checks that carry a Favourite Table booking code (TASK:RN-<loc>-FTable#<n>).
-- Visit date follows the 05:00 trading-day rule.
create or replace view v_feedback_checks as
select s.business_location_id, s.account_reference, s.nb_covers,
       ((s.time_closed at time zone 'Europe/London') - interval '5 hours')::date as visit_date,
       substring(e from 'FTable#([0-9]+)') as booking_code
from sales s
join lateral jsonb_array_elements_text(s.raw->'externalReferences') e on e ~ 'FTable#'
where coalesce(s.cancelled, false) = false and s.table_name is not null
  and coalesce(s.nb_covers, 0) > 0 and s.time_closed is not null;

-- Guests we may email: dined, email on file, not unsubscribed, not already requested. CONTAINS EMAILS: no dashboard grant.
create or replace view v_feedback_candidates as
select b.ft_site_code, b.business_location_id, b.booking_ref_no, b.booking_code, b.site_name,
       c.visit_date, b.guest_count as covers, b.first_name, b.email
from (select distinct business_location_id, booking_code, visit_date from v_feedback_checks) c
join bookings b on b.business_location_id = c.business_location_id
               and b.booking_code = c.booking_code and b.booking_date = c.visit_date
where b.email ~~ '%@%'
  and not exists (select 1 from feedback_suppressions x where x.email_lower = lower(b.email))
  and not exists (select 1 from feedback_requests r
                  where r.business_location_id = b.business_location_id
                    and r.booking_ref_no = b.booking_ref_no and r.visit_date = c.visit_date);

-- Food the guest actually had (no modifiers, no free lines, dry items only).
create or replace view v_feedback_dishes as
select c.business_location_id, b.booking_ref_no, c.visit_date, sl.sku, sl.name as dish_name,
       sum(sl.quantity) as quantity
from v_feedback_checks c
join bookings b on b.business_location_id = c.business_location_id
               and b.booking_code = c.booking_code and b.booking_date = c.visit_date
join sales_lines sl on sl.business_location_id = c.business_location_id
                   and sl.account_reference = c.account_reference
join v_report_lines l on l.business_location_id = sl.business_location_id
                     and l.account_reference = sl.account_reference and l.line_id = sl.line_id
where l.wet_dry = 'dry' and sl.quantity > 0 and sl.net_with_tax > 0
  and nullif(sl.raw->>'parentLineId','') is null and sl.name not like '.%'
group by c.business_location_id, b.booking_ref_no, c.visit_date, sl.sku, sl.name;

create or replace view v_feedback_site as
select * from (values
  (1718940401139714::bigint,'Bournemouth'), (1718940401139717,'Southampton'),
  (1718940401139718,'Portsmouth'), (1718940401139719,'Peterborough'),
  (1718940401139720,'Solihull')) t(business_location_id, site_name);

-- Reporting views exclude dry-run rows.
create or replace view v_feedback_category_scores as
select s.site_name, r.visit_date, c.category, c.rating, c.tags, c.comment, c.rated_at
from feedback_category_ratings c
join feedback_requests r using (token)
join v_feedback_site s on s.business_location_id = r.business_location_id
where c.skipped = false and c.rating is not null and r.dry_run = false;

create or replace view v_feedback_dish_scores as
select s.site_name, r.visit_date, d.dish_name, d.rating, d.comment, d.rated_at
from feedback_dish_ratings d
join feedback_requests r using (token)
join v_feedback_site s on s.business_location_id = r.business_location_id
where r.dry_run = false;

create or replace view v_feedback_summary as
select s.site_name,
       count(*) filter (where r.status in ('sent','opened','completed')) as sent,
       count(*) filter (where r.opened_at is not null) as opened,
       count(*) filter (where r.status = 'completed') as completed,
       round(avg(f.overall_rating)::numeric, 2) as avg_overall
from feedback_requests r
join v_feedback_site s on s.business_location_id = r.business_location_id
left join feedback_responses f using (token)
where r.dry_run = false
group by s.site_name;

-- Alerts waiting for Head Office. Staff-facing text, contains first names: no dashboard grant.
create or replace view v_feedback_alerts_pending as
select a.id, a.kind, a.summary, a.created_at, s.site_name, r.visit_date
from feedback_alerts a
join feedback_requests r using (token)
join v_feedback_site s on s.business_location_id = r.business_location_id
where a.sent_at is null and r.dry_run = false
order by a.created_at;
