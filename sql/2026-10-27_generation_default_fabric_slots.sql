-- 2026-10-27: generations.default_fabric_slots (built-in WHITE / BLACK fabric for optional slots).
-- ADD-ONLY. Adds one nullable column; changes nothing that exists (no default, no RLS change,
-- no RPC change). Safe to run more than once.
--
-- RUN THIS BEFORE deploying the backend + worker that read/write it.
-- Until it runs: normal generations keep working, but a "Sew a look" that needs a built-in
-- default fabric fails (and is refunded) because the backend cannot save the choice.
--
-- What it holds (written by the backend, read by the worker), e.g.:
--   [{"position": 1, "apply_to": "pallu", "slot_label": "Pallu",
--     "default_fabric": "black", "label": "Default black"}]
-- position = 0-based index among the generation's fabric images. NULL for every other generation.

alter table public.generations
  add column if not exists default_fabric_slots jsonb;

comment on column public.generations.default_fabric_slots is
  'Optional fabric slots filled with a built-in plain fabric (white/black) instead of a shop fabric. NULL when none.';
