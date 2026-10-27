-- 2026-10-28: Prompt library (shared prompt sets "single" / "two" with versions) for the admin panel.
-- ADD-ONLY. Creates one new table and adds nullable columns to garment_types; changes nothing
-- that exists (no default values on old rows, no RLS change on existing tables, no RPC change).
-- Safe to run more than once.
--
-- RUN THIS BEFORE opening the new admin screens (Prompt library, Add / edit garment), and before
-- deploying the backend that serves them. Generation, try-on, the web app and the TV app do not
-- read any of this: they keep reading garment_types.look_prompt / tryon_prompt / look_tryon_prompt.
-- No template text is seeded here: Version 1 of each set is pasted in the Prompt library screen.

-- =====================================================================
-- 1. prompt_set_versions (service role only: RLS on, no policies)
-- =====================================================================
create table if not exists public.prompt_set_versions (
  id                uuid primary key default gen_random_uuid(),
  set_key           text not null check (set_key in ('single', 'two')),
  version           integer not null check (version >= 1),
  look_prompt       text not null,
  tryon_prompt      text not null,
  look_tryon_prompt text,
  note              text,
  created_at        timestamptz not null default now(),
  unique (set_key, version)
);

alter table public.prompt_set_versions enable row level security;

comment on table public.prompt_set_versions is
  'Admin prompt library. A version is never overwritten: every save adds version N+1. Service role only.';

-- =====================================================================
-- 2. garment_types: which set / version a garment uses, and its blank values
--    (all nullable; NULL prompt_set = the garment uses its own prompts, as every garment does today)
-- =====================================================================
alter table public.garment_types add column if not exists prompt_set         text;
alter table public.garment_types add column if not exists prompt_set_version integer;
alter table public.garment_types add column if not exists design_text        text;
alter table public.garment_types add column if not exists keep_text          text;
alter table public.garment_types add column if not exists part1_name         text;
alter table public.garment_types add column if not exists part1_sam_word     text;
alter table public.garment_types add column if not exists part2_name         text;
alter table public.garment_types add column if not exists part2_sam_word     text;
alter table public.garment_types add column if not exists part2_default      text;

comment on column public.garment_types.prompt_set is
  'Prompt library set: single | two. NULL = own prompts. Prompts are still read from look_prompt / tryon_prompt / look_tryon_prompt.';
comment on column public.garment_types.part2_default is
  'white | black: built-in fabric for an empty Part 2 (saved for the web app; not used yet).';

create index if not exists garment_types_prompt_set_idx
  on public.garment_types (prompt_set, prompt_set_version)
  where prompt_set is not null;
