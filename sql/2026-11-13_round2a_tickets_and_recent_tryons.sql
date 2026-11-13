-- 2026-11-13: Round 2A. A shop never pays for something it did not get.
-- ADD-ONLY and SAFE TO RUN TWICE. Nothing that exists is renamed, dropped or changed:
--   * generations.client_request_id            new nullable column + partial unique index
--   * generation_attempts.shop_result_path      new nullable column
--   * generation_attempts.shop_result_saved_at  new nullable column + partial index
--   * storage bucket "tryon-recent"             new, PRIVATE (no policies: service role only)
--   * create_generation_with_credit_debit_v3    new function, service role only
-- create_generation_with_credit_debit_v2 and every function the TV / web apps use stay exactly as they are.
--
-- RUN THIS BEFORE deploying the backend + worker.
-- Until it runs: looks use the v2 function exactly as today, try-ons work as today without
-- the 24-hour shop copy (one warning is logged), and the worker's 24-hour clean-up logs a warning.

alter table public.generations add column if not exists client_request_id uuid;
create unique index if not exists generations_shop_client_request_key
  on public.generations (shop_id, client_request_id) where client_request_id is not null;

alter table public.generation_attempts add column if not exists shop_result_path text;
alter table public.generation_attempts add column if not exists shop_result_saved_at timestamptz;
create index if not exists generation_attempts_shop_result_idx
  on public.generation_attempts (shop_id, shop_result_saved_at desc) where shop_result_saved_at is not null;

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('tryon-recent', 'tryon-recent', false, 8388608, array['image/jpeg','image/png','image/webp'])
on conflict (id) do nothing;
-- No storage policies are added: only the service role can read or write this bucket.

create or replace function public.create_generation_with_credit_debit_v3(
  p_shop_id uuid,
  p_hero_image_id uuid,
  p_fabrics jsonb,
  p_credits_cost integer,
  p_prompt_used text,
  p_model_used text default null,
  p_client_request_id uuid default null,
  p_barcode text default null,
  p_mrp numeric default null,
  p_description text default null,
  p_default_fabric_slots jsonb default null
)
returns table(generation_id uuid, status text, credits_used integer, balance_before integer, balance_after integer, duplicate boolean)
language plpgsql
security definer
set search_path to 'public'
as $function$
#variable_conflict use_column
declare
  v_generation_id uuid;
  v_hero_folder_id uuid;
  v_balance_before integer;
  v_balance_after integer;
  v_first_fabric_id uuid;
  v_count integer;
  v_existing record;
begin
  if p_shop_id is null then raise exception 'p_shop_id is required'; end if;
  if p_hero_image_id is null then raise exception 'p_hero_image_id is required'; end if;
  if p_credits_cost is null or p_credits_cost <= 0 then raise exception 'p_credits_cost must be > 0'; end if;
  if p_prompt_used is null or btrim(p_prompt_used) = '' then raise exception 'p_prompt_used is required'; end if;
  if p_fabrics is null or jsonb_typeof(p_fabrics) <> 'array' then raise exception 'p_fabrics must be a JSON array'; end if;

  v_count := jsonb_array_length(p_fabrics);
  if v_count < 1 or v_count > 6 then raise exception 'p_fabrics count must be between 1 and 6'; end if;

  if exists (
    with parsed as (
      select nullif(trim(value->>'fabric_image_id'), '')::uuid as fabric_image_id,
             lower(trim(value->>'apply_to')) as apply_to
      from jsonb_array_elements(p_fabrics)
    )
    select 1 from parsed where fabric_image_id is null or apply_to is null or apply_to = ''
  ) then
    raise exception 'Each fabric item must include fabric_image_id and apply_to';
  end if;

  if exists (
    with parsed as (
      select lower(trim(value->>'apply_to')) as apply_to from jsonb_array_elements(p_fabrics)
    )
    select 1 from parsed group by apply_to having count(*) > 1
  ) then
    raise exception 'Duplicate apply_to values are not allowed';
  end if;

  if v_count > 1 and exists (
    select 1 from jsonb_array_elements(p_fabrics) e where lower(trim(e->>'apply_to')) = 'suit_full_body'
  ) then
    raise exception 'suit_full_body cannot be combined with other apply_to values';
  end if;

  perform pg_advisory_xact_lock(2401, hashtext(p_shop_id::text));

  -- The same ticket again: answer with the look already made and charge nothing.
  if p_client_request_id is not null then
    select g.id, g.status, g.credits_used into v_existing
    from public.generations g
    where g.shop_id = p_shop_id and g.client_request_id = p_client_request_id
    limit 1;
    if found then
      return query select v_existing.id, v_existing.status::text, v_existing.credits_used,
                          null::integer, null::integer, true;
      return;
    end if;
  end if;

  select hi.folder_id into v_hero_folder_id
  from public.hero_images hi
  where hi.id = p_hero_image_id and hi.shop_id = p_shop_id;
  if v_hero_folder_id is null then raise exception 'hero image not found for this shop'; end if;

  if exists (
    with parsed as (
      select nullif(trim(value->>'fabric_image_id'), '')::uuid as fabric_image_id
      from jsonb_array_elements(p_fabrics)
    )
    select 1 from parsed p
    left join public.fabric_images fi on fi.id = p.fabric_image_id and fi.shop_id = p_shop_id
    where fi.id is null
  ) then
    raise exception 'one or more fabric images not found for this shop';
  end if;

  select p.fabric_image_id into v_first_fabric_id
  from (
    select ordinality::int as sort_order,
           nullif(trim(value->>'fabric_image_id'), '')::uuid as fabric_image_id
    from jsonb_array_elements(p_fabrics) with ordinality
  ) p
  order by p.sort_order
  limit 1;

  v_balance_before := public.get_shop_credit_balance(p_shop_id);
  if v_balance_before < p_credits_cost then
    raise exception 'insufficient credits (balance=% required=%)', v_balance_before, p_credits_cost;
  end if;
  v_balance_after := v_balance_before - p_credits_cost;

  insert into public.generations (
    shop_id, hero_image_id, fabric_image_id, folder_id, status, credits_used,
    prompt_used, generation_type, model_used, barcode, mrp, description,
    default_fabric_slots, client_request_id
  )
  values (
    p_shop_id, p_hero_image_id, v_first_fabric_id, v_hero_folder_id, 'queued', p_credits_cost,
    p_prompt_used, 'look', p_model_used, p_barcode, p_mrp, p_description,
    p_default_fabric_slots, p_client_request_id
  )
  returning id into v_generation_id;

  insert into public.generation_fabrics (
    generation_id, shop_id, fabric_image_id, apply_to, sort_order, fabric_code, fabric_color
  )
  select v_generation_id, p_shop_id, p.fabric_image_id, p.apply_to, p.sort_order, p.fabric_code, p.fabric_color
  from (
    select ordinality::int as sort_order,
           nullif(trim(value->>'fabric_image_id'), '')::uuid as fabric_image_id,
           lower(trim(value->>'apply_to')) as apply_to,
           nullif(left(trim(value->>'fabric_code'), 120), '') as fabric_code,
           nullif(left(trim(value->>'fabric_color'), 120), '') as fabric_color
    from jsonb_array_elements(p_fabrics) with ordinality
  ) p
  order by p.sort_order;

  insert into public.credit_ledger (shop_id, delta, reason, generation_id, balance_after)
  values (p_shop_id, -p_credits_cost, 'generation_debit', v_generation_id, v_balance_after);

  return query select v_generation_id, 'queued'::text, p_credits_cost, v_balance_before, v_balance_after, false;
end;
$function$;

revoke all on function public.create_generation_with_credit_debit_v3(uuid, uuid, jsonb, integer, text, text, uuid, text, numeric, text, jsonb) from public, anon, authenticated;
grant execute on function public.create_generation_with_credit_debit_v3(uuid, uuid, jsonb, integer, text, text, uuid, text, numeric, text, jsonb) to service_role;
notify pgrst, 'reload schema';
