-- 2026-11-02: per-shop AI image model, and the output size on each logged attempt.
-- ADD-ONLY. Adds two nullable columns; changes nothing that exists (no default, no RLS change,
-- no RPC change). Safe to run more than once.
--
-- RUN THIS BEFORE deploying the backend + worker that read/write them.
-- Until it runs: generations keep working on the env default model, image_size is not saved
-- (a warning is logged) and saving a shop's model in the admin panel returns an error.
--
-- shops.image_model_id: NULL = Default (the GEMINI_IMAGE_MODEL_ID env var). Otherwise a value
-- from the allow-list in config.py (IMAGE_MODELS).
-- generation_attempts.image_size: the output size requested, e.g. "1K". Old rows stay NULL.

alter table public.shops
  add column if not exists image_model_id text;

comment on column public.shops.image_model_id is
  'AI image model for this shop (allow-list in config.py). NULL = use the GEMINI_IMAGE_MODEL_ID env var.';

alter table public.generation_attempts
  add column if not exists image_size text;

comment on column public.generation_attempts.image_size is
  'Output image size requested from the AI, e.g. 1K. NULL on rows from before this column.';
