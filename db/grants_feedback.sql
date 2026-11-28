-- Dashboard read access for the guest feedback reporting views.
-- Deliberately NOT granted: v_feedback_candidates (emails) and v_feedback_alerts_pending (guest names).
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'dashboard_ro') then
    grant select on feedback_category_ratings, feedback_dish_ratings, feedback_responses,
                    v_feedback_site, v_feedback_category_scores, v_feedback_dish_scores,
                    v_feedback_summary to dashboard_ro;
  end if;
end $$;
