-- 2026-11-13 (A): why a look failed, when the AI was the cause.
-- ADD-ONLY. Safe to run more than once. One new nullable column; nothing else changes.
-- Nothing the TV app or web app reads is renamed, dropped or changed.
--
-- RUN THIS BEFORE deploying the backend + worker.
-- Until it runs, the worker logs one warning ("generations.error_code missing") and carries
-- on: looks still fail and refund exactly as before, only the reason code is not saved.

alter table public.generations add column if not exists error_code text;

comment on column public.generations.error_code is 'Why a look failed, when the AI was the cause: ai_busy | ai_blocked | ai_no_image. NULL otherwise.';
