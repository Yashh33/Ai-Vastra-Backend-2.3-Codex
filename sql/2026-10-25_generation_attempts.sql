-- 2026-10-25: generation_attempts (Operations generation log).
-- ADD-ONLY. Creates one new table + indexes; changes nothing that exists.
-- Run BLOCK 1 first, then BLOCK 2. Both are safe to run more than once.
-- Until BLOCK 1 runs, the backend's log writes fail harmlessly (one server log line each).

-- =====================================================================
-- BLOCK 1 — create the table (service role only: RLS on, no policies)
-- =====================================================================
create table if not exists public.generation_attempts (
  id                    uuid primary key default gen_random_uuid(),
  request_id            text not null,
  client_request_id     text,

  shop_id               uuid,
  user_id               uuid,
  user_email            text,
  user_label            text,

  attempt_type          text not null check (attempt_type in ('look', 'tryon', 'look_tryon')),
  garment_type_id       uuid,
  garment_type_name     text,
  fabric_mode           text check (fabric_mode in ('single', 'multi')),

  hero_path             text,
  hero_colour_corrected boolean,
  source_generation_id  uuid,
  fabric_inputs         jsonb,

  prompt_text           text,
  prompt_version_id     uuid,
  prompt_version_label  text,
  model_id              text,

  status                text not null default 'started'
                          check (status in ('started', 'running', 'succeeded', 'failed', 'timed_out')),
  failed_step           text check (failed_step in (
                          'validation', 'auth', 'credits', 'upload', 'prompt_build', 'ai_call',
                          'ai_no_image', 'ai_blocked', 'storage_save', 'db_save', 'timeout',
                          'client_network', 'unknown')),
  user_message          text,
  error_detail          text,
  http_status           integer,
  ai_finish_reason      text,
  ai_safety             jsonb,

  generation_id         uuid references public.generations(id) on delete set null,
  output_path           text,
  tryon_result_path     text,

  credits_charged       integer,
  credits_refunded      integer,

  ai_input_tokens       integer,
  ai_output_tokens      integer,
  ai_cost_inr           numeric(10, 4),

  device_label          text,
  user_agent            text,
  endpoint              text,

  started_at            timestamptz not null default now(),
  ai_started_at         timestamptz,
  ai_finished_at        timestamptz,
  finished_at           timestamptz,
  duration_ms           integer,

  backfilled            boolean not null default false,
  created_at            timestamptz not null default now()
);

alter table public.generation_attempts enable row level security;

create unique index if not exists generation_attempts_request_id_key
  on public.generation_attempts (request_id);
create index if not exists generation_attempts_created_at_idx
  on public.generation_attempts (created_at desc);
create index if not exists generation_attempts_shop_created_idx
  on public.generation_attempts (shop_id, created_at desc);
create index if not exists generation_attempts_status_created_idx
  on public.generation_attempts (status, created_at desc);
create index if not exists generation_attempts_generation_id_idx
  on public.generation_attempts (generation_id);
create index if not exists generation_attempts_client_request_id_idx
  on public.generation_attempts (client_request_id);


-- =====================================================================
-- BLOCK 2 — backfill succeeded looks since 2026-10-23 00:00 IST
-- Run AFTER block 1. Idempotent: skips generations already in the table.
-- Unknown fields stay null (shown as "not recorded"). Bulk-uploaded looks
-- (prompt_used = 'admin bulk upload') are not generations and are skipped.
-- =====================================================================
insert into public.generation_attempts (
  request_id, shop_id, attempt_type, garment_type_id, garment_type_name, fabric_mode,
  hero_path, prompt_text, model_id, status, generation_id, output_path, credits_charged,
  credits_refunded, endpoint, started_at, finished_at, duration_ms, backfilled, created_at
)
select
  'backfill-' || g.id::text,
  g.shop_id,
  'look',
  g.folder_id,
  gt.name,
  case when coalesce(fc.fabric_count, 0) > 1 then 'multi' else 'single' end,
  h.storage_path,
  g.prompt_used,
  g.model_used,
  'succeeded',
  g.id,
  g.output_path,
  g.credits_used,
  0,
  'backfill',
  g.created_at,
  g.completed_at,
  case when g.completed_at is not null
       then (extract(epoch from (g.completed_at - g.created_at)) * 1000)::integer end,
  true,
  g.created_at
from public.generations g
left join public.garment_types gt on gt.id = g.folder_id
left join public.hero_images h on h.id = g.hero_image_id
left join lateral (
  select count(*) as fabric_count from public.generation_fabrics gf where gf.generation_id = g.id
) fc on true
where g.status = 'done'
  and g.output_path is not null
  and coalesce(g.generation_type, 'look') = 'look'
  and coalesce(g.prompt_used, '') <> 'admin bulk upload'
  and g.created_at >= timestamptz '2026-10-23 00:00:00+05:30'
  and not exists (
    select 1 from public.generation_attempts a where a.generation_id = g.id
  );
