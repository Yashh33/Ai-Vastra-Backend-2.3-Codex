-- 2026-11-05: AI cost split by token type in the Operations log.
-- ADD-ONLY. Safe to run more than once. Nothing that exists is renamed, dropped or changed:
--   * generation_attempts.ai_image_output_tokens  new nullable column
--   * generation_attempts.ai_text_output_tokens   new nullable column
-- ai_input_tokens / ai_output_tokens (total output) / ai_cost_inr keep being filled as before.
--
-- RUN THIS BEFORE deploying the backend + worker.
-- Until it runs: generations work and ai_cost_inr is saved (priced per token type), but these two
-- columns are not saved (a warning is logged), and Operations shows those rows as "old method".

alter table public.generation_attempts
  add column if not exists ai_image_output_tokens integer;

alter table public.generation_attempts
  add column if not exists ai_text_output_tokens integer;

comment on column public.generation_attempts.ai_image_output_tokens is
  'Image output tokens of the AI call (priced at the image output rate). NULL on rows from before 2026-11-05.';

comment on column public.generation_attempts.ai_text_output_tokens is
  'Text + thinking output tokens of the AI call (priced at the text output rate). NULL on rows from before 2026-11-05.';
