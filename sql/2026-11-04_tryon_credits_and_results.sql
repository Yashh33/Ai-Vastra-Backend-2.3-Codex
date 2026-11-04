-- 2026-11-04: credits for web try-ons / look try-ons, and a 7-day admin-only copy of each result.
-- ADD-ONLY. Safe to run more than once. Nothing that exists is renamed, dropped or changed:
--   * credit_ledger.attempt_id          new nullable column + unique index (attempt_id, reason)
--   * generation_attempts.result_copy_* new nullable columns + index
--   * storage bucket "tryon-results"     new, PRIVATE (no policies: only the service role can read)
--   * debit_credits_for_tryon / refund_credits_for_tryon   new functions, service role only
--
-- RUN THIS BEFORE deploying the backend + worker.
-- Until it runs: try-ons still work but are NOT charged (a warning is logged), result copies
-- are not kept (a warning is logged) and the worker's hourly cleanup logs an error.

-- 1. Ledger: which try-on attempt a row belongs to. At most one debit and one refund per attempt.
alter table public.credit_ledger
  add column if not exists attempt_id uuid;

comment on column public.credit_ledger.attempt_id is
  'Web try-on attempt (generation_attempts.id) this debit/refund belongs to. NULL for every other row.';

create unique index if not exists credit_ledger_attempt_reason_key
  on public.credit_ledger (attempt_id, reason)
  where attempt_id is not null;

-- 2. Operations: the small result copy kept for the admin.
alter table public.generation_attempts
  add column if not exists result_copy_path text;

alter table public.generation_attempts
  add column if not exists result_copy_saved_at timestamp with time zone;

comment on column public.generation_attempts.result_copy_path is
  'Path in the private tryon-results bucket of a small copy of the try-on result. Cleared when deleted after 7 days.';

create index if not exists generation_attempts_result_copy_saved_at_idx
  on public.generation_attempts (result_copy_saved_at)
  where result_copy_saved_at is not null;

-- 3. Private bucket. No storage.objects policies are added, so anon / authenticated users
--    cannot list or read it; the backend uses the service role.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('tryon-results', 'tryon-results', false, 2097152, array['image/jpeg'])
on conflict (id) do nothing;

-- 4. Debit. Locks the shop row so concurrent debits for one shop run one at a time.
--    Repeating a call for the same attempt returns the first result and charges nothing more.
create or replace function public.debit_credits_for_tryon(
  p_shop_id uuid,
  p_attempt_id uuid,
  p_amount integer
)
returns table(charged boolean, credits_used integer, balance_before integer, balance_after integer)
language plpgsql
security definer
set search_path to 'public'
as $function$
#variable_conflict use_column
declare
  v_existing record;
  v_balance integer;
begin
  if p_amount is null or p_amount <= 0 then
    raise exception 'Invalid credit amount';
  end if;

  perform 1 from shops where id = p_shop_id for update;
  if not found then
    raise exception 'Shop not found';
  end if;

  select l.delta, l.balance_after into v_existing
  from credit_ledger l
  where l.attempt_id = p_attempt_id and l.reason = 'web_tryon' and l.shop_id = p_shop_id
  limit 1;
  if found then
    return query select true, -v_existing.delta, v_existing.balance_after - v_existing.delta, v_existing.balance_after;
    return;
  end if;

  select l.balance_after into v_balance
  from credit_ledger l
  where l.shop_id = p_shop_id
  order by l.created_at desc
  limit 1;
  v_balance := coalesce(v_balance, 0);

  if v_balance < p_amount then
    raise exception 'Insufficient credits';
  end if;

  -- clock_timestamp(): the row is ordered by when it was written, after the lock.
  insert into credit_ledger (shop_id, delta, reason, balance_after, attempt_id, created_at)
  values (p_shop_id, -p_amount, 'web_tryon', v_balance - p_amount, p_attempt_id, clock_timestamp());

  return query select true, p_amount, v_balance, v_balance - p_amount;
end;
$function$;

-- 5. Refund. Only for an attempt that was charged; at most once. Repeating a call returns
--    the first refund and adds nothing.
create or replace function public.refund_credits_for_tryon(
  p_shop_id uuid,
  p_attempt_id uuid
)
returns table(refunded boolean, refund_amount integer, balance_after integer)
language plpgsql
security definer
set search_path to 'public'
as $function$
#variable_conflict use_column
declare
  v_debit record;
  v_existing record;
  v_balance integer;
begin
  perform 1 from shops where id = p_shop_id for update;
  if not found then
    raise exception 'Shop not found';
  end if;

  select l.delta into v_debit
  from credit_ledger l
  where l.attempt_id = p_attempt_id and l.reason = 'web_tryon' and l.shop_id = p_shop_id
  limit 1;
  if not found then
    return query select false, 0, null::integer;
    return;
  end if;

  select l.delta, l.balance_after into v_existing
  from credit_ledger l
  where l.attempt_id = p_attempt_id and l.reason = 'web_tryon_refund' and l.shop_id = p_shop_id
  limit 1;
  if found then
    return query select true, v_existing.delta, v_existing.balance_after;
    return;
  end if;

  select l.balance_after into v_balance
  from credit_ledger l
  where l.shop_id = p_shop_id
  order by l.created_at desc
  limit 1;
  v_balance := coalesce(v_balance, 0);

  insert into credit_ledger (shop_id, delta, reason, balance_after, attempt_id, created_at)
  values (p_shop_id, -v_debit.delta, 'web_tryon_refund', v_balance - v_debit.delta, p_attempt_id, clock_timestamp());

  return query select true, -v_debit.delta, v_balance - v_debit.delta;
end;
$function$;

-- Backend only (service role). Not callable by the apps, anon or logged-in users.
revoke all on function public.debit_credits_for_tryon(uuid, uuid, integer) from public, anon, authenticated;
revoke all on function public.refund_credits_for_tryon(uuid, uuid) from public, anon, authenticated;
grant execute on function public.debit_credits_for_tryon(uuid, uuid, integer) to service_role;
grant execute on function public.refund_credits_for_tryon(uuid, uuid) to service_role;
