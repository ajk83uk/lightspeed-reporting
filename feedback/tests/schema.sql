-- Local test mirror of the Neon objects the service uses. Never run against Neon.
create table feedback_requests (
  token uuid primary key default gen_random_uuid(), business_location_id bigint, ft_site_code int,
  booking_ref_no text, booking_code text, visit_date date, covers int,
  status text not null default 'queued' check (status in ('queued','sent','opened','completed','suppressed','failed')),
  dry_run boolean not null default true, created_at timestamptz not null default now(),
  sent_at timestamptz, opened_at timestamptz, completed_at timestamptz,
  unique (business_location_id, booking_ref_no, visit_date));
create table feedback_responses (token uuid primary key references feedback_requests, overall_rating smallint check (overall_rating between 1 and 5),
  comment text, submitted_at timestamptz not null default now(), flagged_health boolean not null default false,
  contact_ok boolean not null default false, other_text text);
create table feedback_dish_ratings (id bigserial primary key, token uuid references feedback_requests, sku text, dish_name text, quantity numeric,
  rating smallint check (rating between 1 and 5), comment text, rated_at timestamptz not null default now(), unique (token, dish_name));
create table feedback_category_ratings (id bigserial primary key, token uuid not null references feedback_requests(token), category text not null,
  rating smallint, skipped boolean not null default false, tags text[] not null default '{}', comment text, rated_at timestamptz not null default now(), unique (token, category));
create table feedback_alerts (id bigserial primary key, token uuid not null references feedback_requests(token), kind text not null check (kind in ('low_score','health')),
  summary text, created_at timestamptz not null default now(), sent_at timestamptz, unique (token, kind));
create table feedback_suppressions (email_lower text primary key, reason text default 'unsubscribed', created_at timestamptz default now());
create table bookings (business_location_id bigint, ft_site_code int, booking_ref_no text, booking_code text, booking_date date,
  first_name text, email text, guest_count int, site_name text);
create table v_feedback_dishes (business_location_id bigint, booking_ref_no text, visit_date date, sku text, dish_name text, quantity numeric);
create table cand_src (ft_site_code int, business_location_id bigint, booking_ref_no text, booking_code text, site_name text, visit_date date, covers int, first_name text, email text);
create view v_feedback_candidates as
  select * from cand_src c where c.email ~~ '%@%'
   and not exists (select 1 from feedback_suppressions x where x.email_lower = lower(c.email))
   and not exists (select 1 from feedback_requests r where r.business_location_id = c.business_location_id and r.booking_ref_no = c.booking_ref_no and r.visit_date = c.visit_date);
