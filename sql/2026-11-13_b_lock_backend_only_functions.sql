-- 2026-11-13 (B): only the backend (service role) may run five backend-only functions.
--
-- APPROVED EXCEPTION to add-only: this REMOVES the execute permission that anon (not logged in)
-- and authenticated (logged-in app users) have on these five functions today:
--   create_generation_with_credit_debit, create_generation_with_credit_debit_v2,
--   claim_next_generation_job, mark_generation_failed_with_refund, provision_shop_for_auth_user
--
-- No TV-app or web-app function is touched: resolve_shop_by_slug, get_shop_display_name,
-- set_screen_mode, set_generation_hero, get_carousel_looks, get_browse_garment_types,
-- get_browse_looks and get_browse_looks_page keep exactly the permissions they have.
-- No function body or signature changes; no table, column, RLS policy or storage rule changes.
--
-- Checked in this repo (backend, worker, adminPanel) on 2026-11-13: all five are called only
-- through the service-role client (generations_api.py, whatsapp_state.py, worker.py), or not
-- at all (create_generation_with_credit_debit and provision_shop_for_auth_user are never
-- called by this repo). The admin panel calls none of them. The web-app repo is not in this
-- repo and was not checked.
--
-- WHEN: AFTER the deploy is tested, at a quiet time. The code does not depend on this file.
-- Run it one step at a time in the Supabase SQL editor.

-- STEP 0, read-only: is any of the five called from inside the database (a trigger or another
-- function)? Expect 0 rows. If a row lists a trigger function, STOP and report it: a trigger
-- that is not SECURITY DEFINER runs as the role that fired it, and would lose access.
select p.proname as calling_function, p.prosecdef as is_security_definer
from pg_proc p join pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public'
  and p.proname not in (
    'create_generation_with_credit_debit', 'create_generation_with_credit_debit_v2',
    'claim_next_generation_job', 'mark_generation_failed_with_refund', 'provision_shop_for_auth_user')
  and (p.prosrc ilike '%create_generation_with_credit_debit%'
    or p.prosrc ilike '%claim_next_generation_job%'
    or p.prosrc ilike '%mark_generation_failed_with_refund%'
    or p.prosrc ilike '%provision_shop_for_auth_user%');

-- STEP 1, read-only: who can run these today
-- Expect exactly 5 rows with these argument lists:
--   create_generation_with_credit_debit     uuid, uuid, uuid, integer
--   create_generation_with_credit_debit_v2  uuid, uuid, jsonb, integer
--   claim_next_generation_job               (none)
--   mark_generation_failed_with_refund      uuid, uuid, text
--   provision_shop_for_auth_user            uuid, text, text, integer
-- If a row is missing or its arguments differ, STOP and report: STEP 2 would fail.
select p.proname, pg_get_function_identity_arguments(p.oid) as args,
       has_function_privilege('anon', p.oid, 'execute') as anon_can_run,
       has_function_privilege('authenticated', p.oid, 'execute') as logged_in_can_run,
       has_function_privilege('service_role', p.oid, 'execute') as backend_can_run
from pg_proc p join pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public' and p.proname in (
  'create_generation_with_credit_debit', 'create_generation_with_credit_debit_v2',
  'claim_next_generation_job', 'mark_generation_failed_with_refund', 'provision_shop_for_auth_user');

-- STEP 2: keep the backend's permission first, then remove everyone else's
grant execute on function public.create_generation_with_credit_debit(uuid, uuid, uuid, integer) to service_role;
grant execute on function public.create_generation_with_credit_debit_v2(uuid, uuid, jsonb, integer) to service_role;
grant execute on function public.claim_next_generation_job() to service_role;
grant execute on function public.mark_generation_failed_with_refund(uuid, uuid, text) to service_role;
grant execute on function public.provision_shop_for_auth_user(uuid, text, text, integer) to service_role;
revoke all on function public.create_generation_with_credit_debit(uuid, uuid, uuid, integer) from public, anon, authenticated;
revoke all on function public.create_generation_with_credit_debit_v2(uuid, uuid, jsonb, integer) from public, anon, authenticated;
revoke all on function public.claim_next_generation_job() from public, anon, authenticated;
revoke all on function public.mark_generation_failed_with_refund(uuid, uuid, text) from public, anon, authenticated;
revoke all on function public.provision_shop_for_auth_user(uuid, text, text, integer) from public, anon, authenticated;

-- STEP 3: run STEP 1 again. Expect anon false, logged_in false, backend true on all five rows.
-- Then check: generate one look in the demo shop (web app) and send `menu` on WhatsApp from
-- your own number. If anything fails, run the UNDO below.

-- UNDO (puts back exactly what STEP 2 removed). Copy, remove the leading "-- ", run:
-- grant execute on function public.create_generation_with_credit_debit(uuid, uuid, uuid, integer) to public, anon, authenticated;
-- grant execute on function public.create_generation_with_credit_debit_v2(uuid, uuid, jsonb, integer) to public, anon, authenticated;
-- grant execute on function public.claim_next_generation_job() to public, anon, authenticated;
-- grant execute on function public.mark_generation_failed_with_refund(uuid, uuid, text) to public, anon, authenticated;
-- grant execute on function public.provision_shop_for_auth_user(uuid, text, text, integer) to public, anon, authenticated;
